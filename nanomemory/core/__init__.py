"""Core engine: storage, transactions, retrieval, agent loop and observability.

Sub-modules are imported explicitly by callers; this initializer intentionally exposes no
names so that importing one part of the engine does not drag in the rest.
"""

from __future__ import annotations
