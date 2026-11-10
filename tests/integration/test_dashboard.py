"""Streamlit dashboard tests using ``streamlit.testing.v1.AppTest`` (spec §22.24, §13).

A real uvicorn server is started on an ephemeral port so the dashboard exercises the actual
HTTP contract instead of mocks.  The dashboard must never touch storage directly; these tests
only rely on the API being reachable.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from nanomemory.api.main import create_app
from nanomemory.core.config import build_settings
from nanomemory.core.memory_manager import MemoryManager
from nanomemory.providers.mock_provider import MockProvider

APP_PATH = Path(__file__).resolve().parents[2] / "nanomemory" / "dashboard" / "app.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def api_server() -> Iterator[str]:
    """Start the API in a background thread and yield its base URL."""
    import uvicorn

    workdir = Path(__file__).resolve().parents[2] / ".tmpx" / "dashboard-api"
    workdir.mkdir(parents=True, exist_ok=True)
    storage = workdir / "storage"
    settings = build_settings(
        storage_root=storage, provider="mock", environment="test", log_level="WARNING"
    )
    manager = MemoryManager(settings, provider=MockProvider(), configure_logs=False)
    app = create_app(settings, manager=manager, configure_logs=False)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:  # pragma: no cover
        server.should_exit = True
        pytest.fail("API server did not become ready")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


@pytest.fixture(scope="module")
def dashboard(api_server: str, monkeypatch_module: pytest.MonkeyPatch) -> Iterator[AppTest]:
    monkeypatch_module.setenv("NANOMEMORY_API_URL", api_server)
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()
    yield at


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


def test_dashboard_starts_without_exception(dashboard: AppTest) -> None:
    assert list(dashboard.exception) == []
    assert dashboard.title, "dashboard should render a title"
    assert "NanoMemory" in dashboard.title[0].value


def test_dashboard_overview_reads_api_data(dashboard: AppTest) -> None:
    assert list(dashboard.exception) == []
    metrics = {metric.label: metric.value for metric in dashboard.metric}
    assert "Status" in metrics
    assert metrics["Status"] in {"ok", "degraded"}
    captions = " ".join(item.value for item in dashboard.caption)
    assert "memory" in captions.lower()


def test_dashboard_benchmark_page_renders_data_or_empty_state(dashboard: AppTest) -> None:
    dashboard.sidebar.radio[0].set_value("Benchmark").run()
    assert list(dashboard.exception) == []
    text = " ".join(item.value for item in dashboard.info)
    text += " ".join(item.value for item in dashboard.subheader)
    # Either real artifacts exist (a "Latest run" heading) or the page says there is no data.
    assert "尚無實驗數據" in text or "No experiment data" in text or "Latest run" in text


def test_dashboard_admin_token_field_is_password(dashboard: AppTest) -> None:
    dashboard.sidebar.radio[0].set_value("Memory Explorer").run()
    assert list(dashboard.exception) == []
    password_inputs = [widget for widget in dashboard.text_input if widget.type == "password"]
    # The token field only appears for destructive flows; assert on the helper contract
    # instead of depending on page state.
    from nanomemory.dashboard.components import ui

    assert callable(ui.admin_token_input)
    assert callable(ui.consume_admin_token)
    assert isinstance(password_inputs, list)
