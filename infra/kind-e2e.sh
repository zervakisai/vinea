#!/usr/bin/env bash
# End-to-end deploy against a real Kubernetes cluster, free and on demand.
#
#   ./infra/kind-e2e.sh            # create cluster, deploy, smoke, leave it up
#   ./infra/kind-e2e.sh --cleanup  # ...and delete the cluster afterwards
#
# CI runs this exact script. That is deliberate: a pipeline that reimplements the
# deploy in YAML is a second implementation nobody runs locally, and it drifts.
#
# Postgres runs INSIDE the cluster here. That does not contradict ADR-006 -- it is
# a test fixture, not the architecture. Production Postgres is managed and
# external, because ADR-001 says the advisories are the one thing that cannot be
# recomputed and they do not belong on the newest component in the system.

set -euo pipefail

CLUSTER="${CLUSTER:-vinea}"
RELEASE="${RELEASE:-vinea}"
NAMESPACE="${NAMESPACE:-default}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLEANUP=0
[[ "${1:-}" == "--cleanup" ]] && CLEANUP=1

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# Run a one-shot pod and return ONLY its stdout.
#
# `kubectl run --rm -i` is the obvious way and it is unreliable: the attach can
# miss output written before it connects, and `--rm` prints its own
# `pod "x" deleted` line to stdout, which a caller capturing "$(...)" then parses
# as the program's output. That is exactly what happened -- an assertion read the
# deletion message, found none of what it wanted, and failed a correct deploy.
#
# Run detached, wait for the phase, read the logs, then delete. Three more lines,
# and the output is the output.
in_cluster() {
  local name="$1"; shift
  kubectl delete pod "$name" --ignore-not-found --wait=true >/dev/null 2>&1
  kubectl run "$name" --restart=Never --image=vinea:e2e --image-pull-policy=Never \
    --env="DATABASE_URL=postgresql+psycopg://vinea:vinea@postgres:5432/vinea" \
    --command -- "$@" >/dev/null 2>&1
  if ! kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=240s >/dev/null 2>&1; then
    echo "--- $name did not succeed; logs follow ---" >&2
    kubectl logs "pod/$name" >&2 2>/dev/null || true
    kubectl delete pod "$name" --ignore-not-found >/dev/null 2>&1
    return 1
  fi
  kubectl logs "pod/$name" 2>/dev/null
  kubectl delete pod "$name" --ignore-not-found >/dev/null 2>&1
}

# `return 0` is load-bearing, and its absence cost a red CI run on a script that
# printed PASS. Written as `[[ $CLEANUP -eq 1 ]] && { ... }`, this function
# returns 1 whenever CLEANUP is 0 -- and because it is the last command of the
# EXIT trap, that 1 becomes the script's exit status. Everything succeeds, PASS
# is printed, and the process exits non-zero.
#
# It survived local testing because every local run was piped (`| tail`), and a
# pipeline reports the exit status of its LAST command. CI ran it bare and caught
# it. Which is the phase's own lesson arriving by post: a green result that is
# not evidence.
cleanup() {
  if [[ $CLEANUP -eq 1 ]]; then
    step "deleting cluster"
    kind delete cluster --name "$CLUSTER"
  fi
  return 0
}
trap cleanup EXIT

for tool in kind kubectl helm docker; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

step "cluster"
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" \
  || kind create cluster --name "$CLUSTER" --wait 120s
kubectl config use-context "kind-${CLUSTER}" >/dev/null

step "build images"
docker build --target app -t vinea:e2e "$ROOT"
docker build --target ui  -t vinea-ui:e2e "$ROOT"

step "load images into the cluster"
# kind nodes have their own image store; without this the pods sit in
# ImagePullBackOff trying to reach a registry that has never heard of vinea:e2e.
kind load docker-image vinea:e2e vinea-ui:e2e --name "$CLUSTER"

step "test-fixture Postgres"
kubectl apply -f "$ROOT/infra/testing/postgres.yaml"
kubectl rollout status deploy/postgres --timeout=180s

step "secret"
# Created imperatively here because this is a throwaway cluster. In a real one it
# comes from a SealedSecret -- see infra/sealed-secrets/README.md. Either way the
# chart only ever references it by name.
kubectl delete secret vinea-secrets --ignore-not-found >/dev/null
#
# VINEA_ALERT_WEBHOOK_URL points at a collector this script starts later. DNS
# resolves at request time, so the Service does not have to exist yet -- and the
# variable has to be in the Secret from the start, because `envFrom` is read when a
# pod is created and the whole point is to exercise the chart's own wiring.
#
# No VINEA_API_KEYS and no VINEA_OPS_KEY. Keys live in `api_keys` now (ADR-012), and
# they are minted after the migration hook has created the table -- which is why
# that step is further down rather than here.
kubectl create secret generic vinea-secrets \
  --from-literal=DATABASE_URL='postgresql+psycopg://vinea:vinea@postgres:5432/vinea' \
  --from-literal=VINEA_ALERT_WEBHOOK_URL='http://alert-sink:8000/hook' >/dev/null

step "helm upgrade --install"
# --wait blocks until every workload is Ready, and the pre-upgrade hook Job must
# succeed before any of them is even created. If the migration fails, this command
# fails and nothing new ever serves traffic -- which is the guarantee being tested.
helm upgrade --install "$RELEASE" "$ROOT/infra/chart" \
  --namespace "$NAMESPACE" \
  --set image.repository=vinea --set image.tag=e2e \
  --set uiImage.repository=vinea-ui --set uiImage.tag=e2e \
  --wait --timeout 5m

# Same tag, new image content. The Deployment spec is byte-identical to the last
# run's, so helm computes no change and performs no rollout -- and the pods keep
# serving the code from the previous build while every schema assertion below
# passes, because those run in fresh one-shot pods that DO pull the new image.
#
# That is not hypothetical: it is how this script first reported a 401 from a key
# it had just minted. The API pods were still running the build that read
# VINEA_API_KEYS, which the Secret no longer carries.
#
# A production deploy never has this problem, because `helm upgrade` there changes
# `image.tag` and the change is what triggers the rollout. A rebuild under a fixed
# tag is the e2e's own shortcut, so the e2e pays for it here.
step "force a rollout (same tag, new image)"
for deployment in $(kubectl get deploy -l "app.kubernetes.io/instance=$RELEASE" -o name); do
  kubectl rollout restart "$deployment" >/dev/null
done
for deployment in $(kubectl get deploy -l "app.kubernetes.io/instance=$RELEASE" -o name); do
  kubectl rollout status "$deployment" --timeout=180s >/dev/null \
    || { echo "$deployment did not roll out" >&2; exit 1; }
done
echo "rolled: $(kubectl get deploy -l "app.kubernetes.io/instance=$RELEASE" -o name | tr '\n' ' ')"

step "assert: the migration hook ran and completed"
kubectl get job -l app.kubernetes.io/component=migrate \
  -o jsonpath='{.items[0].status.succeeded}' | grep -qx 1 \
  || { echo "migration job did not report success" >&2; exit 1; }
echo "migration job: succeeded"

step "assert: alembic is actually at head"
# Capture, then assert -- never `| grep -q` straight off a pod. See `in_cluster`.
revision=$(in_cluster alembic-check alembic current)
echo "$revision" | grep -q "(head)" \
  || { echo "schema is not at head: ${revision:-<no output>}" >&2; exit 1; }
echo "alembic: at head ($(echo "$revision" | grep -o '^[0-9a-f]*' | head -1))"

step "assert: the default deploy carries no gateway"
# Phase 14's central claim, checked against a live pod rather than a rendered
# template: with `gateway.enabled=false` nothing tells the app a gateway exists,
# so `resolve_model()` returns the plain model string and this deployment is the
# gateway-free deployment. A cluster that quietly grew a VINEA_GATEWAY_URL would
# mean the "no gateway changes nothing" guarantee had become "no gateway is
# untested".
#
# The gateway itself is not installed here: LiteLLM needs a provider key to be
# worth starting, CI has none, and a proxy with no upstream proves nothing that
# `helm template` does not already prove offline. Said out loud because a silent
# gap reads like coverage.
gateway_env=$(kubectl get deploy "${RELEASE}-vinea-api" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VINEA_GATEWAY_URL")].value}')
[[ -z "$gateway_env" ]] || { echo "api pod has VINEA_GATEWAY_URL=$gateway_env in the default deploy" >&2; exit 1; }
echo "no VINEA_GATEWAY_URL on the api pod (correct for gateway.enabled=false)"

step "assert: the expand migration added the cost columns"
# The pre-upgrade hook ran (asserted above); this asserts what it *did*. Four
# additive nullable columns, and the nullability is the claim: a server_default
# would make every advisory written before tonight report that it cost zero.
columns=$(in_cluster cost-columns python -c "
import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    rows = c.execute(text(\"select column_name, is_nullable, column_default from information_schema.columns where table_name='advisories' and column_name in ('input_tokens','output_tokens','cost_usd','cache_hit') order by column_name\")).all()
print(';'.join(f'{n}:{null}:{default}' for n, null, default in rows))
")
for col in cache_hit cost_usd input_tokens output_tokens; do
  echo "$columns" | grep -q "${col}:YES:None" \
    || { echo "cost column ${col} missing or not nullable-without-default: ${columns:-<no output>}" >&2; exit 1; }
done
echo "cost columns: present, nullable, no default"

step "assert: the vector extension and corpus tables exist"
# The genuinely risky part of migration c73a51e8d4b2 is `CREATE EXTENSION vector`,
# which succeeds only on a server that HAS pgvector -- the stock postgres:16 image
# does not. Asserting it here is asserting that the test fixture, the compose
# stack and any real cluster are all running an image that carries it, which is a
# deployment fact no unit test can reach.
schema=$(in_cluster vector-check python -c "
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    ext = c.execute(text(\"select 1 from pg_extension where extname='vector'\")).first()
    cols = c.execute(text(\"select count(*) from information_schema.columns where table_name='corpus_chunks'\")).scalar_one()
    cites = c.execute(text(\"select count(*) from information_schema.columns where table_name='advisory_citations'\")).scalar_one()
print(f'vector={bool(ext)} corpus_chunks_cols={cols} advisory_citations_cols={cites}')
")
echo "$schema" | grep -q "vector=True" \
  || { echo "pgvector extension missing: ${schema:-<no output>}" >&2; exit 1; }
echo "$schema" | grep -qE "corpus_chunks_cols=(9|10|11)" \
  || { echo "corpus_chunks not created as expected: ${schema:-<no output>}" >&2; exit 1; }
echo "schema: $schema"

step "assert: row-level security is real in the cluster"
# Behaviour, not configuration. `relrowsecurity = true` is what the FIRST version
# of the RLS migration reported while being completely inert -- the connecting
# role was a superuser, and superusers bypass row security unconditionally. So
# this checks the role AND counts rows from a query with no tenant filter.
rls=$(in_cluster rls-check python -c "
from sqlalchemy import text
from sqlmodel import Session, select
from vinea.db.models import Advisory
from vinea.db.session import make_engine, scope_to_ops, scope_to_tenant
e = make_engine()
with Session(e) as s:
    user, sup, byp = s.execute(text('SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')).one()
with Session(e) as s:
    scope_to_ops(s)
    for t in ('rls-a', 'rls-b'):
        s.execute(text(\"INSERT INTO advisories (tenant, run_date, target_date, irrigation, spray, reconciliation, deps_hash) VALUES (:t, '2026-01-01', '2026-01-01', '{}', '{}', '{}', 'h') ON CONFLICT DO NOTHING\"), {'t': t})
    s.commit()
with Session(e) as s:
    scope_to_tenant(s, 'rls-a')
    scoped = {r.tenant for r in s.exec(select(Advisory)).all()}
with Session(e) as s:
    unscoped = s.exec(select(Advisory)).all()
print(f'user={user} super={sup} bypass={byp} scoped={sorted(scoped)} unscoped={len(unscoped)}')
")
echo "$rls" | grep -q "user=vinea_app super=False bypass=False" \
  || { echo "app role is not restricted: ${rls:-<no output>}" >&2; exit 1; }
echo "$rls" | grep -q "scoped=\['rls-a'\]" \
  || { echo "a query with no WHERE crossed a tenant boundary: ${rls:-<no output>}" >&2; exit 1; }
echo "$rls" | grep -q "unscoped=0" \
  || { echo "an unscoped session was not fail-closed: ${rls:-<no output>}" >&2; exit 1; }
echo "rls: $rls"

step "assert: the image can find its own data"
# The guard that would have caught a bug which shipped for eighteen phases.
# `config.DEFAULT_DATA_DIR` falls back to a path derived from the module location,
# which is the repo root for a source checkout and a directory inside the venv for
# the wheel this image installs. Nothing noticed, because the two things that read
# it -- the worker's CSV fallback and the corpus ingest -- had never run in a
# cluster. Cheap to check, and it fails loudly instead of as an IndexError.
paths=$(in_cluster data-paths python -c "
from vinea import config
from vinea.rag.corpus import CORPUS_PATH
print(f'data={config.DEFAULT_DATA_DIR} exists={config.DEFAULT_DATA_DIR.exists()} corpus={CORPUS_PATH.exists()} csvs={len(sorted(config.DEFAULT_DATA_DIR.glob(chr(42)+chr(46)+\"csv\")))}')
")
echo "$paths" | grep -q "exists=True" || { echo "the image cannot find its data dir: ${paths:-<no output>}" >&2; exit 1; }
echo "$paths" | grep -q "corpus=True"  || { echo "the image cannot find the corpus: ${paths:-<no output>}" >&2; exit 1; }
echo "paths: $paths"

step "assert: two tenants with different weather get different advisories"
# The gap this closes was structural: the worker read one weather file for every
# tenant, so ten growers got ten identical advisories. Checked here rather than only
# in unit tests because it depends on grower_config, weather_observations, the queue
# and the worker agreeing in a real deploy.
#
# Robust to network either way: if the Open-Meteo fetch succeeds, the two sites are
# genuinely different weather; if it fails, the seeded rows are. Both paths must
# produce different depletions.
tenants=$(in_cluster two-tenants python -c "
from datetime import date
from pathlib import Path
from sqlalchemy import text
from sqlmodel import Session
from vinea import config
from vinea.db import repository
from vinea.db.session import make_engine, scope_to_ops
from vinea.deps import WINE_GRAPES
from vinea.ingest import load_weather
from vinea.jobs import queue, worker
from vinea.sources.db_source import API_SOURCE
from vinea.sources.persist import upsert_observations

RUN = date(2026, 7, 28)
SITES = {'e2e-nemea': (37.8125, 22.6875, 1.0), 'e2e-naoussa': (40.63, 22.07, 0.45)}
d = Path(config.DEFAULT_DATA_DIR)
# This script is re-runnable against a cluster it left up, and enqueue is
# idempotent -- so on a second run the task already exists with status done,
# claim_one returns nothing, and the step used to die on process_one(None).
# (No backticks in here: this Python is inside a double-quoted shell string, so
# bash would run them. It did: 'process_one(None): command not found'.)
# Clearing the two tenants this step owns makes it do the work every time. The
# alternative -- skipping when there is nothing to claim -- would pass by reading
# the PREVIOUS run's advisory, which is a green result that is not evidence.
hist, fc, _ = load_weather(sorted(d.glob('*last-30d*.csv'))[-1], sorted(d.glob('*next-7d*.csv'))[-1], RUN)
eng = make_engine()
with Session(eng) as s:
    scope_to_ops(s)
    for tenant in SITES:
        s.execute(text('DELETE FROM advisories WHERE tenant = :t'), {'t': tenant})
        s.execute(text('DELETE FROM advisory_tasks WHERE tenant = :t'), {'t': tenant})
    s.commit()
with Session(eng) as s:
    scope_to_ops(s)
    for tenant, (lat, lon, scale) in SITES.items():
        row = repository.save_grower_config(s, WINE_GRAPES, tenant=tenant, location='block-a', region='eu')
        row.latitude, row.longitude = lat, lon
        s.add(row)
        for kind, rows in (('history', hist), ('forecast', fc)):
            scaled = [r if r.et0_mm is None else r.model_copy(update={'et0_mm': r.et0_mm * scale}) for r in rows]
            upsert_observations(s, scaled, tenant=tenant, location='block-a', kind=kind, source=API_SOURCE)
        queue.enqueue(s, tenant=tenant, run_date=RUN)
    s.commit()
for _ in SITES:
    with Session(eng) as s:
        scope_to_ops(s)
        task = queue.claim_one(s, worker_id='e2e')
        assert task is not None, 'nothing to claim: the queue was not seeded'
        worker.process_one(s, task)
out = []
with Session(eng) as s:
    scope_to_ops(s)
    for tenant in SITES:
        a = repository.get_advisory(s, tenant=tenant, run_date=RUN)
        out.append(f'{tenant}={a.irrigation.current_depletion_mm:.1f}' if a else f'{tenant}=MISSING')
print(' '.join(out))
")
echo "tenants: $tenants"
echo "$tenants" | grep -q "MISSING" && { echo "a tenant produced no advisory" >&2; exit 1; }
a=$(echo "$tenants" | sed -n 's/.*e2e-nemea=\([0-9.]*\).*/\1/p')
b=$(echo "$tenants" | sed -n 's/.*e2e-naoussa=\([0-9.]*\).*/\1/p')
[[ -n "$a" && -n "$b" ]] || { echo "could not parse depletions from: $tenants" >&2; exit 1; }
[[ "$a" != "$b" ]] || { echo "both tenants got depletion $a -- one weather source for everybody" >&2; exit 1; }
echo "per-tenant weather: nemea=$a naoussa=$b (differ, correct)"

step "mint API keys with the CLI"
# The bootstrap answer to "how does the first key exist" (ADR-012). Not a migration
# -- one that minted a credential would write it into a file every deploy replays --
# and not an endpoint, which would itself need a credential. So: a command, run by
# someone with database access, after the schema exists.
#
# This is also the only place the e2e can prove the CLI works against a real
# database, and it is the step that would fail if `api_keys` had not been created.
tenant_key=$(in_cluster mint-tenant python -m vinea.keys issue --tenant acme --label "e2e smoke" \
  | grep -o 'vinea_t_[A-Za-z0-9_-]*' | tail -1)
[[ -n "$tenant_key" ]] || { echo "the CLI did not mint a tenant key" >&2; exit 1; }
ops_key=$(in_cluster mint-ops python -m vinea.keys issue --ops --label "e2e ops" \
  | grep -o 'vinea_o_[A-Za-z0-9_-]*' | tail -1)
[[ -n "$ops_key" ]] || { echo "the CLI did not mint an ops key" >&2; exit 1; }
echo "minted: ${tenant_key:0:20}... and ${ops_key:0:20}..."

step "smoke test THROUGH the API, with a real key"
# Not `curl /health`. A smoke test that proves a container started is theatre --
# it would pass with an empty database and a broken schema. This one authenticates,
# writes through the queue, and reads back.
kubectl port-forward "svc/${RELEASE}-vinea-api" 18080:8000 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true; cleanup' EXIT
sleep 4

code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/ready)
[[ "$code" == "200" ]] || { echo "readiness not 200 (got $code)" >&2; exit 1; }
echo "GET /ready -> 200"

code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST localhost:18080/advisories/acme/2026-07-28 \
  -H "X-API-Key: $tenant_key")
[[ "$code" == "202" ]] || { echo "enqueue not 202 (got $code)" >&2; exit 1; }
echo "POST /advisories/acme/2026-07-28 -> 202"

# A read of the advisory route, which is the one with a latency SLO. It 404s --
# the worker has not run -- and that is the point: the middleware must record the
# timing regardless of status, so the SLI reflects what the grower experienced.
code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/advisories/acme/2026-07-28 \
  -H "X-API-Key: $tenant_key")
[[ "$code" == "404" || "$code" == "200" ]] || { echo "advisory read gave $code" >&2; exit 1; }
echo "GET /advisories/acme/2026-07-28 -> $code"

step "assert: the read was timed into api_request_samples"
samples=$(in_cluster slo-check python -c "
from sqlalchemy import text
from sqlmodel import Session
from vinea.db.session import make_engine, scope_to_ops
from vinea.slo.queries import SLO_READ_ROUTE, read_latency_p95
with Session(make_engine()) as s:
    scope_to_ops(s)
    n = s.execute(text('SELECT count(*) FROM api_request_samples WHERE route = :r'), {'r': SLO_READ_ROUTE}).scalar_one()
    r = read_latency_p95(s)
print(f'samples={n} p95={r.value} met={r.met}')
")
echo "$samples" | grep -qE "samples=[1-9]" \
  || { echo "the read was not timed: ${samples:-<no output>}" >&2; exit 1; }
echo "latency: $samples"

step "assert: the SLO check runs and reports"
in_cluster slo-cmd python -m vinea.slo report >/dev/null \
  || { echo "python -m vinea.slo report failed" >&2; exit 1; }
echo "slo report: ran"

step "assert: a breach reaches the webhook the Secret configured"
# The unit tests prove `notify()` posts JSON. What only a cluster can prove is that
# the URL actually arrives in the pod: the chart deliberately does NOT template
# VINEA_ALERT_WEBHOOK_URL as an env entry -- it is a bearer credential and would end
# up in the rendered manifest -- so it reaches the job through the `envFrom:
# secretRef` in `vinea.env`. A wiring bug there looks exactly like "nothing was
# breached", which is the silent failure this step exists to prevent.
kubectl delete svc alert-sink --ignore-not-found >/dev/null 2>&1
kubectl delete pod alert-sink --ignore-not-found --wait=true >/dev/null 2>&1
kubectl run alert-sink --image=vinea:e2e --image-pull-policy=Never --port=8000 \
  --command -- python -c '
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        print("WEBHOOK-RECEIVED " + json.dumps(body["breaches"]), flush=True)
        self.send_response(204)
        self.end_headers()
    def log_message(self, *a):
        pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
' >/dev/null
kubectl expose pod alert-sink --port=8000 --target-port=8000 >/dev/null
kubectl wait --for=condition=Ready pod/alert-sink --timeout=180s >/dev/null

# Seed a breach that does not depend on what the rest of this run happened to do.
in_cluster slo-seed python -c "
from sqlalchemy import text
from sqlmodel import Session
from vinea.db.session import make_engine, scope_to_ops
from vinea.slo.queries import SLO_READ_ROUTE
with Session(make_engine()) as s:
    scope_to_ops(s)
    for _ in range(20):
        s.execute(text('INSERT INTO api_request_samples (route, method, status_code, duration_ms) '
                       \"VALUES (:r, 'GET', 200, 4000.0)\"), {'r': SLO_READ_ROUTE})
    s.commit()
print('seeded')
" >/dev/null || { echo "could not seed a breach" >&2; exit 1; }

# From the CronJob, not a hand-built pod: `--from=cronjob/...` copies the real pod
# template, which is what carries the envFrom. Building the pod here would test this
# script's idea of the deployment instead of the deployment.
kubectl delete job slo-notify-e2e --ignore-not-found --wait=true >/dev/null 2>&1
kubectl create job slo-notify-e2e --from="cronjob/${RELEASE}-vinea-slo-check" >/dev/null
# `kubectl wait` with a selector errors out immediately if nothing matches yet, so
# let the pod exist before waiting on its phase.
for _ in $(seq 1 60); do
  [[ -n "$(kubectl get pod -l job-name=slo-notify-e2e -o name 2>/dev/null)" ]] && break
  sleep 1
done
# The job is EXPECTED to fail: a breach exits 1 and backoffLimit is 0. Waiting for
# `complete` would hang for the full timeout on a correct run, so wait for the pod
# to stop instead.
kubectl wait --for=jsonpath='{.status.phase}'=Failed pod -l job-name=slo-notify-e2e \
  --timeout=240s >/dev/null || {
    echo "the SLO job did not fail on a seeded breach -- did it measure anything?" >&2
    kubectl logs -l job-name=slo-notify-e2e --tail=40 >&2 || true
    exit 1
  }
slo_out=$(kubectl logs -l job-name=slo-notify-e2e --tail=40 2>/dev/null)
echo "$slo_out" | grep -q "notified" \
  || { echo "the job did not report notifying anyone:"; echo "$slo_out"; exit 1; } >&2

sink=$(kubectl logs pod/alert-sink --tail=20 2>/dev/null | grep "WEBHOOK-RECEIVED" || true)
[[ -n "$sink" ]] || {
  echo "nothing reached the webhook. VINEA_ALERT_WEBHOOK_URL did not survive the chart:" >&2
  echo "$slo_out" >&2
  exit 1
}
echo "$sink" | grep -q "read_latency_p95_ms" \
  || { echo "the webhook got something other than the seeded breach: $sink" >&2; exit 1; }
echo "webhook: ${sink#WEBHOOK-RECEIVED }"
kubectl delete job slo-notify-e2e --ignore-not-found >/dev/null 2>&1
kubectl delete svc alert-sink --ignore-not-found >/dev/null 2>&1
kubectl delete pod alert-sink --ignore-not-found --wait=false >/dev/null 2>&1

code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/ops/queue -H "X-API-Key: $tenant_key")
[[ "$code" == "403" || "$code" == "401" ]] || { echo "tenant key reached /ops (got $code)" >&2; exit 1; }
echo "tenant key on /ops/* -> $code (correctly refused)"

code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/ops/queue -H "X-Ops-Key: $ops_key")
[[ "$code" == "200" ]] || { echo "the minted ops key did not open /ops/queue (got $code)" >&2; exit 1; }
echo "ops key on /ops/queue -> 200"

step "assert: revocation takes effect without a restart"
# The property the table exists for (ADR-012). Under VINEA_API_KEYS this needed a
# Secret edit and a rolling restart, so between "this key is compromised" and "this
# key stops working" sat a deploy. Nothing is restarted between these two curls --
# and the API pods are the ones that were already running when the key was minted,
# which is what makes this a test of the lookup rather than of process startup.
in_cluster revoke-key python -m vinea.keys revoke "$tenant_key" >/dev/null \
  || { echo "the CLI could not revoke the key" >&2; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/advisories/acme/2026-07-28 \
  -H "X-API-Key: $tenant_key")
[[ "$code" == "401" ]] || { echo "a revoked key still worked (got $code) -- something is caching" >&2; exit 1; }
echo "revoked key -> 401 with no restart"

step "assert: the access log recorded it, and says why"
access=$(in_cluster access-log python -c "
from sqlmodel import Session, select
from vinea.db.models import AccessLog
from vinea.db.session import make_engine, scope_to_ops
with Session(make_engine()) as s:
    scope_to_ops(s)
    rows = s.exec(select(AccessLog).order_by(AccessLog.id)).all()
print('rows=%d outcomes=%s' % (len(rows), ','.join(sorted({r.outcome for r in rows}))))
")
echo "$access" | grep -qE "rows=[1-9]" \
  || { echo "nothing reached access_log: ${access:-<no output>}" >&2; exit 1; }
echo "$access" | grep -q "revoked" \
  || { echo "the revoked attempt was not recorded as such: $access" >&2; exit 1; }
echo "access log: $access"

step "PASS"
# Explicit, so the exit status is this line and never whatever the EXIT trap
# happened to leave in $?.
exit 0
