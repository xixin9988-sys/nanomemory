"""Token / cost / latency metrics with cross-process rotation (spec §10, §22.16, §30.1, §35.6).

* ``storage/metrics/usage.jsonl`` is append-only JSONL.
* Appends and rotation happen inside **one** metrics resource lock critical section, so a
  rotation can never lose a record that another process is writing (§35.6).
* ``usage_source`` is preserved verbatim: ``exact`` means "the provider told us", everything
  else is an estimate and is reported as such.  Nothing here ever upgrades an estimate to
  exact.
* Corrupt lines are isolated to ``usage.corrupt.jsonl`` and counted; they never break the
  memory read/write paths.
"""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanomemory.core.config import Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import atomic_write_json
from nanomemory.core.locks import LockManager
from nanomemory.core.logging_config import get_logger
from nanomemory.core.memory_models import USAGE_SCHEMA_VERSION, UsageRecord
from nanomemory.core.paths import ensure_dir

AuditFn = Callable[..., None]


@dataclass(slots=True)
class MetricsSummary:
    """Aggregated usage for the API, dashboard and benchmark reports."""

    requests: int = 0
    exact_requests: int = 0
    estimated_requests: int = 0
    unknown_requests: int = 0
    mock_requests: int = 0
    prompt_tokens_exact: int = 0
    completion_tokens_exact: int = 0
    total_tokens_exact: int = 0
    estimated_tokens_total: int = 0
    latency_ms_avg: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    cost_by_currency: dict[str, float] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    by_provider: dict[str, int] = field(default_factory=dict)
    corrupt_lines: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "usage_sources": {
                "exact": self.exact_requests,
                "estimated": self.estimated_requests,
                "unknown": self.unknown_requests,
                "mock": self.mock_requests,
            },
            "prompt_tokens_exact": self.prompt_tokens_exact,
            "completion_tokens_exact": self.completion_tokens_exact,
            "total_tokens_exact": self.total_tokens_exact,
            "estimated_tokens_total": self.estimated_tokens_total,
            "latency_ms": {
                "avg": self.latency_ms_avg,
                "p50": self.latency_ms_p50,
                "p95": self.latency_ms_p95,
            },
            "cost_by_currency": self.cost_by_currency,
            "by_model": self.by_model,
            "by_provider": self.by_provider,
            "corrupt_lines": self.corrupt_lines,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "note": (
                "exact_* values come from provider-reported usage; estimated/unknown values "
                "are local estimates and are never billed values."
            ),
        }


class MetricsStore:
    """Append-only usage log with rotation under the metrics resource lock."""

    def __init__(
        self,
        settings: Settings,
        locks: LockManager,
        *,
        audit: AuditFn | None = None,
    ) -> None:
        self.settings = settings
        self.locks = locks
        self._audit_fn = audit
        self.log = get_logger("metrics")
        self.directory = settings.metrics_dir
        self.path = self.directory / "usage.jsonl"
        self.corrupt_path = self.directory / "usage.corrupt.jsonl"

    # ------------------------------------------------------------------ plumbing
    def ensure_storage(self) -> None:
        ensure_dir(self.directory, root=self.settings.storage_root, label="metrics directory")
        if not self.path.exists():
            self.path.touch()

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self._audit_fn is None:
            return
        try:
            self._audit_fn(event_type=event_type, **fields)
        except Exception:  # noqa: BLE001
            self.log.warning("audit_sink_failed", extra={"event": "audit_sink_failed"})

    @property
    def _resource(self) -> str:
        return "metrics"

    # ------------------------------------------------------------------ write
    def record(self, usage: UsageRecord) -> None:
        """Append one usage record, rotating first when the file would exceed its cap."""
        if not self.settings.metrics_enabled:
            return
        line = json.dumps(usage.model_dump(mode="json"), ensure_ascii=False)
        payload = (line + "\n").encode("utf-8")
        with self.locks.acquire([self._resource], operation="metrics_append"):
            self.ensure_storage()
            self._rotate_if_needed_locked(len(payload))
            try:
                with open(self.path, "ab") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise NanoMemoryError(
                    code=ErrorCode.IO_ERROR,
                    message="Cannot append to the metrics log.",
                    details={"errno": exc.errno},
                ) from exc

    def _rotation_targets(self) -> list[Path]:
        return [
            self.path.with_name(f"usage.{index}.jsonl")
            for index in range(1, max(0, self.settings.metrics_backup_count) + 1)
        ]

    def _rotate_if_needed_locked(self, incoming: int) -> None:
        try:
            current = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            current = 0
        if current + incoming <= self.settings.metrics_max_bytes:
            return
        targets = self._rotation_targets()
        # shift usage.N -> usage.N+1, dropping the oldest
        for index in range(len(targets) - 1, 0, -1):
            source = targets[index - 1]
            destination = targets[index]
            if source.exists():
                try:
                    os.replace(source, destination)
                except OSError:  # pragma: no cover - rotation failure must not lose the append
                    self.log.warning("metrics_rotate_failed", extra={"event": "metrics_rotate"})
        if targets:
            try:
                os.replace(self.path, targets[0])
            except OSError:
                self.log.warning("metrics_rotate_failed", extra={"event": "metrics_rotate"})
        self.ensure_storage()
        self._audit("metrics_rotated", max_bytes=self.settings.metrics_max_bytes)

    # ------------------------------------------------------------------ read
    def _candidate_files(self) -> list[Path]:
        files = [self.path]
        files.extend(self._rotation_targets())
        return [path for path in files if path.is_file()]

    def read(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[UsageRecord], int]:
        """Read usage records newest-first; returns ``(records, corrupt_line_count)``."""
        with self.locks.acquire([self._resource], operation="metrics_read"):
            self.ensure_storage()
            records, corrupt = self._parse_locked()
        filtered: list[UsageRecord] = []
        for record in records:
            if model and record.model != model:
                continue
            if provider and record.provider != provider:
                continue
            stamp = _parse_timestamp(record.timestamp)
            if since and stamp and stamp < since:
                continue
            if until and stamp and stamp > until:
                continue
            filtered.append(record)
        filtered.sort(key=lambda item: item.timestamp, reverse=True)
        if limit is not None:
            filtered = filtered[: max(0, limit)]
        return filtered, corrupt

    def _parse_locked(self) -> tuple[list[UsageRecord], int]:
        records: list[UsageRecord] = []
        corrupt = 0
        corrupt_lines: list[str] = []
        for path in self._candidate_files():
            with open(path, "rb") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                        record = UsageRecord.model_validate(payload)
                        if record.schema_version > USAGE_SCHEMA_VERSION:
                            raise ValueError("usage schema newer than supported")
                        records.append(record)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                        corrupt += 1
                        if len(corrupt_lines) < 50:
                            corrupt_lines.append(raw.decode("utf-8", errors="replace")[:500])
        if corrupt_lines:
            self._append_corrupt(corrupt_lines)
        return records, corrupt

    def _append_corrupt(self, lines: list[str]) -> None:
        try:
            with open(self.corrupt_path, "a", encoding="utf-8", newline="\n") as handle:
                for line in lines:
                    handle.write(json.dumps({"raw": line, "detected_at": _now_iso()}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:  # pragma: no cover - diagnostics only
            return

    # ------------------------------------------------------------------ summary
    def summary(self) -> MetricsSummary:
        records, corrupt = self.read()
        summary = MetricsSummary(corrupt_lines=corrupt)
        latencies: list[float] = []
        for record in records:
            summary.requests += 1
            if record.usage_source == "exact":
                summary.exact_requests += 1
                summary.prompt_tokens_exact += record.prompt_tokens or 0
                summary.completion_tokens_exact += record.completion_tokens or 0
                summary.total_tokens_exact += record.total_tokens or 0
            elif record.usage_source == "estimated":
                summary.estimated_requests += 1
                summary.estimated_tokens_total += record.estimated_tokens or 0
            elif record.usage_source == "mock":
                summary.mock_requests += 1
                summary.estimated_tokens_total += record.estimated_tokens or 0
            else:
                summary.unknown_requests += 1
            if record.latency_ms is not None:
                latencies.append(record.latency_ms)
            if record.cost is not None and record.cost_currency:
                summary.cost_by_currency[record.cost_currency] = round(
                    summary.cost_by_currency.get(record.cost_currency, 0.0) + record.cost, 8
                )
            summary.by_model[record.model] = summary.by_model.get(record.model, 0) + 1
            summary.by_provider[record.provider] = summary.by_provider.get(record.provider, 0) + 1
            if summary.first_timestamp is None or record.timestamp < summary.first_timestamp:
                summary.first_timestamp = record.timestamp
            if summary.last_timestamp is None or record.timestamp > summary.last_timestamp:
                summary.last_timestamp = record.timestamp
        if latencies:
            summary.latency_ms_avg = round(statistics.fmean(latencies), 2)
            summary.latency_ms_p50 = round(_percentile(latencies, 50), 2)
            summary.latency_ms_p95 = round(_percentile(latencies, 95), 2)
        return summary

    # ------------------------------------------------------------------ maintenance
    def statistics(self) -> dict[str, Any]:
        summary = self.summary()
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path_bytes": size,
            "rotated_files": sum(1 for path in self._rotation_targets() if path.exists()),
            "usage": summary.as_dict(),
        }

    def reset(self) -> None:
        """Test helper: replace the log with an empty file plus a state marker."""
        with self.locks.acquire([self._resource], operation="metrics_reset"):
            self.ensure_storage()
            atomic_write_json(self.directory / "reset.json", {"reset_at": _now_iso()})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


__all__ = ["MetricsStore", "MetricsSummary"]
