"""MCP server tests: protocol framing, tool wiring and error paths.

The server is spawned as a real subprocess because stdout/stderr discipline and UTF-8 framing
are part of the contract with DSH.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class McpProcess:
    """Line-oriented MCP client for a spawned server."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message: dict[str, object] | str) -> None:
        assert self.process.stdin is not None
        payload = message if isinstance(message, str) else json.dumps(message)
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

    def recv(self, timeout: float = 30.0) -> dict[str, object]:
        line = self.lines.get(timeout=timeout)
        if line is None:
            raise AssertionError("MCP server closed stdout")
        return json.loads(line)

    def call(self, request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self.recv()

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.process.kill()


@pytest.fixture()
def mcp(tmp_path: Path) -> Iterator[McpProcess]:
    environment = dict(os.environ)
    environment.update(
        {
            "NANOMEMORY_MCP_STORAGE_ROOT": str(tmp_path / "storage"),
            "NANOMEMORY_PROVIDER": "mock",
            "NANOMEMORY_LOG_LEVEL": "WARNING",
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "nanomemory.mcp_server"],
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    client = McpProcess(process)
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
    )
    init = client.recv()
    assert init["result"]["serverInfo"]["name"] == "nanomemory"  # type: ignore[index]
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    try:
        yield client
    finally:
        client.close()


def test_initialize_declares_tools_and_untrusted_instructions(mcp: McpProcess) -> None:
    # Re-initialize to inspect the payload (idempotent by design).
    mcp.send(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        }
    )
    payload = mcp.recv()["result"]
    assert "tools" in payload["capabilities"]
    assert "untrusted" in payload["instructions"].lower()
    assert payload["serverInfo"]["version"]


def test_tools_list_has_schemas(mcp: McpProcess) -> None:
    mcp.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = mcp.recv()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "nanomemory_health",
        "nanomemory_get_working",
        "nanomemory_update_working",
        "nanomemory_add_memory",
        "nanomemory_search_memory",
        "nanomemory_list_episodes",
        "nanomemory_run_agent",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_working_memory_round_trip_and_version_conflict(mcp: McpProcess) -> None:
    working = mcp.call(3, "nanomemory_get_working", {})
    snapshot = json.loads(working["result"]["content"][0]["text"])
    assert snapshot["memory_version"] == 1

    updated = mcp.call(
        4,
        "nanomemory_update_working",
        {"expected_version": 1, "current_goal": "MCP round trip", "status": "in_progress"},
    )
    assert updated["result"].get("isError") is not True
    assert json.loads(updated["result"]["content"][0]["text"])["memory_version"] == 2

    stale = mcp.call(
        5, "nanomemory_update_working", {"expected_version": 1, "current_goal": "stale"}
    )
    assert stale["result"]["isError"] is True
    error = json.loads(stale["result"]["content"][0]["text"])
    assert error["code"] == "version_conflict"


def test_add_and_search_memory(mcp: McpProcess) -> None:
    created = mcp.call(
        6,
        "nanomemory_add_memory",
        {"content": "The MCP bridge uses stdio transport.", "category": "plugin"},
    )
    record = json.loads(created["result"]["content"][0]["text"])
    assert record["id"].startswith("mem-")

    found = mcp.call(7, "nanomemory_search_memory", {"query": "stdio transport"})
    payload = json.loads(found["result"]["content"][0]["text"])
    assert payload["count"] >= 1
    assert any("stdio" in item["content"] for item in payload["records"])


def test_secret_is_rejected(mcp: McpProcess) -> None:
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    response = mcp.call(
        8, "nanomemory_add_memory", {"content": f"key {secret}", "category": "test"}
    )
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"])["code"] == "secret_detected"


def test_list_episodes_after_agent_run(mcp: McpProcess) -> None:
    agent = mcp.call(9, "nanomemory_run_agent", {"task": "mcp smoke task"})
    assert agent["result"].get("isError") is not True
    result = json.loads(agent["result"]["content"][0]["text"])
    assert result["status"] in {"completed", "in_progress"}
    episodes = mcp.call(10, "nanomemory_list_episodes", {"limit": 5})
    payload = json.loads(episodes["result"]["content"][0]["text"])
    assert payload["count"] >= 1


def test_unknown_tool_returns_jsonrpc_error(mcp: McpProcess) -> None:
    mcp.send(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "shell", "arguments": {"cmd": "rm -rf /"}},
        }
    )
    response = mcp.recv()
    assert response["error"]["code"] == -32602


def test_parse_error_is_reported(mcp: McpProcess) -> None:
    mcp.send("{not json")
    response = mcp.recv()
    assert response["error"]["code"] == -32700


def test_health_reports_mock_provider(mcp: McpProcess) -> None:
    response = mcp.call(12, "nanomemory_health", {})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["provider_mode"] == "mock"
    assert payload["status"] in {"ok", "degraded"}
