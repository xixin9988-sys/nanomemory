"""Store fixtures shared by the storage unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from nanomemory.core.backup import BackupService
from nanomemory.core.config import Settings
from nanomemory.core.episodic_memory import EpisodicMemoryStore
from nanomemory.core.locks import LockManager
from nanomemory.core.long_term_memory import LongTermMemoryStore
from nanomemory.core.memory_validator import MemoryValidator
from nanomemory.core.metrics import MetricsStore
from nanomemory.core.migrations import MigrationManager
from nanomemory.core.secret_scanner import SecretScanner
from nanomemory.core.working_memory import WorkingMemoryStore


@pytest.fixture()
def validator(settings: Settings) -> MemoryValidator:
    return MemoryValidator(settings, SecretScanner())


@pytest.fixture()
def working_store(
    settings: Settings, lock_manager: LockManager, validator: MemoryValidator
) -> WorkingMemoryStore:
    return WorkingMemoryStore(settings, lock_manager, validator)


@pytest.fixture()
def long_term_store(
    settings: Settings, lock_manager: LockManager, validator: MemoryValidator
) -> LongTermMemoryStore:
    store = LongTermMemoryStore(settings, lock_manager, validator)
    store.ensure_storage()
    return store


@pytest.fixture()
def episodic_store(
    settings: Settings, lock_manager: LockManager, validator: MemoryValidator
) -> EpisodicMemoryStore:
    store = EpisodicMemoryStore(settings, lock_manager, validator)
    store.ensure_storage()
    return store


@pytest.fixture()
def metrics_store(settings: Settings, lock_manager: LockManager) -> MetricsStore:
    store = MetricsStore(settings, lock_manager)
    store.ensure_storage()
    return store


@pytest.fixture()
def backup_service(settings: Settings, lock_manager: LockManager) -> BackupService:
    return BackupService(settings, lock_manager)


@pytest.fixture()
def migrations(settings: Settings, lock_manager: LockManager) -> MigrationManager:
    return MigrationManager(settings, lock_manager)


@pytest.fixture()
def prepared_session(working_store: WorkingMemoryStore) -> Iterator[str]:
    """A session with an initialised version-1 Working Memory."""
    from nanomemory.core.config import DEFAULT_SESSION_ID

    session_id = DEFAULT_SESSION_ID
    working_store.registry.ensure_default()
    working_store.ensure_session(session_id)
    yield session_id
