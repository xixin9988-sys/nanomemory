"""Overview page: health, Working Memory summary and memory counters."""

from __future__ import annotations

from typing import Any

import streamlit as st

from nanomemory.dashboard.client import ApiClient
from nanomemory.dashboard.components.ui import safe_call


def render(client: ApiClient, health: dict[str, Any] | None) -> None:
    st.header("Overview")
    if health is None:
        return
    columns = st.columns(4)
    columns[0].metric("Status", health.get("status", "unknown"))
    columns[1].metric("Provider", health.get("provider_mode", "?"))
    columns[2].metric("Version", health.get("version", "?"))
    columns[3].metric("Maintenance", "yes" if health.get("maintenance_mode") else "no")
    if health.get("reasons"):
        st.info("Degraded reasons: " + ", ".join(health["reasons"]))

    working = safe_call("overview_working", client.get_working)
    statistics = safe_call("overview_stats", client.statistics)

    if working:
        st.subheader("Working Memory")
        document = working["document"]
        st.markdown(f"**Current goal:** {document['current_goal']}")
        st.markdown(f"**Status:** {document['status']}")
        st.markdown(f"**Next action:** {document['next_action']}")
        st.caption(f"memory_version {working['memory_version']} · updated {working['updated_at']}")
        if document.get("blockers"):
            st.warning("Blockers: " + "; ".join(document["blockers"]))
        with st.expander("In progress / completed"):
            st.markdown(
                "- **In progress:** " + ("; ".join(document.get("in_progress") or []) or "none")
            )
            st.markdown(
                "- **Completed:** " + ("; ".join(document.get("completed") or []) or "none")
            )

    if statistics:
        counts = st.columns(4)
        counts[0].metric("Sessions", statistics.get("sessions", 0))
        counts[1].metric("Long-term records", statistics["long_term"].get("records", 0))
        counts[2].metric("Episodes", statistics["episodic"].get("events", 0))
        counts[3].metric("Quarantined lines", statistics["episodic"].get("pending_quarantine", 0))
        capacity = statistics["long_term"].get("capacity", {})
        st.progress(
            min(1.0, (capacity.get("capacity_percent") or 0) / 100),
            text=(
                f"Long-term capacity {capacity.get('capacity_used', 0)}/"
                f"{capacity.get('capacity_limit', 0)}"
            ),
        )
        usage = statistics.get("metrics", {})
        tokens = st.columns(3)
        tokens[0].metric("Requests", usage.get("requests", 0))
        tokens[1].metric("Exact tokens", usage.get("total_tokens_exact", 0))
        tokens[2].metric("Estimated tokens", usage.get("estimated_tokens_total", 0))
        if usage.get("usage_sources"):
            st.caption(
                "usage sources: "
                + ", ".join(f"{key}={value}" for key, value in usage["usage_sources"].items())
            )
        st.caption(
            "Estimated token counts are local heuristics and are never billed provider usage."
        )

    backup_stats = (statistics or {}).get("backups")
    if backup_stats:
        st.caption(
            f"backups: {backup_stats.get('backups', 0)} · retention "
            f"{backup_stats.get('retention_count')} + {backup_stats.get('retention_daily')} daily"
        )
