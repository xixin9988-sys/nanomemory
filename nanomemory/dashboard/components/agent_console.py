"""Agent Console: run a task and inspect retrieval, tools, budget and memory proposals."""

from __future__ import annotations

import streamlit as st

from nanomemory.dashboard.client import ApiClient
from nanomemory.dashboard.components.ui import render_table, safe_call


def render(client: ApiClient) -> None:
    st.header("Agent Console")
    st.caption(
        "The agent receives only the memory selected by the retriever. Memory content is "
        "untrusted data; tool permissions are enforced by the API, not by the model."
    )
    with st.form("agent_form"):
        task = st.text_area("Task", placeholder="Describe the task for the agent…")
        columns = st.columns(3)
        max_iterations = columns[0].number_input("Max iterations", 1, 20, 5)
        timeout = columns[1].number_input("Timeout (s)", 1, 600, 120)
        allow_write = columns[2].checkbox("Allow memory-write tools", value=False)
        submitted = st.form_submit_button("Run agent")

    if submitted and task.strip():
        payload = {
            "task": task,
            "max_iterations": int(max_iterations),
            "timeout_seconds": float(timeout),
            "allow_write_memory": bool(allow_write),
        }
        with st.spinner("Running agent…"):
            result = safe_call("agent_run", lambda: client.run_agent(payload))
        if result:
            st.session_state["last_agent_result"] = result

    result = st.session_state.get("last_agent_result")
    if not result:
        return

    status = result.get("status")
    if status == "completed":
        st.success(f"completed in {result.get('iterations')} iteration(s)")
    elif status in {"error", "budget_exceeded", "timeout", "iteration_limit", "cancelled"}:
        st.warning(f"status: {status}")
    else:
        st.info(f"status: {status}")
    st.markdown(result.get("response") or "_(no response)_")
    if result.get("next_action"):
        st.caption(f"next action: {result['next_action']}")

    columns = st.columns(4)
    columns[0].metric("Iterations", result.get("iterations", 0))
    columns[1].metric("Tool calls", len(result.get("tool_calls") or []))
    columns[2].metric("Latency (ms)", result.get("latency_ms", 0))
    columns[3].metric("Tokens used", (result.get("budget") or {}).get("tokens_used", 0))

    memory = result.get("memory_update") or {}
    if memory:
        if memory.get("applied"):
            st.success(f"Memory update applied (memory_version {memory.get('memory_version')})")
        else:
            st.warning("Memory update rejected: " + str(memory.get("error") or "unknown reason"))

    retrieval = result.get("retrieval") or {}
    if retrieval:
        st.subheader("Memory used")
        st.write(
            f"selected {retrieval.get('selected')} · omitted {retrieval.get('omitted')} · "
            f"{retrieval.get('estimated_tokens')}/{retrieval.get('budget')} tokens"
        )
        st.markdown("- **selected ids:** " + (", ".join(retrieval.get("ids") or []) or "none"))

    context = result.get("context") or {}
    if context:
        with st.expander("Context assembly"):
            st.json(context)

    if result.get("tool_calls"):
        st.subheader("Tool calls")
        render_table(
            [
                {
                    "name": call["name"],
                    "success": call["success"],
                    "error_code": call.get("error_code"),
                    "duration_ms": call.get("duration_ms"),
                    "arguments": call.get("arguments"),
                }
                for call in result["tool_calls"]
            ],
            use_container_width=True,
        )

    usage = result.get("usage") or []
    if usage:
        st.subheader("Usage")
        render_table(
            [
                {
                    "request_id": record["request_id"],
                    "usage_source": record["usage_source"],
                    "prompt_tokens": record.get("prompt_tokens"),
                    "completion_tokens": record.get("completion_tokens"),
                    "estimated_tokens": record.get("estimated_tokens"),
                    "cost": record.get("cost"),
                }
                for record in usage
            ],
            use_container_width=True,
        )
    if result.get("error"):
        st.error(f"{result['error']['code']}: {result['error']['message']}")
    with st.expander("Raw result"):
        st.json(result)
