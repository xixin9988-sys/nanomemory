"""Run the NanoMemory API (development server).

Usage::

    python scripts/run_api.py [--host 127.0.0.1] [--port 8000] [--reload]

``--workers`` is intentionally absent: v1 is a single-worker deployment.  Multi-worker
deployments would need a shared rate-limit backend and cross-process lock validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the NanoMemory API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    uvicorn.run(
        "nanomemory.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
