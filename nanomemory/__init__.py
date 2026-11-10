"""NanoMemory - a lightweight, persistent and token-efficient memory engine for AI agents.

The package is intentionally file-based: Working Memory lives in Markdown with a JSON
sidecar, Long-term Memory in JSON, Episodic Memory in JSONL.  Every persistent write is
validated, cross-process locked, and committed through an atomic transaction path.

Public surface
--------------
``nanomemory.NanoMemory``
    Convenience facade that wires configuration, storage, retrieval, providers and the
    agent loop together.  Imported lazily so ``import nanomemory`` stays cheap.
``nanomemory.__version__``
    Installed product version, also reported by ``/health``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nanomemory.core.facade import NanoMemory as NanoMemory

__all__ = ["__version__", "PRODUCT_NAME", "NanoMemory"]

PRODUCT_NAME = "NanoMemory"
__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """Lazily expose the convenience facade without importing the whole stack."""
    if name == "NanoMemory":
        from nanomemory.core.facade import NanoMemory

        return NanoMemory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
