"""``nanomemory`` CLI — init, start, status, remember, recall, explain, lifecycle, doctor.

The CLI is a thin wrapper around :class:`nanomemory.sdk.Memory` so the same rules apply to the
SDK, the REST API and the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from nanomemory import __version__
from nanomemory.sdk import Memory, _run


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            print(f"{key}: {value}")


def _memory(args: argparse.Namespace) -> Memory:
    return Memory(args.path, user_id=args.user)


def cmd_init(args: argparse.Namespace) -> int:
    memory = _memory(args)
    status = _run(memory.status())
    print(f"NanoMemory {__version__} initialised ({memory.mode} mode)")
    print(f"storage: {status['storage_root']}")
    print(f"session: {status['session_id']} | records: {status['records']}")
    print("next: nanomemory start   (or: from nanomemory import Memory)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    memory = _memory(args)
    _emit(_run(memory.status()), as_json=args.json)
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    memory = _memory(args)
    report = _run(
        memory.remember(
            args.text,
            memory_type=args.type,
            source=args.source,
            confirm=True if args.confirm else None,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"extracted {report['extracted']} | created {report['created']} | "
        f"pending {report['pending']} | conflicts {report['conflicts']} | "
        f"superseded {report['superseded']}"
    )
    for item in report["items"]:
        print(
            f"  - {item.get('id', '(rejected)'):<24} {item.get('status', ''):<20} {item['content'][:80]}"
        )
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    memory = _memory(args)
    result = _run(memory.recall(args.query, limit=args.limit, memory_type=args.type))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(
        f"intent {result['intent']} | items {len(result['items'])} | "
        f"tokens {result['estimated_tokens']}/{result['budget']}"
    )
    if result["insufficient_context"]:
        print(f"insufficient context: {result['fallback_reason']}")
    for item in result["items"]:
        print(f"  [{item['score']:.3f}] {item['kind']:<10} {item['content'][:100]}")
        print(f"          reason: {item['reason']}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    memory = _memory(args)
    data = _run(memory.explain(args.memory_id))
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_lifecycle(args: argparse.Namespace) -> int:
    memory = _memory(args)
    report = _run(memory.lifecycle(dry_run=not args.apply))
    _emit(report, as_json=args.json)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    memory = _memory(args)
    manager = memory.manager
    report = manager.health.check()
    payload = {
        "version": __version__,
        "mode": memory.mode,
        "provider": memory.settings.provider,
        "storage_root": str(memory.settings.storage_root),
        "status": report.status,
        "degraded": report.degraded,
        "reasons": report.reasons,
        "checks": report.checks,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.status == "ok" else 1


def cmd_start(args: argparse.Namespace) -> int:
    if args.path is not None:
        os.environ["NANOMEMORY_STORAGE_ROOT"] = str(Path(args.path).resolve())
    import uvicorn

    print(f"NanoMemory {__version__} API on http://{args.host}:{args.port}")
    uvicorn.run("nanomemory.api.main:app", host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanomemory",
        description="NanoMemory — persistent memory for any AI application.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("--path", default=None, help="storage directory (default: ./storage)")
        p.add_argument("--user", default="local", help="user id (default: local)")

    init = sub.add_parser("init", help="initialise storage and print the location")
    shared(init)
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="show storage and record counts")
    shared(status)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    remember = sub.add_parser("remember", help="extract and store memories from text")
    shared(remember)
    remember.add_argument("text")
    remember.add_argument("--type", default=None, help="force a memory type")
    remember.add_argument("--source", default="cli", help="source label")
    remember.add_argument("--confirm", action="store_true", help="confirm model-derived writes")
    remember.add_argument("--json", action="store_true")
    remember.set_defaults(func=cmd_remember)

    recall = sub.add_parser("recall", help="retrieve memories for a query")
    shared(recall)
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=5)
    recall.add_argument("--type", default=None)
    recall.add_argument("--json", action="store_true")
    recall.set_defaults(func=cmd_recall)

    explain = sub.add_parser("explain", help="explain provenance and history of a memory")
    shared(explain)
    explain.add_argument("memory_id")
    explain.set_defaults(func=cmd_explain)

    lifecycle = sub.add_parser("lifecycle", help="evaluate retention (dry-run by default)")
    shared(lifecycle)
    lifecycle.add_argument("--apply", action="store_true", help="archive eligible memories")
    lifecycle.add_argument("--json", action="store_true")
    lifecycle.set_defaults(func=cmd_lifecycle)

    doctor = sub.add_parser("doctor", help="run health checks")
    shared(doctor)
    doctor.set_defaults(func=cmd_doctor)

    start = sub.add_parser("start", help="start the REST API")
    start.add_argument("--path", default=None)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)
    start.set_defaults(func=cmd_start)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as error:  # pragma: no cover - CLI boundary
        print(f"error: {error}", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]
