"""Health probe for Docker / CI.

Exit code 0 when ``GET /health`` returns HTTP 200 and ``degraded`` is false, 1 otherwise.
Uses only the standard library so it works in a slim image without curl (spec §22.22).
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the NanoMemory health endpoint")
    parser.add_argument("--url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Treat a degraded but reachable service as healthy (not recommended for Docker)",
    )
    args = parser.parse_args(argv)
    request = urllib.request.Request(args.url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:  # noqa: S310 - local URL
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"health probe failed: {type(exc).__name__}")
        return 1
    degraded = bool(payload.get("degraded"))
    maintenance = bool(payload.get("maintenance_mode"))
    print(
        json.dumps(
            {
                "status_code": status,
                "service_status": payload.get("status"),
                "degraded": degraded,
                "maintenance_mode": maintenance,
                "reasons": payload.get("reasons", []),
            }
        )
    )
    if status != 200:
        return 1
    if maintenance:
        return 1
    if degraded and not args.allow_degraded:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
