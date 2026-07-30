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


# --------------------------------------------------------------------------- #
# The gateway is opt-in, all the way down (phase 14)                           #
# --------------------------------------------------------------------------- #


@helm_required
def test_the_default_deploy_has_no_gateway():
    """The phase's central claim, asserted on the rendered manifest.

    With `gateway.enabled: false` nothing tells the app a gateway exists, so
    `resolve_model()` returns the plain model string and the deployment behaves
    exactly as phase 13's did. If this ever renders a URL by default, the "no
    gateway changes nothing" guarantee has quietly become "no gateway is
    untested".
    """
    rendered = _render()
    assert "VINEA_GATEWAY_URL" not in rendered
    assert "component: gateway" not in rendered


@helm_required
def test_the_worker_is_wired_to_the_gateway_and_the_ui_is_not():
    """The workload that calls a model gets the URL. The UI never should.

    This replaces an assertion that counted occurrences of VINEA_GATEWAY_URL and
    expected 3. The count was a hostage to the next workload added -- it broke the
    moment the SLO CronJob started using the shared env helper, which was correct
    behaviour and a red test -- and it never checked *which* workloads, so it would
    equally have passed with the wrong three.

    Checking by workload also surfaced something the count hid: the UI is not
    wired to the gateway at all, and must not be. ADR-005 says it may only speak
    HTTP to the API, so a gateway URL in its environment would be either dead
    configuration or a violation waiting to happen.
    """
    rendered = _render("gateway.enabled=true")

    def docs_for(component: str) -> list[str]:
        # ANY document for the component, not the first: a Service and a
        # Deployment both carry `component: api`, and the Service renders first
        # with no env at all.
        return [doc for doc in rendered.split("---") if f"component: {component}" in doc]

    worker_docs = docs_for("worker")
    assert worker_docs, "no rendered worker manifest"
    assert any("VINEA_GATEWAY_URL" in doc for doc in worker_docs), (
        "the worker is the only workload that calls a model; it must be wired"
    )

    ui_docs = docs_for("ui")
    assert ui_docs, "no rendered ui manifest"
    assert not any("VINEA_GATEWAY_URL" in doc for doc in ui_docs), (
        "the UI may only speak HTTP to the API (ADR-005) -- it has no business "
        "holding a gateway URL"
    )

    assert "http://vinea-vinea-gateway:4000" in rendered
    assert "kind: ConfigMap" in rendered


@helm_required
def test_an_external_gateway_renders_no_gateway_workload():
    """Somebody else operates it: point at it and render nothing to run."""
    rendered = _render("gateway.enabled=true", "gateway.externalUrl=https://llm.example.com")
    assert "https://llm.example.com" in rendered
    assert "component: gateway" not in rendered


@helm_required
def test_the_gateway_secret_is_not_the_application_secret():
    """The security case for a gateway, as a Kubernetes fact.

    Provider keys live in the gateway's Secret; app pods read a different one and
    hold only a virtual key with a spend ceiling. Collapsing the two would hand
    every workload the unbounded credential and undo the reason for running a
    gateway at all -- so the separation is asserted rather than left to values.
    """
    rendered = _render("gateway.enabled=true")
    api_secret = "vinea-secrets"
    gateway_secret = "vinea-gateway-secrets"
    assert gateway_secret in rendered
    assert api_secret != gateway_secret
    # The gateway's Secret must appear exactly once: on the gateway pod.
    assert rendered.count(gateway_secret) == 1


@helm_required
def test_the_gateway_config_is_not_templated_secrets():
    """The ConfigMap is world-readable to anyone with `get configmaps`.

    Every credential in the LiteLLM config must therefore be `os.environ/NAME`,
    resolved from the Secret at runtime -- never a value.
    """
    rendered = _render("gateway.enabled=true")
    assert "os.environ/ANTHROPIC_API_KEY" in rendered
    assert "sk-ant-" not in rendered
    assert "sk-vinea" not in rendered


@helm_required
def test_the_gateway_rolls_when_its_config_changes():
    """Without the checksum annotation, editing the model list updates the mounted
    file on a pod that read it at startup -- and `helm upgrade` reports success on
    a gateway still serving the old configuration."""
    rendered = _render("gateway.enabled=true")
    assert "checksum/config:" in rendered


# --------------------------------------------------------------------------- #
# Retrieval is opt-in too (phase 15)                                           #
# --------------------------------------------------------------------------- #


@helm_required
def test_the_default_deploy_ingests_no_corpus():
    """Same claim as the gateway's, one phase later.

    With `rag.enabled: false` no corpus reaches the database, `retrieve_for`
    finds nothing, and the deployment is phase 14's. If this ever renders by
    default, "retrieval changes nothing when off" has become "off is untested".
    """
    assert "component: corpus-ingest" not in _render()


@helm_required
def test_the_corpus_ingest_runs_after_the_migration_not_before():
    """The two hooks point opposite ways, and the ordering is the reasoning.

    The migration is `pre-upgrade`: code that expects a column the database lacks
    is broken code serving traffic, so it must gate the release. The ingest is
    `post-upgrade`: it writes rows into the table that migration just created,
    and a missing corpus is not an outage -- retrieval fails open to silence and
    the advisory is produced regardless.
    """
    rendered = _render("rag.enabled=true")
    assert "helm.sh/hook: post-install,post-upgrade" in rendered
    assert "component: corpus-ingest" in rendered
    # Still exactly one pre-upgrade hook: the migration. If the ingest ever
    # became one, a corpus problem would start blocking deploys.
    assert rendered.count("helm.sh/hook: pre-install,pre-upgrade") == 1


@helm_required
def test_tracing_is_off_by_default_and_wired_when_a_host_is_set():
    """LANGFUSE_HOST is the switch; the keys stay in the Secret.

    Off is a supported state, not a broken one: with no host, `configure_tracing`
    builds no exporter and `advisories.trace_id` stays NULL. The host is not a
    secret and belongs in values where `helm get values` shows it; the public and
    secret keys arrive through `envFrom` and must never be templated.
    """
    assert "LANGFUSE_HOST" not in _render()

    rendered = _render("langfuse.host=http://langfuse-web.langfuse.svc.cluster.local:3000")
    assert "LANGFUSE_HOST" in rendered
    assert "langfuse-web.langfuse.svc.cluster.local" in rendered
    # The keys are never rendered, only referenced.
    for leaked in ("LANGFUSE_SECRET_KEY:", "sk-lf-", "pk-lf-"):
        assert leaked not in rendered, f"{leaked} appears in a rendered manifest"


@helm_required
def test_langfuse_is_not_vendored_into_this_chart():
    """It is a separate product with its own chart.

    Vendoring four stateful services in would make `helm upgrade vinea` responsible
    for someone else's database migrations. The chart wires an address; it does not
    run the thing at that address (docs/deploy-langfuse.md).
    """
    rendered = _render("langfuse.host=http://example:3000")
    for foreign in ("clickhouse", "langfuse/langfuse", "minio"):
        assert foreign not in rendered.lower(), f"{foreign} is being deployed by this chart"
