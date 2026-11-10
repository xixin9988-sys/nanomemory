"""Shared Streamlit UI helpers: admin token handling, health banner, safe API calls.

The admin token rules (spec §30.6, §35.5):

* entered through a ``password`` input,
* kept only in ``st.session_state`` for the short lifetime of the operation,
* never written to the URL, a file, local storage, a log or another widget,
* **deleted** and the widget key rotated after every use, so the next destructive action
  always asks again — session_state is not treated as authentication.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import streamlit as st

from nanomemory.dashboard.client import ApiClient, ApiError


@lru_cache(maxsize=1)
def arrow_available() -> bool:
    """True when ``pyarrow`` can be imported (Streamlit's dataframe backend).

    On locked-down Windows hosts the pyarrow native library may be blocked by an application
    control policy.  Rather than failing the page, tables fall back to a Markdown rendering.
    """
    try:
        import pyarrow  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "unavailable"
        return False
    return True


def render_table(rows: list[dict[str, Any]], *, use_container_width: bool = True) -> None:
    """Render a table, using Markdown when the Arrow backend is unavailable."""
    if not rows:
        st.info("No rows to display.")
        return
    if arrow_available():  # pragma: no cover - depends on the host
        st.dataframe(rows, use_container_width=use_container_width)
        return
    headers = list(rows[0].keys())
    lines = [
        "| " + " | ".join(str(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [str(row.get(header, "")).replace("|", "\\|") for header in headers]
        lines.append("| " + " | ".join(cells) + " |")
    st.markdown("\n".join(lines))


def api_base_url(default: str = "http://127.0.0.1:8000") -> str:
    return st.session_state.get("api_base_url") or default


def get_client() -> ApiClient:
    return ApiClient(api_base_url(), session_id=st.session_state.get("session_id"))


def _token_key(operation: str) -> str:
    nonce = st.session_state.setdefault(f"nonce_{operation}", uuid.uuid4().hex)
    return f"admin_token_{operation}_{nonce}"


def admin_token_input(operation: str) -> str | None:
    """Render a one-shot password field for ``operation``."""
    st.caption(
        "Admin token is required for destructive operations. It is used once and not stored."
    )
    value = st.text_input(
        "Admin token",
        type="password",
        key=_token_key(operation),
        help="Value of NANOMEMORY_ADMIN_TOKEN on the API host.",
    )
    return value.strip() or None if value else None


def consume_admin_token(operation: str) -> None:
    """Drop the token value and rotate the widget key so the next action asks again."""
    prefix = "admin_token_"
    for key in [key for key in st.session_state if key.startswith(prefix)]:
        del st.session_state[key]
    st.session_state[f"nonce_{operation}"] = uuid.uuid4().hex


def show_error(error: ApiError) -> None:
    st.error(f"{error.message} (code: {error.code})")
    if error.details:
        with st.expander("Error details"):
            st.json(error.details)
    if error.request_id:
        st.caption(f"request_id: {error.request_id}")


def safe_call(operation: str, call: Callable[[], Any]) -> Any:
    """Run an API call, rendering a readable error instead of raising."""
    try:
        return call()
    except ApiError as error:
        st.session_state[f"last_error_{operation}"] = {
            "code": error.code,
            "message": error.message,
            "request_id": error.request_id,
        }
        show_error(error)
        return None


def health_banner(client: ApiClient) -> dict[str, Any] | None:
    try:
        health = client.health()
    except ApiError as error:
        st.error(
            "Cannot reach the NanoMemory API. Start it with "
            "`uvicorn nanomemory.api.main:app` and check the URL in the sidebar."
        )
        st.caption(f"reason: {error.code}")
        return None
    if health.get("degraded"):
        st.warning("Service is degraded: " + ", ".join(health.get("reasons") or ["unknown reason"]))
    if health.get("maintenance_mode"):
        st.error("Service is in maintenance mode; memory writes are blocked.")
    return health


def render_sidebar(client: ApiClient) -> dict[str, Any] | None:
    """Connection, session and health controls shared by every page."""
    with st.sidebar:
        st.header("NanoMemory")
        st.text_input(
            "API base URL",
            key="api_base_url",
            value=st.session_state.get("api_base_url", "http://127.0.0.1:8000"),
        )
        client = get_client()
        health = health_banner(client)
        if health:
            st.caption(
                f"provider: {health.get('provider')} ({health.get('provider_mode')}) · "
                f"model: {health.get('model')}"
            )
            st.caption(
                f"memory budget: {health.get('memory_token_limit')} / "
                f"context budget: {health.get('context_token_budget')} tokens"
            )
            if st.button("Refresh health"):
                st.rerun()
        session_mode = (health or {}).get("session_mode", "single")
        if session_mode == "multi":
            st.divider()
            st.subheader("Session")
            if st.button("Create new session"):
                created = safe_call("session", client.create_session)
                if created:
                    st.session_state["session_id"] = created["session_id"]
                    st.rerun()
            st.text_input(
                "Session id",
                key="session_id",
                help="Send the X-NanoMemory-Session-ID header to every API call.",
            )
        else:
            st.caption("session mode: single (default session)")
        st.divider()
        st.caption(
            "Dashboard reads and writes only through the REST API. Memory content is treated "
            "as untrusted data."
        )
    return health


__all__ = [
    "admin_token_input",
    "api_base_url",
    "consume_admin_token",
    "get_client",
    "health_banner",
    "render_sidebar",
    "safe_call",
    "show_error",
]
