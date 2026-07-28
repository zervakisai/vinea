"""The operator dashboard + grower view. Talks ONLY to the FastAPI.

THE RULE (S6): the UI reaches the system through the HTTP API and nothing else. It
never imports `vinea.db`, `vinea.jobs`, `vinea.agents`, or `vinea.graph`; it never
opens a database session; it never runs a model. If you deleted this package,
nothing upstream would notice.

That rule is enforced, not just stated: `tests/test_ui.py` scans this package's
source and fails if it imports any of the forbidden internals. The only door in is
`client.py`, which is httpx calls to the API -- the same discipline S5's API has
toward the queue, one layer further out.

`client.py` is the API client. `app.py` is the Streamlit entrypoint. `panels/`
holds the views (grower card, operator overview, quality monitor, trace links). Run
it with:

    uv run streamlit run src/vinea/ui/app.py
"""
