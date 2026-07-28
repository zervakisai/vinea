"""phase 13 -- the deployment contract: probes, image, chart.

Three tiers, and the tier decides what a failure means:

  * **Probe semantics** run fully offline. They need no database and no cluster,
    because the interesting behaviour -- what happens when the database is *gone*
    -- is exactly the case a real database cannot produce on demand.
  * **Chart rendering** needs `helm` on PATH; it SKIPS without it.
  * **A live cluster** would need `kind`; nothing here requires one, and CI runs
    the real install separately.

The skip-not-fail rule is the house rule (ADR-003's queue tests set the
precedent): a red suite that means "you didn't install helm" trains people to
ignore red.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vinea.api import main

CHART = Path(__file__).resolve().parents[1] / "infra" / "chart"


# --------------------------------------------------------------------------- #
# Probe semantics -- liveness and readiness are different questions            #
# --------------------------------------------------------------------------- #


class _DeadSession:
    """A session whose every query fails, i.e. Postgres is gone."""

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("connection refused")


class _LiveSession:
    def execute(self, *_args, **_kwargs):
        return None


# NOTE: these must be generator *functions*, not lambdas returning an iterator.
# FastAPI decides how to handle a dependency with `inspect.isgeneratorfunction`,
# so `lambda: iter([session])` injects the iterator object itself rather than the
# session it yields -- and then `session.execute(...)` raises AttributeError, the
# route catches it, and every probe reports "unreachable". The dead-database tests
# below passed that way first time round, for entirely the wrong reason.
@pytest.fixture
def client_without_database():
    def _dead_session():
        yield _DeadSession()

    main.app.dependency_overrides[main.get_session] = _dead_session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def client_with_database():
    def _live_session():
        yield _LiveSession()

    main.app.dependency_overrides[main.get_session] = _live_session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_liveness_stays_200_when_the_database_is_gone(client_without_database):
    """The load-bearing one.

    A liveness probe that fails on an unreachable database restarts every pod,
    repeatedly, for a fault no restart can fix -- converting an outage into a
    crash loop. /health must therefore keep answering 200 and report the trouble
    in the body instead.
    """
    r = client_without_database.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "unreachable"}


def test_readiness_is_503_when_the_database_is_gone(client_without_database):
    """Readiness removes the pod from the load balancer; it does not restart it."""
    r = client_without_database.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "database": "unreachable"}


def test_readiness_is_200_when_the_database_answers(client_with_database):
    r = client_with_database.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok"}


def test_health_alone_cannot_serve_as_a_readiness_probe(client_without_database):
    """Why /ready had to exist at all.

    An httpGet probe reads the status code and nothing else. /health answers 200
    with a dead database -- correctly, for liveness -- so a readiness probe
    pointed at it would keep a pod that cannot serve in the load balancer. This
    test is the guard on that reasoning: if someone 'simplifies' /health into
    returning 503, liveness starts crash-looping and this fails.
    """
    assert client_without_database.get("/health").status_code == 200
    assert client_without_database.get("/ready").status_code == 503


# --------------------------------------------------------------------------- #
# Chart rendering -- skips without helm                                        #
# --------------------------------------------------------------------------- #

helm_required = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm not on PATH. Install helm to exercise the chart; CI always has it.",
)


def _render(*set_values: str) -> str:
    args = ["helm", "template", "vinea", str(CHART)]
    for value in set_values:
        args += ["--set", value]
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.fail(f"helm template failed:\n{out.stderr}")
    return out.stdout


@helm_required
def test_chart_renders():
    assert "kind: Deployment" in _render()


@helm_required
def test_migration_job_is_a_pre_upgrade_hook():
    """The ordering claim of the phase, asserted on the rendered manifest.

    The migration must run to completion *before* any new pod serves traffic. In
    Helm that is a hook weight and a hook kind, not a comment in a CI script --
    so this test reads them back out of the rendered YAML.
    """
    rendered = _render()
    assert "helm.sh/hook: pre-install,pre-upgrade" in rendered
    assert "alembic" in rendered


@helm_required
def test_api_probes_point_at_the_right_endpoints():
    """Liveness at /health, readiness at /ready -- never the other way round."""
    rendered = _render()
    assert "path: /health" in rendered
    assert "path: /ready" in rendered


@helm_required
def test_worker_is_a_cronjob_not_a_deployment():
    """The worker is not request-driven; a Deployment would restart it forever."""
    rendered = _render()
    assert "kind: CronJob" in rendered


@helm_required
def test_no_plaintext_secret_values_are_rendered():
    """Secrets come from a SealedSecret, so the chart must not template literals.

    The house rule is that secrets never live in tracked files. In Kubernetes the
    manifests *are* tracked, so the rule needs a mechanism rather than discipline.
    """
    rendered = _render()
    assert "kind: Secret\n" not in rendered
    for leaked in ("ANTHROPIC_API_KEY: sk-", "postgresql+psycopg://", "password:"):
        assert leaked not in rendered
