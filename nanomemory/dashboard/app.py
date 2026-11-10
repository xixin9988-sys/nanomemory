"""NanoMemory Streamlit dashboard entry point.

Run with::

    streamlit run nanomemory/dashboard/app.py

The dashboard talks to the FastAPI service exclusively (``NANOMEMORY_API_URL``, default
``http://127.0.0.1:8000``); it never opens a storage file for writing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# Allow `streamlit run nanomemory/dashboard/app.py` from a source checkout before
# `pip install -e .` has been executed.
if __package__ in (None, ""):  # pragma: no cover - depends on how Streamlit launches us
    _root = Path(__file__).resolve().parents[2]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from nanomemory.dashboard.client import ApiClient, ApiError  # noqa: E402
from nanomemory.dashboard.components import (  # noqa: E402
    agent_console,
    benchmark_view,
    memory_explorer,
    overview,
    token_analytics,
    ui,
)

PAGES = {
    "Overview": "overview",
    "Memory Explorer": "memory",
    "Token Analytics": "tokens",
    "Benchmark": "benchmark",
    "Agent Console": "agent",
}


def main() -> None:
    st.set_page_config(page_title="NanoMemory", page_icon="🧠", layout="wide")
    st.session_state.setdefault(
        "api_base_url", os.environ.get("NANOMEMORY_API_URL", "http://127.0.0.1:8000")
    )

    client: ApiClient = ui.get_client()
    health = ui.render_sidebar(client)
    page = st.sidebar.radio("Page", list(PAGES), index=0)
    st.title("NanoMemory — Local Memory Engine")
    st.caption(
        "Local single-user MVP · memory content is untrusted reference data, never instructions"
    )

    try:
        if page == "Overview":
            overview.render(client, health)
        elif page == "Memory Explorer":
            memory_explorer.render(client)
        elif page == "Token Analytics":
            token_analytics.render(client)
        elif page == "Benchmark":
            benchmark_view.render()
        elif page == "Agent Console":
            agent_console.render(client)
    except ApiError as error:  # pragma: no cover - defensive top-level guard
        ui.show_error(error)


if __name__ == "__main__":
    main()
