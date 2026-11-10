"""Tool registry: registration, schema validation, permissions and timeouts (spec §25.3, §29.4).

Hard rules:

* A tool exists only if it was explicitly registered in code.  Model output can never import,
  resolve or execute arbitrary callables.
* ``ToolSpec`` is metadata only; the callable lives in ``RegisteredTool`` and is never
  serialised by :meth:`ToolRegistry.specs`.
* Arguments are validated by the tool's Pydantic ``args_schema`` before the handler runs.
* Read-only tools cannot write; a ``write_memory`` tool additionally requires the caller to
  pass ``allow_write_memory=True`` in the context.
* Timeouts and the agent budget are enforced around every handler invocation.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nanomemory.core.budget import BudgetTracker
from nanomemory.core.config import PROJECT_ROOT, Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import read_text
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import ToolResult
from nanomemory.core.paths import ensure_no_symlink, is_link_like


class ToolPermission(StrEnum):
    READ_ONLY = "read_only"
    WRITE_MEMORY = "write_memory"
    EXTERNAL = "external"


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """Serialisable tool metadata; deliberately contains no callable."""

    name: str
    description: str
    args_schema: type[BaseModel]
    permission: ToolPermission = ToolPermission.READ_ONLY
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("tool name must be a simple identifier")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not (inspect.isclass(self.args_schema) and issubclass(self.args_schema, BaseModel)):
            raise ValueError("args_schema must be a Pydantic BaseModel subclass")

    def metadata(self) -> dict[str, Any]:
        """JSON-safe metadata (schema included, handler excluded)."""
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.value,
            "timeout_seconds": self.timeout_seconds,
            "args_schema": self.args_schema.model_json_schema(),
        }


ToolHandler = Callable[[BaseModel, "ToolContext"], Awaitable[ToolResult]]


@dataclass(slots=True)
class RegisteredTool:
    """Runtime registry entry: metadata plus handler."""

    spec: ToolSpec
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise ValueError("handler must be callable")


@dataclass(slots=True)
class ToolContext:
    """Everything a handler is allowed to know about the current run."""

    session_id: str
    request_id: str = ""
    settings: Settings | None = None
    working: Any = None
    long_term: Any = None
    budget: BudgetTracker | None = None
    allow_write_memory: bool = False
    allowed_root: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        if self.allowed_root is not None:
            return self.allowed_root
        if self.settings is not None and self.settings.read_tool_allowed_root is not None:
            return self.settings.read_tool_allowed_root
        return PROJECT_ROOT

    @property
    def max_file_bytes(self) -> int:
        return self.settings.read_tool_max_file_bytes if self.settings else 256 * 1024

    @property
    def allowed_extensions(self) -> frozenset[str]:
        if self.settings is None:
            return frozenset({".md", ".txt", ".json"})
        return self.settings.allowed_extensions


class ToolRegistry:
    """Explicit, allowlist-only tool registry."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self.log = get_logger("tools")

    # ------------------------------------------------------------------ registration
    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """Export metadata only (never handlers)."""
        return [entry.spec.metadata() for entry in self._tools.values()]

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    # ------------------------------------------------------------------ execution
    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        context: ToolContext,
    ) -> ToolResult:
        started = time.perf_counter()
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_NOT_ALLOWED.value,
                message="Tool is not registered.",
            )
        if entry.spec.permission is ToolPermission.WRITE_MEMORY and not context.allow_write_memory:
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_NOT_ALLOWED.value,
                message="Memory-write tools require explicit permission for this run.",
            )
        try:
            args = entry.spec.args_schema.model_validate(arguments or {})
        except ValidationError as exc:
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_INVALID_ARGS.value,
                message="Tool arguments failed schema validation.",
                data={"errors": exc.errors(include_url=False)[:5]},
            )
        if context.budget is not None:
            try:
                context.budget.check(stage=f"tool:{name}")
            except NanoMemoryError as exc:
                return ToolResult(
                    success=False,
                    error_code=exc.code.value,
                    message=exc.message,
                    data=exc.details,
                )
            context.budget.record_tool()
        try:
            result = await asyncio.wait_for(
                entry.handler(args, context), timeout=entry.spec.timeout_seconds
            )
        except TimeoutError:
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_TIMEOUT.value,
                message=f"Tool timed out after {entry.spec.timeout_seconds}s.",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except NanoMemoryError as exc:
            return ToolResult(
                success=False,
                error_code=exc.code.value,
                message=exc.message,
                data=exc.details,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the model
            self.log.warning(
                "tool_execution_failed",
                extra={"event": "tool_execution_failed", "tool": name, "type": type(exc).__name__},
            )
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_EXECUTION_FAILED.value,
                message="Tool execution failed.",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        if not isinstance(result, ToolResult):
            return ToolResult(
                success=False,
                error_code=ErrorCode.TOOL_EXECUTION_FAILED.value,
                message="Tool returned an invalid result.",
            )
        result.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return result


# --------------------------------------------------------------------------- built-in args
class CalculatorArgs(BaseModel):
    expression: str = Field(min_length=1, max_length=200)


class ReadFileArgs(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    max_bytes: int | None = Field(default=None, ge=1, le=1_000_000)


class SearchMemoryArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)


class AddMemoryArgs(BaseModel):
    content: str = Field(min_length=3, max_length=4000)
    category: str = Field(default="general", min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=10)
    importance: int = Field(default=3, ge=1, le=5)


_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Tuple,
)
_ALLOWED_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}


def _safe_eval(expression: str) -> float | int:
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise NanoMemoryError(
                code=ErrorCode.TOOL_INVALID_ARGS,
                message="Expression contains unsupported syntax.",
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                raise NanoMemoryError(
                    code=ErrorCode.TOOL_INVALID_ARGS,
                    message="Only abs/round/min/max calls are allowed.",
                )
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise NanoMemoryError(
                code=ErrorCode.TOOL_INVALID_ARGS,
                message="Only numeric literals are allowed.",
            )
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCTIONS:
            raise NanoMemoryError(
                code=ErrorCode.TOOL_INVALID_ARGS,
                message="Unknown identifier in expression.",
            )
    return eval(  # noqa: S307 - AST has been validated against a strict whitelist
        compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, dict(_ALLOWED_FUNCTIONS)
    )


# --------------------------------------------------------------------------- built-ins
async def _calculator(args: CalculatorArgs, context: ToolContext) -> ToolResult:  # noqa: ARG001
    try:
        value = _safe_eval(args.expression)
    except ZeroDivisionError:
        return ToolResult(success=False, error_code="division_by_zero", message="Division by zero.")
    return ToolResult(success=True, data={"result": value})


async def _read_text_file(args: ReadFileArgs, context: ToolContext) -> ToolResult:
    root = context.root
    candidate = root / args.path
    resolved = ensure_no_symlink(root, candidate, label="read path")
    # Memory storage is not readable through the generic file tool: it would expose other
    # sessions' memory and metadata to the model outside the retrieval contract.
    if context.settings is not None:
        storage_root = Path(context.settings.storage_root).resolve()
        try:
            resolved.relative_to(storage_root)
        except ValueError:
            pass
        else:
            return ToolResult(
                success=False,
                error_code=ErrorCode.PATH_NOT_ALLOWED.value,
                message="Files inside the memory storage root are not readable by this tool.",
            )
    if resolved.suffix.lower() not in context.allowed_extensions:
        return ToolResult(
            success=False,
            error_code=ErrorCode.PATH_NOT_ALLOWED.value,
            message="File extension is not in the read allowlist.",
            data={"extension": resolved.suffix.lower()},
        )
    limit = min(args.max_bytes or context.max_file_bytes, context.max_file_bytes)
    try:
        content = read_text(resolved, max_bytes=limit)
    except NanoMemoryError as exc:
        return ToolResult(success=False, error_code=exc.code.value, message=exc.message)
    return ToolResult(
        success=True,
        data={
            "path": resolved.relative_to(Path(root).resolve()).as_posix(),
            "bytes": len(content.encode("utf-8")),
            "content": content[:20_000],
        },
    )


async def _search_memory(args: SearchMemoryArgs, context: ToolContext) -> ToolResult:
    if context.long_term is None:
        return ToolResult(
            success=False, error_code="tool_unavailable", message="Memory store unavailable."
        )
    records = context.long_term.search(args.query, limit=args.limit)
    return ToolResult(
        success=True,
        data={
            "count": len(records),
            "records": [
                {
                    "id": record.id,
                    "content": record.content,
                    "category": record.category,
                    "tags": record.tags,
                    "source": record.source,
                    "version": record.version,
                }
                for record in records
            ],
        },
    )


async def _add_long_term_memory(args: AddMemoryArgs, context: ToolContext) -> ToolResult:
    if context.long_term is None:
        return ToolResult(
            success=False, error_code="tool_unavailable", message="Memory store unavailable."
        )
    record = context.long_term.add(
        {
            "content": args.content,
            "category": args.category,
            "tags": args.tags,
            "importance": args.importance,
            "source": "agent-tool:add_long_term_memory",
            "source_type": "model",
        },
        session_id=context.session_id,
    )
    return ToolResult(
        success=True,
        data={"id": record.id, "status": record.status.value, "version": record.version},
    )


def build_default_registry() -> ToolRegistry:
    """Register the safe built-in tools (no shell, no arbitrary paths, no network)."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="calculator",
            description="Evaluate a small arithmetic expression (numbers, + - * / % **).",
            args_schema=CalculatorArgs,
            permission=ToolPermission.READ_ONLY,
            timeout_seconds=2.0,
        ),
        _calculator,
    )
    registry.register(
        ToolSpec(
            name="read_text_file",
            description=(
                "Read a UTF-8 text file from the project root. Path must stay inside the "
                "allowed root and use an allowlisted extension."
            ),
            args_schema=ReadFileArgs,
            permission=ToolPermission.READ_ONLY,
            timeout_seconds=5.0,
        ),
        _read_text_file,
    )
    registry.register(
        ToolSpec(
            name="search_memory",
            description="Search long-term memory by keyword and return matching records.",
            args_schema=SearchMemoryArgs,
            permission=ToolPermission.READ_ONLY,
            timeout_seconds=5.0,
        ),
        _search_memory,
    )
    registry.register(
        ToolSpec(
            name="add_long_term_memory",
            description="Store one durable fact in long-term memory (requires write permission).",
            args_schema=AddMemoryArgs,
            permission=ToolPermission.WRITE_MEMORY,
            timeout_seconds=5.0,
        ),
        _add_long_term_memory,
    )
    return registry


__all__ = [
    "AddMemoryArgs",
    "CalculatorArgs",
    "ReadFileArgs",
    "RegisteredTool",
    "SearchMemoryArgs",
    "ToolContext",
    "ToolPermission",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "is_link_like",
]
