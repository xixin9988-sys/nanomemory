"""Convenience facade: ``from nanomemory import NanoMemory``.

Thin wrapper around :class:`nanomemory.core.memory_manager.MemoryManager` kept for ergonomic
embedding in scripts and notebooks.
"""

from __future__ import annotations

from nanomemory.core.config import Settings, get_settings
from nanomemory.core.memory_manager import MemoryManager


class NanoMemory(MemoryManager):
    """Default-configured :class:`MemoryManager`."""

    def __init__(self, settings: Settings | None = None, **kwargs: object) -> None:
        super().__init__(settings or get_settings(), **kwargs)  # type: ignore[arg-type]


__all__ = ["NanoMemory"]
