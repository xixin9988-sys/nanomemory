"""Turn a pytest JUnit report into GitHub Actions annotations.

CI logs are not readable without repository admin rights, but workflow annotations are.  When a
test fails, this script prints one ::error:: line per failure so the failing test names and the
first line of the assertion message are visible from the checks API.

    python scripts/ci_junit_annotations.py junit.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    path = Path((argv or sys.argv[1:] or ["junit.xml"])[0])
    if not path.is_file():
        print(f"::warning::JUnit report not found at {path}")
        return 0
    tree = ET.parse(path)
    failures = 0
    for case in tree.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        failures += 1
        if failures > 25:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        message = (problem.get("message") or "").strip().splitlines()
        first = message[0][:300] if message else "failed"
        file = case.get("file") or ""
        line = case.get("line") or "1"
        print(f"::error file={file},line={line},title=pytest::{name} - {first}")
    print(f"::notice::pytest reported {failures} failing test(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
