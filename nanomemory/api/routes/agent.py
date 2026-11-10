"""Agent execution endpoint (spec §12)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from nanomemory.api.deps import ManagerDep, SessionDep, rate_limit_dependency
from nanomemory.api.schemas import AgentRunRequest

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])
_agent_limit = rate_limit_dependency("agent", "rate_limit_agent_per_minute")


@router.post("/run", dependencies=[Depends(_agent_limit)])
async def run_agent(
    body: AgentRunRequest,
    manager: ManagerDep,
    session_id: SessionDep,
) -> dict[str, Any]:
    """Run one agent task with iteration, timeout and budget limits."""
    result = await manager.run_agent(
        body.task,
        session_id=session_id,
        max_iterations=body.max_iterations,
        timeout_seconds=body.timeout_seconds,
        allow_write_memory=body.allow_write_memory,
    )
    return result.as_dict()


__all__ = ["router"]
