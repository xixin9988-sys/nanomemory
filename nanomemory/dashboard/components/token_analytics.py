"""Token analytics: exact vs estimated usage, cost and latency."""

from __future__ import annotations

from typing import Any

import streamlit as st

from nanomemory.dashboard.client import ApiClient
from nanomemory.dashboard.components.ui import render_table, safe_call


def render(client: ApiClient) -> None:
    st.header("Token Analytics")
    summary = safe_call("token_summary", client.summary)
    if summary:
        sources = summary.get("usage_sources", {})
        columns = st.columns(4)
        columns[0].metric("Requests", summary.get("requests", 0))
        columns[1].metric("Exact tokens", summary.get("total_tokens_exact", 0))
        columns[2].metric("Estimated tokens", summary.get("estimated_tokens_total", 0))
        columns[3].metric("Corrupt lines", summary.get("corrupt_lines", 0))
        st.caption(
            "usage sources — exact: {exact}, estimated: {estimated}, unknown: {unknown}, "
            "mock: {mock}".format(
                exact=sources.get("exact", 0),
                estimated=sources.get("estimated", 0),
                unknown=sources.get("unknown", 0),
                mock=sources.get("mock", 0),
            )
        )
        latency = summary.get("latency_ms", {})
        latency_columns = st.columns(3)
        latency_columns[0].metric("Latency avg", latency.get("avg") or "N/A")
        latency_columns[1].metric("Latency p50", latency.get("p50") or "N/A")
        latency_columns[2].metric("Latency p95", latency.get("p95") or "N/A")
        costs = summary.get("cost_by_currency") or {}
        if costs:
            st.json(costs)
        else:
            st.info("Cost: N/A — no price table configured (NANOMEMORY_PRICE_TABLE_JSON).")
        if summary.get("by_model"):
            st.json(summary["by_model"])
        st.caption(
            "Estimated tokens and costs are local heuristics. They are never provider-billed "
            "values; only usage_source=exact comes from the provider."
        )

    st.divider()
    model = st.text_input("Filter by model", value="")
    provider = st.text_input("Filter by provider", value="")
    limit = st.slider("Max records", 10, 1000, 100)
    params: dict[str, Any] = {"limit": limit}
    if model.strip():
        params["model"] = model.strip()
    if provider.strip():
        params["provider"] = provider.strip()
    usage = safe_call("token_usage", lambda: client.usage(**params))
    if usage:
        st.caption(f"{usage['count']} records · corrupt lines {usage['corrupt_lines']}")
        rows = [
            {
                "timestamp": record["timestamp"],
                "request_id": record["request_id"],
                "provider": record["provider"],
                "model": record["model"],
                "usage_source": record["usage_source"],
                "prompt_tokens": record.get("prompt_tokens"),
                "completion_tokens": record.get("completion_tokens"),
                "total_tokens": record.get("total_tokens"),
                "estimated_tokens": record.get("estimated_tokens"),
                "latency_ms": record.get("latency_ms"),
                "cost": record.get("cost"),
                "currency": record.get("cost_currency"),
            }
            for record in usage["records"]
        ]
        render_table(rows)
