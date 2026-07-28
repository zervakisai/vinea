"""Persistence: the boundary expressed at the data layer.

The rule this package exists to enforce is ADR-001 -- *store what you cannot
recompute*. Raw observations and LLM output with its provenance are
irreplaceable, so they are rows. Deterministic features are a pure function of
observations plus config, so they are a cache with a comment saying you may
drop it.

`models.py` holds the tables, `mapping.py` is the single place contracts become
rows and back, `repository.py` is the only thing the rest of the system calls,
and `session.py` makes engines.
"""
