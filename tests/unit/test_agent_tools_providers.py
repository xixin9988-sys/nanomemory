"""Provider, tool registry and agent loop tests (spec §8, §22.10, §25.3–§25.4, §29.4)."""

from __future__ import annotations

import asyncio
import json

import pytest

from nanomemory.core.agent_loop import CancellationToken
from nanomemory.core.config import build_settings
from nanomemory.core.errors import NanoMemoryError
from nanomemory.core.memory_manager import MemoryManager
from nanomemory.core.memory_models import AgentResponse, ToolResult
from nanomemory.core.tools import (
    CalculatorArgs,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)
from nanomemory.providers.base import ProviderError, ProviderErrorCategory
from nanomemory.providers.mock_provider import MockProvider
from nanomemory.providers.openai_compatible import OpenAICompatibleProvider


def _agent_response(**overrides: object) -> str:
    payload = {
        "response": "done",
        "task_status": "completed",
        "next_action": None,
        "tool_calls": [],
        "memory_update": None,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _tool_call_response(name: str, arguments: dict[str, object]) -> str:
    return _agent_response(
        response="calling a tool",
        task_status="in_progress",
        tool_calls=[{"name": name, "arguments": arguments}],
        next_action="wait for tool",
    )


# --------------------------------------------------------------------------- provider
def test_mock_echo_is_deterministic() -> None:
    provider = MockProvider()

    async def run() -> tuple[str, str]:
        first = await provider.generate("<current_task>hello</current_task>")
        second = await provider.generate("<current_task>hello</current_task>")
        return first.text, second.text

    first, second = asyncio.run(run())
    assert first == second
    payload = json.loads(first)
    assert payload["tool_calls"] == []


def test_mock_scripted_exhaustion() -> None:
    provider = MockProvider(mode="scripted", scripted_responses=[])

    async def run() -> None:
        await provider.generate("x")

    with pytest.raises(NanoMemoryError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.code.value == "provider_invalid_response"
    assert excinfo.value.details.get("reason") == "mock_script_exhausted"


def test_mock_timeout_and_rate_limit_modes() -> None:
    async def call(mode: str) -> None:
        await MockProvider(mode=mode).generate("x")

    for mode, category in (("timeout", "timeout"), ("rate_limit", "rate_limit")):
        with pytest.raises(ProviderError) as excinfo:
            asyncio.run(call(mode))
        assert excinfo.value.details["category"] == category


def test_structured_output_parses_fenced_json() -> None:
    provider = MockProvider(
        mode="scripted",
        scripted_responses=["```json\n" + _agent_response() + "\n```"],
    )

    async def run() -> AgentResponse:
        parsed, _ = await provider.generate_structured("x", AgentResponse)
        return parsed  # type: ignore[return-value]

    response = asyncio.run(run())
    assert response.task_status.value == "completed"


def test_invalid_json_raises_invalid_response_after_retries() -> None:
    provider = MockProvider(mode="fail_json")

    async def run() -> None:
        await provider.generate_structured("x", AgentResponse, max_attempts=2)

    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.category is ProviderErrorCategory.INVALID_RESPONSE
    assert len(provider.calls) == 2


def test_real_provider_without_credentials_fails(tmp_path) -> None:
    settings = build_settings(
        storage_root=tmp_path / "storage", provider="openai_compatible", model="deepseek-chat"
    )
    with pytest.raises(NanoMemoryError) as excinfo:
        OpenAICompatibleProvider(settings)
    assert excinfo.value.code.value == "provider_not_configured"


# --------------------------------------------------------------------------- tools
def test_default_registry_specs_are_serialisable() -> None:
    registry = build_default_registry()
    specs = registry.specs()
    assert {spec["name"] for spec in specs} == {
        "calculator",
        "read_text_file",
        "search_memory",
        "add_long_term_memory",
    }
    rendered = json.dumps(specs)
    assert "handler" not in rendered
    assert "__call__" not in rendered


def test_unregistered_tool_rejected() -> None:
    registry = build_default_registry()
    context = ToolContext(session_id="s")

    async def run() -> ToolResult:
        return await registry.execute("shell", {"cmd": "rm -rf /"}, context)

    result = asyncio.run(run())
    assert result.success is False
    assert result.error_code == "tool_not_allowed"


def test_invalid_arguments_rejected() -> None:
    registry = build_default_registry()
    context = ToolContext(session_id="s")

    async def run() -> ToolResult:
        return await registry.execute("calculator", {"expression": ""}, context)

    result = asyncio.run(run())
    assert result.error_code == "tool_invalid_args"


def test_calculator_blocks_code_execution() -> None:
    registry = build_default_registry()
    context = ToolContext(session_id="s")

    async def run(expression: str) -> ToolResult:
        return await registry.execute("calculator", {"expression": expression}, context)

    ok = asyncio.run(run("(2 + 3) * 4"))
    assert ok.success and ok.data["result"] == 20
    blocked = asyncio.run(run("__import__('os').system('echo hi')"))
    assert blocked.success is False


def test_write_tool_requires_permission() -> None:
    registry = build_default_registry()
    args = {"content": "a fact worth storing", "category": "test"}

    async def run(allow: bool) -> ToolResult:
        return await registry.execute(
            "add_long_term_memory", args, ToolContext(session_id="s", allow_write_memory=allow)
        )

    denied = asyncio.run(run(False))
    assert denied.error_code == "tool_not_allowed"
    # Without a store the allowed call fails differently, but permission passed.
    allowed = asyncio.run(run(True))
    assert allowed.error_code != "tool_not_allowed"


def test_read_text_file_blocks_traversal(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.md").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    registry = build_default_registry()
    context = ToolContext(
        session_id="s",
        settings=build_settings(storage_root=tmp_path / "storage", read_tool_allowed_root=root),
        allowed_root=root,
    )

    async def run(path: str) -> ToolResult:
        return await registry.execute("read_text_file", {"path": path}, context)

    ok = asyncio.run(run("ok.md"))
    assert ok.success and ok.data["content"] == "hello"
    escaped = asyncio.run(run("../outside.txt"))
    assert escaped.success is False
    assert escaped.error_code in {"path_not_allowed", "symlink_detected"}


def test_tool_timeout() -> None:
    registry = ToolRegistry()

    class Args(CalculatorArgs):
        pass

    async def slow(args: CalculatorArgs, context: ToolContext) -> ToolResult:  # noqa: ARG001
        await asyncio.sleep(1.0)
        return ToolResult(success=True)

    registry.register(
        ToolSpec(
            name="slow_tool",
            description="slow",
            args_schema=CalculatorArgs,
            timeout_seconds=0.05,
        ),
        slow,
    )

    async def run() -> ToolResult:
        return await registry.execute("slow_tool", {"expression": "1"}, ToolContext(session_id="s"))

    result = asyncio.run(run())
    assert result.error_code == "tool_timeout"


def test_spec_rejects_bad_schema() -> None:
    with pytest.raises(ValueError):
        ToolSpec(name="bad", description="x", args_schema=dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ToolSpec(name="bad name", description="x", args_schema=CalculatorArgs)


# --------------------------------------------------------------------------- agent
@pytest.fixture()
def manager(tmp_path):
    settings = build_settings(
        storage_root=tmp_path / "storage",
        provider="mock",
        max_iterations=3,
        agent_max_tool_calls=5,
        request_token_budget=8000,
    )
    mgr = MemoryManager(settings, provider=MockProvider(), configure_logs=False)
    mgr.initialize_storage()
    assert mgr.startup().ok
    return mgr


def test_agent_echo_run_completes(manager) -> None:
    session_id = manager.settings.default_session_id
    result = asyncio.run(manager.run_agent("say hello", session_id=session_id))
    assert result.status == "completed"
    assert result.response
    assert result.iterations == 1
    assert result.memory_update_applied is False
    assert result.usage and result.usage[0]["usage_source"] == "mock"
    assert result.budget["tokens_used"] >= 0
    episodes = manager.read_episodes(
        __import__("nanomemory.core.episodic_memory", fromlist=["EpisodicQuery"]).EpisodicQuery(
            session_id=session_id
        )
    )
    types = [view.episode.event_type for view in episodes.events]
    assert "task_started" in types
    assert "task_completed" in types


def test_agent_executes_tool(manager) -> None:
    manager.provider = MockProvider(
        mode="scripted",
        scripted_responses=[
            _tool_call_response("calculator", {"expression": "(2+3)*4"}),
            _agent_response(response="the answer is 20"),
        ],
    )
    manager.agent.provider = manager.provider
    result = asyncio.run(
        manager.run_agent("compute (2+3)*4", session_id=manager.settings.default_session_id)
    )
    assert result.status == "completed"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "calculator"
    assert result.tool_calls[0]["success"] is True
    assert result.iterations == 2


def test_agent_invalid_json_reports_provider_error(manager) -> None:
    manager.provider = MockProvider(mode="fail_json")
    manager.agent.provider = manager.provider
    result = asyncio.run(manager.run_agent("x", session_id=manager.settings.default_session_id))
    assert result.status == "error"
    assert result.error_code == "provider_invalid_response"


def test_agent_provider_timeout(manager) -> None:
    manager.provider = MockProvider(mode="timeout")
    manager.agent.provider = manager.provider
    result = asyncio.run(manager.run_agent("x", session_id=manager.settings.default_session_id))
    assert result.error_code == "provider_timeout"


def test_agent_iteration_limit(manager) -> None:
    responses = [_tool_call_response("calculator", {"expression": "1+1"}) for _ in range(5)]
    manager.provider = MockProvider(mode="scripted", scripted_responses=responses)
    manager.agent.provider = manager.provider
    result = asyncio.run(manager.run_agent("loop", session_id=manager.settings.default_session_id))
    assert result.status == "iteration_limit"
    assert result.iterations == manager.settings.max_iterations


def test_agent_budget_exhaustion_stops_calls(manager) -> None:
    manager.settings.agent_max_total_tokens = 1
    manager.agent.settings = manager.settings
    manager.provider = MockProvider(
        mode="scripted",
        scripted_responses=[
            _tool_call_response("calculator", {"expression": "1+1"}),
            _agent_response(),
        ],
    )
    manager.agent.provider = manager.provider
    result = asyncio.run(
        manager.run_agent("budget", session_id=manager.settings.default_session_id)
    )
    assert result.status == "budget_exceeded"
    assert len(manager.provider.calls) == 1  # no further provider call after exhaustion
    assert result.budget["limits"]["max_total_tokens"] == 1


def test_agent_applies_memory_update(manager) -> None:
    manager.provider = MockProvider(
        memory_update={
            "current_goal": "Ship NanoMemory",
            "status": "in_progress",
            "completed": ["models"],
            "in_progress": ["agent loop"],
            "blockers": [],
            "next_action": "write API",
        }
    )
    manager.agent.provider = manager.provider
    result = asyncio.run(
        manager.run_agent("update memory", session_id=manager.settings.default_session_id)
    )
    assert result.memory_update_applied is True
    snapshot = manager.read_working_memory(manager.settings.default_session_id)
    assert snapshot.document.current_goal == "Ship NanoMemory"
    assert snapshot.meta.memory_version == 2


def test_agent_rejects_secret_in_memory_update(manager) -> None:
    manager.provider = MockProvider(
        memory_update={
            "current_goal": "goal",
            "next_action": "use sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
        }
    )
    manager.agent.provider = manager.provider
    result = asyncio.run(manager.run_agent("leak", session_id=manager.settings.default_session_id))
    assert result.memory_update_applied is False
    assert result.memory_update_error == "secret_detected"
    snapshot = manager.read_working_memory(manager.settings.default_session_id)
    assert snapshot.meta.memory_version == 1


def test_agent_cancellation(manager) -> None:
    token = CancellationToken()
    token.cancel()
    result = asyncio.run(
        manager.run_agent(
            "cancelled", session_id=manager.settings.default_session_id, cancel_token=token
        )
    )
    assert result.status == "cancelled"
    assert manager.provider.calls == []


def test_agent_missing_provider_credentials(tmp_path) -> None:
    settings = build_settings(
        storage_root=tmp_path / "storage", provider="openai_compatible", model="deepseek-chat"
    )
    mgr = MemoryManager(settings, configure_logs=False)
    mgr.initialize_storage()
    assert mgr.provider_error == "provider_not_configured"
    with pytest.raises(NanoMemoryError) as excinfo:
        asyncio.run(mgr.run_agent("x", session_id=settings.default_session_id))
    assert excinfo.value.code.value == "provider_not_configured"
