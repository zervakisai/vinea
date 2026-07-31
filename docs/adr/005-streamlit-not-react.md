# ADR-005: Streamlit for the dashboard, not React

- **Status:** accepted
- **Date:** 2026-07-20
- **Milestone:** phase 11 (UI)

## Context

The system needs a UI: a grower views their advisory, an operator watches the queue
and quality. The reflex for "a dashboard" is a single-page app -- React, a component
library, a build pipeline, a dev server, an API client generated from the OpenAPI
spec. That's days of work and a second language in the repo.

The question is what the UI is *for* here. It is not the product -- the product is
the deterministic advisory and the boundary around it. The UI is a window onto data
the API already serves. Nothing about the architecture is demonstrated by
hand-rolling a React app; the interesting decisions all live below the API line.

## Decision

**Build the UI in Streamlit: a Python script per panel, charts from a dataframe, no
build step.** The whole dashboard -- grower card, operator overview with the
queue-depth chart, quality monitor, trace deep-links -- is a few hundred lines of
Python that any contributor who knows the rest of the repo can read and change.

The UI talks *only* to the FastAPI (S6's rule): every panel gets its data from
`ui/client.py`, which is httpx calls to the API. No panel imports `vinea.db`,
`vinea.jobs`, or an agent. That rule is enforced by a test
(`test_ui_never_imports_the_database_or_internals`) that scans the UI source and
fails on a forbidden import -- so "the UI is a thin client" is checkable, not
aspirational.

## Alternatives considered

**React (or Vue/Svelte) SPA.** The default for a "real" dashboard, and the right call
when the UI *is* the product -- rich interaction, custom components, a design system.
Rejected here because none of that is needed: the value is the advisory and the
boundary, and a React app would be days of build tooling, a second language, and a
generated API client to maintain, all to render a few read-mostly panels. Complexity
must earn its place, and this complexity doesn't.

**Server-rendered templates (Jinja + HTMX).** Lighter than React, and a fine choice.
Rejected mostly on charts: the operator overview and quality monitor want real charts
(queue depth over time, confidence distribution), and Streamlit gives those from a
dataframe in one line, where templates would mean adding a JS charting library and
wiring it up -- reintroducing the front-end build I'm avoiding.

**A CLI-only "UI".** The repo already has a CLI. Rejected because the operator views
are inherently visual -- "is the queue backing up tonight" is a chart question, and a
chart in a terminal is a worse chart.

**Gradio.** Similar trade-offs to Streamlit, more oriented to ML-model demos (input →
output) than to multi-panel dashboards with an operator view. Streamlit's page/panel
model fits "several views over shared data" better.

## Consequences

**Good.**

- Hours, not days. The dashboard is Python the rest of the repo's contributors already
  read. Adding a panel is adding a function.
- The "UI talks only to the API" rule is enforced by a source-scanning test. Delete
  the UI and nothing upstream breaks -- verified by the same test suite that keeps
  the core byte-identical.
- Charts come free: `st.line_chart(df)` is the queue-depth-over-time panel.
- No build step, no second language, no generated client to keep in sync.

**Costs, accepted.**

- Streamlit's rerun-the-whole-script model is not how you'd build a high-interaction
  app. Fine for read-mostly dashboards; a limitation the day someone wants a rich
  editing UI, at which point the API is already there for a React app to consume
  without touching anything below the API line.
- Rendering bugs hide from a "did the server boot" check -- a chart that can't
  serialise its data crashes only when the panel actually runs. Mitigated by testing
  panels with Streamlit's `AppTest`, which executes the real render code headlessly.
  The known trap it guards against: a pandas `Interval` bin label altair can't
  serialise, which is why the confidence-distribution bins are labelled as strings.

## Verification

```bash
# The UI renders, offline, against the real API + DB via AppTest:
uv run pytest tests/test_ui.py -q                 # panels render, no exceptions

# The rule -- UI reaches nothing but the API:
uv run pytest tests/test_ui.py -k never_imports -v

# Live:
uv run uvicorn vinea.api.main:app --port 8099 &
# Keys are minted, not invented (ADR-012):
#   python -m vinea.keys issue --tenant acme --label "local UI"
#   python -m vinea.keys issue --ops --label "local ops"
VINEA_API_URL=http://localhost:8099 VINEA_UI_TENANT_KEY=vinea_t_... \
  VINEA_OPS_KEY=vinea_o_... uv run streamlit run src/vinea/ui/app.py
# open http://localhost:8501 -- grower card, operator queue-depth chart, quality monitor
```
