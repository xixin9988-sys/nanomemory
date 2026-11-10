"""Run both benchmark CLIs and write UTF-8 reports (spec §22.23).

Usage::

    python reports/run_benchmarks.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"


def commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or "unknown"


def run(report: str, args: list[str]) -> int:
    command = [sys.executable, "-m", *args]
    completed = subprocess.run(  # noqa: S603 - fixed argv
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=1800,
    )
    body = (
        f"command: python {' '.join(args)}\n"
        f"executed_at: {datetime.now(UTC).isoformat()}\n"
        f"commit: {commit()}\n"
        f"exit_code: {completed.returncode}\n"
        f"{'-' * 70}\n"
        f"{completed.stdout or ''}"
    )
    if (completed.stderr or "").strip():
        body += f"\n{'-' * 70}\nstderr:\n{completed.stderr}"
    (REPORTS / report).write_text(body, encoding="utf-8")
    print(f"{report}: exit {completed.returncode}")
    return completed.returncode


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    dataset = "benchmarks/datasets/memory_bench_long.json"
    failures = run(
        "phase7_token_benchmark.txt",
        ["benchmarks.token_benchmark", "--dataset", dataset, "--strategy", "all", "--runs", "1"],
    )
    failures += run(
        "phase7_task_benchmark.txt",
        ["benchmarks.task_benchmark", "--dataset", dataset, "--strategy", "all", "--runs", "1"],
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
