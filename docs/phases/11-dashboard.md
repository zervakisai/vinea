# Phase 11 — Dashboard

`git checkout phase-11`

## What you learn

How to write down an architectural rule in a way that a **test** enforces, so that
the boundary survives the next person in a hurry.

## The central idea

[ADR-005](../adr/005-streamlit-not-react.md): Streamlit, not React.

**The rule:** the UI reaches the system through the HTTP API and nothing else. It
never imports `vinea.db`, `vinea.jobs`, `vinea.agents` or `vinea.graph`; it never
opens a database session; it never runs a model.

And that rule is *enforced*, not merely stated. `tests/test_ui.py` scans this
package's source and fails if it imports any forbidden internal:

```python
assert not any(i.startswith("vinea.db") for i in imports)
```

This is the interesting part of the phase. Every codebase has rules like this in a
CONTRIBUTING file, where they decay. A rule with a test is a rule; a rule in prose
is a preference.

## Decisions

- **Streamlit over React** — because the deliverable is a read-mostly operational
  view, and a React app would add a build step, a second language and a deploy
  target to render three tables and a timeline. Revisit when the UI needs genuine
  interaction, not before.
- **`client.py` is the only door in.** httpx calls to the API. One file to audit,
  one file to mock.
- **pandas is a UI-layer dependency.** It arrives here, for chart dataframes. The
  deterministic core stays pandas-free, so importing the physics never drags a
  dataframe library into a worker process.
- **Three panels, three audiences.** `grower.py` (the advisory card),
  `operator.py` (queue and throughput), `quality.py` (data quality and trace
  links). Different questions, so different views, rather than one page with a
  role toggle.

## Read this

- `docs/adr/005-streamlit-not-react.md`
- `src/vinea/ui/__init__.py` — the rule, stated where you would look for it
- `src/vinea/ui/client.py` — the only door
- `tests/test_ui.py` — the import scan that enforces the rule

## The trap

The import scan is a **static** check. It greps this package's source for forbidden
module names, which means it catches the obvious violation — someone adding
`from vinea.db import repository` at the top of a panel — and misses anything
dynamic:

```python
mod = importlib.import_module("vinea." + name)      # invisible to the scan
```

It also cannot see a violation that goes through a *new* module you add inside
`ui/`, if the scan's list of forbidden prefixes was not updated. The test encodes a
list, and lists rot.

So treat it as a tripwire, not a proof. It raises the cost of the accidental
violation, which is most of them; it does not make the boundary impossible to
cross. Knowing which of those two you have is the difference between a guardrail and
a comfort blanket.

## Try it

```bash
docker compose up -d postgres
uv run uvicorn vinea.api.main:app --port 8099 &
VINEA_API_URL=http://localhost:8099 VINEA_UI_TENANT_KEY=key-acme \
  VINEA_OPS_KEY=ops-secret uv run streamlit run src/vinea/ui/app.py
```

Then break the rule on purpose: add `from vinea.db import repository` to
`src/vinea/ui/panels/grower.py` and run `uv run pytest tests/test_ui.py`. It goes
red, and the message tells you which rule you broke. That failure mode is the whole
deliverable of this phase.
