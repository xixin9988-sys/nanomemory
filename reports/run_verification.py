"""Reproduce the verification artifacts in ``reports/``.

Usage::

    python reports/run_verification.py

Each step records the exact command, timestamp, exit code and output in a UTF-8 text file.
JUnit XML and coverage XML are produced by pytest itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
PYTHON = sys.executable
_RUFF_EXE = Path(sys.executable).with_name("ruff.exe")
RUFF = [_RUFF_EXE.as_posix()] if _RUFF_EXE.exists() else [PYTHON, "-m", "ruff"]


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def run(name: str, command: list[str], *, timeout: int = 1800) -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    target = REPORTS / name
    started = _stamp()
    completed = subprocess.run(  # noqa: S603 - fixed argv built here
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    output = (
        f"command: {' '.join(command)}\n"
        f"executed_at: {started}\n"
        f"commit: {_commit()}\n"
        f"exit_code: {completed.returncode}\n"
        f"{'-' * 70}\n"
        f"{completed.stdout or ""}"
    )
    if (completed.stderr or "").strip():
        output += f"\n{'-' * 70}\nstderr:\n{completed.stderr}"
    target.write_text(output, encoding="utf-8")
    print(f"{name}: exit {completed.returncode}")
    return completed.returncode


def main() -> int:
    failures = 0
    failures += run(
        "phase9_pytest.txt",
        [
            PYTHON,
            "-m",
            "pytest",
            "-q",
            "--junitxml=reports/phase9_junit.xml",
            "--cov=nanomemory",
            "--cov-report=term-missing",
            "--cov-report=xml:reports/phase9_coverage.xml",
        ],
    )
    failures += run("phase9_ruff_check.txt", [*RUFF, "check", "."])
    failures += run("phase9_ruff_format.txt", [*RUFF, "format", "--check", "."])
    failures += run(
        "phase1_import_smoke.txt",
        [
            PYTHON,
            "-c",
            (
                "import importlib, nanomemory\n"
                "mods = ['nanomemory', 'nanomemory.core', 'nanomemory.api', "
                "'nanomemory.providers', 'nanomemory.dashboard']\n"
                "for name in mods:\n"
                "    importlib.import_module(name)\n"
                "print('import smoke ok', nanomemory.__version__)\n"
            ),
        ],
    )
    failures += run(
        "phase1_package_layout.txt",
        [
            PYTHON,
            "-c",
            (
                "import sys\n"
                "from setuptools import find_packages\n"
                "packages = sorted(find_packages(include=['nanomemory*']))\n"
                "print('discovered packages:', packages)\n"
                "assert 'nanomemory' in packages\n"
                "assert 'nanomemory.core' in packages\n"
                "assert 'nanomemory.api' in packages\n"
                "print('package discovery ok')\n"
            ),
        ],
    )
    # Lightweight README command checks that need no network or long-running server.
    failures += run("phase8_cli_help.txt", [PYTHON, "scripts/init_storage.py", "--help"])
    failures += run("phase8_health_probe_help.txt", [PYTHON, "scripts/health_probe.py", "--help"])
    failures += run(
        "phase8_sensitive_scan.txt",
        [
            PYTHON,
            "reports/sensitive_scan.py",
        ],
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
