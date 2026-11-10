"""Session registry (spec §24.3, §29.1).

v1 is a **local single-user MVP**: sessions isolate Working Memory on disk, not users.  A
session id is always server-generated and validated here; the model, the memory body and the
request body can never choose or overwrite it.

Layout: ``storage/working/<session_id>/`` with ``.session.json`` holding creation metadata.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from nanomemory.core.config import SESSION_ID_PATTERN, Settings
from nanomemory.core.errors import ErrorCode, NanoMemoryError
from nanomemory.core.fsio import atomic_write_json, read_json
from nanomemory.core.memory_models import SessionMeta, SessionState, utc_now_iso
from nanomemory.core.paths import ensure_dir, safe_component


class SessionRegistry:
    """Creates, validates and enumerates Working Memory sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.working_dir

    # ------------------------------------------------------------------ validation
    @staticmethod
    def validate_id(session_id: str) -> str:
        """Validate a session id from a header or path; raise a typed error otherwise."""
        if not session_id:
            raise NanoMemoryError(
                code=ErrorCode.SESSION_ID_REQUIRED,
                message="A session id is required for this request.",
            )
        if not SESSION_ID_PATTERN.match(session_id):
            raise NanoMemoryError(
                code=ErrorCode.SESSION_ID_INVALID,
                message="Session id must be a UUID4 string.",
            )
        safe_component(session_id, label="session_id")
        return session_id.lower()

    # ------------------------------------------------------------------ paths
    def session_dir(self, session_id: str, *, create: bool = False) -> Path:
        validated = self.validate_id(session_id)
        path = self.root / validated
        if create:
            return ensure_dir(path, root=self.settings.storage_root, label="session directory")
        return path

    def exists(self, session_id: str) -> bool:
        try:
            validated = self.validate_id(session_id)
        except NanoMemoryError:
            return False
        return (self.root / validated).is_dir()

    def require(self, session_id: str) -> Path:
        """Return the session directory or raise ``session_not_found``."""
        path = self.session_dir(session_id)
        if not path.is_dir():
            raise NanoMemoryError(
                code=ErrorCode.SESSION_NOT_FOUND,
                message="Session does not exist.",
            )
        return path

    # ------------------------------------------------------------------ lifecycle
    def create(self) -> SessionMeta:
        """Create a new session with a server-generated UUID4 id (spec §29.1)."""
        for _ in range(5):
            session_id = str(uuid.uuid4())
            if not self.exists(session_id):
                break
        else:  # pragma: no cover - astronomically unlikely
            raise NanoMemoryError(
                code=ErrorCode.INTERNAL_ERROR,
                message="Could not allocate a unique session id.",
            )
        directory = self.session_dir(session_id, create=True)
        meta = SessionMeta(session_id=session_id, created_at=utc_now_iso())
        atomic_write_json(directory / ".session.json", meta.model_dump(mode="json"))
        return meta

    def ensure_default(self) -> SessionMeta:
        """Create (once) the fixed single-session default session."""
        session_id = self.settings.default_session_id
        directory = self.session_dir(session_id, create=True)
        meta_path = directory / ".session.json"
        if meta_path.is_file():
            try:
                return SessionMeta.model_validate(read_json(meta_path))
            except NanoMemoryError:
                # Corrupt registry entry: rewrite metadata but never touch memory files.
                pass
        meta = SessionMeta(session_id=session_id, created_at=utc_now_iso())
        atomic_write_json(meta_path, meta.model_dump(mode="json"))
        return meta

    def read_meta(self, session_id: str) -> SessionMeta:
        directory = self.require(session_id)
        payload = read_json(directory / ".session.json")
        return SessionMeta.model_validate(payload)

    def touch(self, session_id: str) -> None:
        """Record last-seen time without failing the request when it cannot be written."""
        directory = self.require(session_id)
        meta_path = directory / ".session.json"
        try:
            meta = SessionMeta.model_validate(read_json(meta_path))
            meta.last_seen_at = utc_now_iso()
            meta.status = SessionState.ACTIVE
            atomic_write_json(meta_path, meta.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - best effort observability only
            return

    def list_sessions(self) -> list[str]:
        """List existing session ids (directories only, no symlinks followed)."""
        if not self.root.is_dir():
            return []
        sessions: list[str] = []
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            if SESSION_ID_PATTERN.match(entry.name):
                sessions.append(entry.name)
        return sorted(sessions)


__all__ = ["SessionRegistry"]
