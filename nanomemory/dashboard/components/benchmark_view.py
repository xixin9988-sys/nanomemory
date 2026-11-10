"""Benchmark view: renders real result artifacts, or says "no experiment data yet".

Only files produced by ``benchmarks/`` are read (they are experiment artifacts, not storage).
When nothing has been measured the page says so explicitly — it never invents numbers
(spec §13 D, §22.17).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from nanomemory.dashboard.components.ui import render_table

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "benchmarks" / "results"


def _load_results() -> list[dict[str, Any]]:
    if not RESULTS_DIR.is_dir():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(RESULTS_DIR.glob("*_summary.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payload["_file"] = path.name
        summaries.append(payload)
    return summaries


def render() -> None:
    st.header("Benchmark")
    st.caption(
        "Results are produced by `python -m benchmarks.token_benchmark`. Mock results are not "
        "real API measurements and are labelled as such."
    )
    summaries = _load_results()
    if not summaries:
        st.info("尚無實驗數據 / No experiment data yet. Run the benchmark to populate this page.")
        with st.expander("How to run"):
            st.code(
                "python -m benchmarks.token_benchmark --strategy all\n"
                "python -m benchmarks.task_benchmark",
                language="bash",
            )
        return

    latest = summaries[-1]
    st.subheader(f"Latest run: {latest.get('_file')}")
    metadata = latest.get("metadata", {})
    columns = st.columns(4)
    columns[0].metric("Mode", metadata.get("mode", "unknown"))
    columns[1].metric("Cases", latest.get("case_count", 0))
    columns[2].metric("Dataset hash", str(metadata.get("dataset_sha256", ""))[:10])
    columns[3].metric("Seed", metadata.get("seed", "-"))
    if metadata.get("mode") == "mock":
        st.warning(
            "These are MOCK results: token counts are local estimates and do not represent "
            "real provider usage or cost."
        )
    st.caption(
        f"generated {metadata.get('generated_at', '?')} · commit "
        f"{metadata.get('commit', 'unknown')} · estimator {metadata.get('token_estimator', '?')}"
    )

    strategies = latest.get("strategies", {})
    rows = []
    for name, stats in strategies.items():
        rows.append(
            {
                "strategy": name,
                "input_tokens": stats.get("prompt_tokens"),
                "output_tokens": stats.get("completion_tokens"),
                "total_tokens": stats.get("total_tokens"),
                "cost": stats.get("cost"),
                "latency_ms": stats.get("latency_ms"),
                "task_success": stats.get("task_success"),
                "fact_recall": stats.get("fact_recall"),
                "saving_rate_pct": stats.get("saving_rate_pct"),
            }
        )
    render_table(rows)
    if "nanomemory" in strategies and "full_history" in strategies:
        saving = strategies["nanomemory"].get("saving_rate_pct")
        if saving is not None:
            st.metric("Token saving vs full history", f"{saving:.2f}%")
        st.caption(
            "Token savings are only meaningful alongside task success and fact recall; both "
            "are reported above and must not be cherry-picked."
        )
    with st.expander("Raw summaries"):
        st.json(summaries)
