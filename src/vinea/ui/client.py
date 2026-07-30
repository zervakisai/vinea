"""The API client -- the ONLY door between the UI and the system.

Every panel gets its data through a method here, and every method is an HTTP call
to the FastAPI. There is deliberately no import of `vinea.db`, `vinea.repository`,
or any internal in this file: if the UI could reach the database directly, "the UI
talks only to the API" would be a slogan, not a fact. Keeping the client a thin
httpx wrapper is what makes the rule checkable -- and checked (tests/test_ui.py).

Auth mirrors the API's two credentials (S5): a tenant key for grower views, an ops
key for operator views. The client carries both and sends the right one per call,
so the UI can't accidentally use a tenant key on an operator endpoint (it would
401, as the API intends).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import httpx


@dataclass
class ApiClient:
    """A thin httpx wrapper over the Vinea API. No DB, no models, just HTTP."""

    base_url: str = "http://localhost:8099"
    tenant_key: str | None = None
    ops_key: str | None = None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> ApiClient:
        """Build a client from the environment, the way the Streamlit app does."""
        return cls(
            base_url=os.environ.get("VINEA_API_URL", "http://localhost:8099"),
            tenant_key=os.environ.get("VINEA_UI_TENANT_KEY"),
            ops_key=os.environ.get("VINEA_OPS_KEY"),
        )

    # --- low-level -----------------------------------------------------------

    def _get(self, path: str, *, ops: bool = False, params: dict | None = None) -> httpx.Response:
        headers = {}
        if ops:
            headers["X-Ops-Key"] = self.ops_key or ""
        else:
            headers["X-API-Key"] = self.tenant_key or ""
        return httpx.get(
            f"{self.base_url}{path}", headers=headers, params=params, timeout=self.timeout
        )

    def _post(self, path: str) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}{path}", headers={"X-API-Key": self.tenant_key or ""}, timeout=self.timeout
        )

    # --- grower views (tenant key) ------------------------------------------

    def health(self) -> dict:
        return self._get("/health").json()

    def get_advisory(self, tenant: str, run_date: date) -> dict | None:
        """One advisory envelope, or None if it doesn't exist yet (404)."""
        r = self._get(f"/advisories/{tenant}/{run_date.isoformat()}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def list_advisories(
        self, tenant: str, *, start: date | None = None, end: date | None = None
    ) -> list[dict]:
        params = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        r = self._get(f"/advisories/{tenant}", params=params)
        r.raise_for_status()
        return r.json()

    def enqueue(self, tenant: str, run_date: date) -> dict:
        r = self._post(f"/advisories/{tenant}/{run_date.isoformat()}")
        r.raise_for_status()
        return r.json()

    # --- operator views (ops key) -------------------------------------------

    def queue_depth(self) -> dict:
        r = self._get("/ops/queue", ops=True)
        r.raise_for_status()
        return r.json()

    def queue_history(self, *, limit: int = 500) -> list[dict]:
        r = self._get("/ops/queue/history", ops=True, params={"limit": limit})
        r.raise_for_status()
        return r.json()

    def slo(self) -> list[dict]:
        """The objectives and their error budgets, from /ops/slo."""
        r = self._get("/ops/slo", ops=True)
        r.raise_for_status()
        return r.json()

    def all_advisories(
        self, *, start: date | None = None, end: date | None = None, limit: int = 500
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        r = self._get("/ops/advisories", ops=True, params=params)
        r.raise_for_status()
        return r.json()


def langfuse_trace_url(trace_id: str) -> str:
    """Deep link to a trace in Langfuse.

    Built from the same LANGFUSE_HOST the exporter uses (obs/tracing.py), so the
    link points at the store the trace actually went to. If Langfuse isn't
    configured the caller shows the raw id instead of a dead link.
    """
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    project = os.environ.get("LANGFUSE_PROJECT_ID", "vinea")
    return f"{host}/project/{project}/traces/{trace_id}"
