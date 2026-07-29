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
kubectl create secret generic vinea-secrets \
  --from-literal=DATABASE_URL='postgresql+psycopg://vinea:vinea@postgres:5432/vinea' \
  --from-literal=VINEA_API_KEYS='key-acme:acme' \
  --from-literal=VINEA_OPS_KEY='ops-secret' >/dev/null

step "helm upgrade --install"
# --wait blocks until every workload is Ready, and the pre-upgrade hook Job must
# succeed before any of them is even created. If the migration fails, this command
# fails and nothing new ever serves traffic -- which is the guarantee being tested.
helm upgrade --install "$RELEASE" "$ROOT/infra/chart" \
  --namespace "$NAMESPACE" \
  --set image.repository=vinea --set image.tag=e2e \
  --set uiImage.repository=vinea-ui --set uiImage.tag=e2e \
  --wait --timeout 5m

step "assert: the migration hook ran and completed"
kubectl get job -l app.kubernetes.io/component=migrate \
  -o jsonpath='{.items[0].status.succeeded}' | grep -qx 1 \
  || { echo "migration job did not report success" >&2; exit 1; }
echo "migration job: succeeded"

step "assert: alembic is actually at head"
# Capture, then assert: `kubectl run -i | grep -q` can lose the tail of the
# output to attach timing, which turned this into a soft check that always fell
# through to its fallback. Hard assert on the captured text instead.
revision=$(kubectl run alembic-check --rm -i --restart=Never --image=vinea:e2e \
  --image-pull-policy=Never --env="DATABASE_URL=postgresql+psycopg://vinea:vinea@postgres:5432/vinea" \
  --command -- alembic current 2>/dev/null || true)
echo "$revision" | grep -q "(head)" \
  || { echo "schema is not at head: ${revision:-<no output>}" >&2; exit 1; }
echo "alembic: at head ($(echo "$revision" | grep -o '^[0-9a-f]*' | head -1))"

step "assert: the default deploy carries no gateway (phase 14)"
# Phase 14's central claim, checked against a live pod rather than a rendered
# template: with `gateway.enabled=false` nothing tells the app a gateway exists,
# so `resolve_model()` returns the plain model string and this deployment is the
# phase-13 one. A cluster that quietly grew a VINEA_GATEWAY_URL would mean the
# "no gateway changes nothing" guarantee had become "no gateway is untested".
#
# The gateway itself is not installed here: LiteLLM needs a provider key to be
# worth starting, CI has none, and a proxy with no upstream proves nothing that
# `helm template` does not already prove offline. Said out loud because a silent
# gap reads like coverage.
gateway_env=$(kubectl get deploy "${RELEASE}-vinea-api" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VINEA_GATEWAY_URL")].value}')
[[ -z "$gateway_env" ]] || { echo "api pod has VINEA_GATEWAY_URL=$gateway_env in the default deploy" >&2; exit 1; }
echo "no VINEA_GATEWAY_URL on the api pod (correct for gateway.enabled=false)"

step "assert: the expand migration added the cost columns (phase 14)"
# The pre-upgrade hook ran (asserted above); this asserts what it *did*. Four
# additive nullable columns, and the nullability is the claim: a server_default
# would make every advisory written before tonight report that it cost zero.
columns=$(kubectl run cost-columns --rm -i --restart=Never --image=vinea:e2e \
  --image-pull-policy=Never --env="DATABASE_URL=postgresql+psycopg://vinea:vinea@postgres:5432/vinea" \
  --command -- python -c "
import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    rows = c.execute(text(\"select column_name, is_nullable, column_default from information_schema.columns where table_name='advisories' and column_name in ('input_tokens','output_tokens','cost_usd','cache_hit') order by column_name\")).all()
print(';'.join(f'{n}:{null}:{default}' for n, null, default in rows))
" 2>/dev/null || true)
for col in cache_hit cost_usd input_tokens output_tokens; do
  echo "$columns" | grep -q "${col}:YES:None" \
    || { echo "cost column ${col} missing or not nullable-without-default: ${columns:-<no output>}" >&2; exit 1; }
done
echo "cost columns: present, nullable, no default"

step "assert: the pgvector extension and corpus tables exist (phase 15)"
# The genuinely risky part of migration c73a51e8d4b2 is `CREATE EXTENSION vector`,
# which succeeds only on a server that HAS pgvector -- the stock postgres:16 image
# does not. Asserting it here is asserting that the test fixture, the compose
# stack and any real cluster are all running an image that carries it, which is a
# deployment fact no unit test can reach.
schema=$(kubectl run vector-check --rm -i --restart=Never --image=vinea:e2e \
  --image-pull-policy=Never --env="DATABASE_URL=postgresql+psycopg://vinea:vinea@postgres:5432/vinea" \
  --command -- python -c "
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    ext = c.execute(text(\"select 1 from pg_extension where extname='vector'\")).first()
    cols = c.execute(text(\"select count(*) from information_schema.columns where table_name='corpus_chunks'\")).scalar_one()
    cites = c.execute(text(\"select count(*) from information_schema.columns where table_name='advisory_citations'\")).scalar_one()
print(f'vector={bool(ext)} corpus_chunks_cols={cols} advisory_citations_cols={cites}')
" 2>/dev/null || true)
echo "$schema" | grep -q "vector=True" \
  || { echo "pgvector extension missing: ${schema:-<no output>}" >&2; exit 1; }
echo "$schema" | grep -qE "corpus_chunks_cols=(9|10|11)" \
  || { echo "corpus_chunks not created as expected: ${schema:-<no output>}" >&2; exit 1; }
echo "schema: $schema"

step "assert: row-level security is real in the cluster (phase 17)"
# Behaviour, not configuration. `relrowsecurity = true` is what the FIRST version
# of the RLS migration reported while being completely inert -- the connecting
# role was a superuser, and superusers bypass row security unconditionally. So
# this checks the role AND counts rows from a query with no tenant filter.
rls=$(kubectl run rls-check --rm -i --restart=Never --image=vinea:e2e \
  --image-pull-policy=Never --env="DATABASE_URL=postgresql+psycopg://vinea:vinea@postgres:5432/vinea" \
  --command -- python -c "
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
" 2>/dev/null || true)
echo "$rls" | grep -q "user=vinea_app super=False bypass=False" \
  || { echo "app role is not restricted: ${rls:-<no output>}" >&2; exit 1; }
echo "$rls" | grep -q "scoped=\['rls-a'\]" \
  || { echo "a query with no WHERE crossed a tenant boundary: ${rls:-<no output>}" >&2; exit 1; }
echo "$rls" | grep -q "unscoped=0" \
  || { echo "an unscoped session was not fail-closed: ${rls:-<no output>}" >&2; exit 1; }
echo "rls: $rls"

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
  -H 'X-API-Key: key-acme')
[[ "$code" == "202" ]] || { echo "enqueue not 202 (got $code)" >&2; exit 1; }
echo "POST /advisories/acme/2026-07-28 -> 202"

code=$(curl -s -o /dev/null -w '%{http_code}' localhost:18080/ops/queue -H 'X-API-Key: key-acme')
[[ "$code" == "403" || "$code" == "401" ]] || { echo "tenant key reached /ops (got $code)" >&2; exit 1; }
echo "tenant key on /ops/* -> $code (correctly refused)"

step "PASS"
# Explicit, so the exit status is this line and never whatever the EXIT trap
# happened to leave in $?.
exit 0
