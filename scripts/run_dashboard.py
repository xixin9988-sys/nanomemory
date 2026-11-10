"""Launch the Streamlit dashboard against a running NanoMemory API.

Usage::

    python scripts/run_dashboard.py [--api-url http://127.0.0.1:8000] [--port 8501]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the NanoMemory dashboard")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args(argv)

    environment = dict(os.environ)
    environment["NANOMEMORY_API_URL"] = args.api_url
    script = PROJECT_ROOT / "nanomemory" / "dashboard" / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
    ]
    return subprocess.call(command, cwd=str(PROJECT_ROOT), env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
