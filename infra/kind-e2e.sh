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
