"""``python -m nanomemory`` entry point."""

from nanomemory.cli import main

__all__ = ["main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
