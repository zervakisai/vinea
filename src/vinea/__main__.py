"""Enables `python -m vinea` (mirrors the `vinea` console script)."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
