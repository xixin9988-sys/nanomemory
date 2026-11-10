"""Live API smoke test (spec §22.23).

Starts a real uvicorn server on an ephemeral port with a throw-away storage root, exercises
the critical endpoints (including admin auth and a version conflict), prints a report and
shuts the server down.  Used to produce ``reports/phase5_api_smoke.txt``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORAGE = PROJECT_ROOT / "reports" / ".smoke-storage"
ADMIN_TOKEN = "smoke-admin-token-not-a-real-secret"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.2)
    return False


def main() -> int:
    if STORAGE.exists():
        shutil.rmtree(STORAGE, ignore_errors=True)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    environment.update(
        {
            "NANOMEMORY_STORAGE_ROOT": str(STORAGE),
            "NANOMEMORY_PROVIDER": "mock",
            "NANOMEMORY_ADMIN_TOKEN": ADMIN_TOKEN,
            "NANOMEMORY_LOG_LEVEL": "WARNING",
            "NANOMEMORY_LOG_FORMAT": "json",
            "NANOMEMORY_RATE_LIMIT_ENABLED": "true",
            "NANOMEMORY_RATE_LIMIT_PER_MINUTE": "1000",
        }
    )
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "nanomemory.api.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
        "--log-level",
        "warning",
    ]
    print(f"command: {' '.join(command)}")
    print(f"port: {port}")
    server = subprocess.Popen(  # noqa: S603 - fixed argv
        command,
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    checks: list[tuple[str, bool, str]] = []
    try:
        if not wait_ready(base_url):
            print("server did not become ready")
            return 1
        headers = {"Content-Type": "application/json"}
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            health = client.get("/health")
            checks.append(("GET /health", health.status_code == 200, f"status={health.json().get('status')}"))

            working = client.get("/api/v1/memory/working")
            checks.append(
                (
                    "GET /api/v1/memory/working",
                    working.status_code == 200 and working.json()["memory_version"] == 1,
                    f"version={working.json().get('memory_version')}",
                )
            )

            updated = client.put(
                "/api/v1/memory/working",
                headers=headers,
                json={"expected_version": 1, "current_goal": "API smoke goal", "status": "in_progress"},
            )
            checks.append(
                (
                    "PUT /api/v1/memory/working",
                    updated.status_code == 200 and updated.json()["memory_version"] == 2,
                    f"status={updated.status_code}",
                )
            )

            conflict = client.put(
                "/api/v1/memory/working",
                headers=headers,
                json={"expected_version": 1, "current_goal": "stale"},
            )
            checks.append(
                (
                    "PUT stale expected_version",
                    conflict.status_code == 409 and conflict.json()["code"] == "version_conflict",
                    f"status={conflict.status_code}",
                )
            )

            created = client.post(
                "/api/v1/memory/long-term",
                headers=headers,
                json={"content": "Smoke test fact about the API.", "category": "smoke", "tags": ["smoke"]},
            )
            checks.append(
                ("POST /api/v1/memory/long-term", created.status_code == 200, f"status={created.status_code}")
            )

            agent = client.post(
                "/api/v1/agent/run", headers=headers, json={"task": "smoke test the agent loop"}
            )
            agent_payload = agent.json()
            checks.append(
                (
                    "POST /api/v1/agent/run",
                    agent.status_code == 200 and agent_payload.get("status") in {"completed", "in_progress"},
                    f"status={agent.status_code} agent_status={agent_payload.get('status')}",
                )
            )

            summary = client.get("/api/v1/metrics/summary")
            checks.append(
                (
                    "GET /api/v1/metrics/summary",
                    summary.status_code == 200,
                    f"requests={summary.json().get('requests')}",
                )
            )

            denied = client.post("/api/v1/memory/backup", headers=headers, json={"label": "smoke"})
            checks.append(
                (
                    "POST backup without admin token",
                    denied.status_code == 401,
                    f"status={denied.status_code} code={denied.json().get('code')}",
                )
            )

            backup = client.post(
                "/api/v1/memory/backup",
                headers={**headers, "Authorization": f"Bearer {ADMIN_TOKEN}"},
                json={"label": "smoke"},
            )
            checks.append(
                (
                    "POST backup with admin token",
                    backup.status_code == 200,
                    f"status={backup.status_code}",
                )
            )

            if backup.status_code == 200:
                backup_id = backup.json()["backup_id"]
                verified = client.post(
                    f"/api/v1/memory/backups/{backup_id}/verify",
                    headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )
                checks.append(
                    (
                        "POST backup verify",
                        verified.status_code == 200 and verified.json().get("valid") is True,
                        f"valid={verified.json().get('valid')}",
                    )
                )
                bad_restore = client.post(
                    "/api/v1/memory/restore",
                    headers={**headers, "Authorization": f"Bearer {ADMIN_TOKEN}"},
                    json={"backup_id": backup_id, "confirmation": "wrong", "force": False},
                )
                checks.append(
                    (
                        "POST restore with wrong confirmation",
                        bad_restore.status_code == 400,
                        f"status={bad_restore.status_code}",
                    )
                )

            openapi = client.get("/openapi.json")
            checks.append(
                ("GET /openapi.json", openapi.status_code == 200, f"status={openapi.status_code}")
            )

            health_probe = subprocess.run(  # noqa: S603
                [sys.executable, str(PROJECT_ROOT / "scripts" / "health_probe.py"), "--url", f"{base_url}/health"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            checks.append(
                (
                    "scripts/health_probe.py",
                    health_probe.returncode == 0,
                    (health_probe.stdout or "").strip().splitlines()[-1] if health_probe.stdout else "",
                )
            )
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        output = server.stdout.read() if server.stdout else ""
        shutil.rmtree(STORAGE, ignore_errors=True)

    print()
    print(f"{'check':<44}{'result':<8}detail")
    print("-" * 100)
    failures = 0
    for name, ok, detail in checks:
        if not ok:
            failures += 1
        print(f"{name:<44}{'PASS' if ok else 'FAIL':<8}{detail}")
    print()
    print(f"checks: {len(checks)} · failures: {failures}")
    if failures:
        print("server log tail:")
        print("\n".join(output.strip().splitlines()[-20:]))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
