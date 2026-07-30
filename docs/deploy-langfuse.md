# Tracing with Langfuse

Vinea exports OpenTelemetry spans over OTLP/HTTP. Langfuse is where they land:
one trace per advisory, showing the graph's node spans and the agents' model calls
in one tree, with `advisories.trace_id` deep-linking the UI to it.

Turning it off is a supported state. With no `LANGFUSE_HOST` and keys,
`configure_tracing` builds no exporter, `trace_id` stays NULL, and advisories are
produced exactly as before. Telemetry off is a deployment choice, not an error.

## What the trace is for

The span tree is the architecture, observed rather than asserted:

```
advisory.run                               ← root, carries the provenance tags
└─ run graph vineyard_advisor
   ├─ run node FeatureBuilderNode          ← no GENERATION child. Ever.
   ├─ run node IrrigationNode
   │  └─ irrigation_agent run
   │     └─ chat <model>                   GENERATION
   ├─ run node SprayNode
   │  └─ spray_agent run
   │     └─ chat <model>                   GENERATION
   └─ run node CoordinatorNode
      └─ coordinator_agent run
         └─ chat <model>                   GENERATION
```

`FeatureBuilderNode` computes every number a grower sees and has no model call
beneath it. `tests/test_langfuse_live.py` asserts that against the *exported*
tree — not against a local span list — because the claim is about what an operator
can see at 03:00, not about what the code intended.

## Locally

```bash
docker compose --profile langfuse up -d

export LANGFUSE_HOST=http://localhost:3000
export LANGFUSE_PUBLIC_KEY=pk-lf-local-vinea
export LANGFUSE_SECRET_KEY=sk-lf-local-vinea
```

Those keys are real, not placeholders: the compose stack provisions an org, a
project, a login (`dev@vinea.local` / `vinea-local-pw`) and that keypair headlessly
on first boot, so there is no interactive signup step.

Verify:

```bash
curl -s localhost:3000/api/public/health          # {"status":"OK","version":"3.x"}
uv run pytest tests/test_langfuse_live.py -v      # 3 passed
```

Then open <http://localhost:3000> and look at a trace.

**Give it a minute on first boot.** Langfuse v3 runs ClickHouse migrations at
startup, and the web service answers before they finish. A trace posted in that
window is accepted and never appears.

### Four services, and why each is load-bearing

| service | why it cannot be dropped |
|---|---|
| `langfuse-web` | accepts OTLP spans, serves the UI and the API |
| `langfuse-worker` | drains the Redis queue into ClickHouse. **Without it traces are accepted and never appear** — the most confusing possible failure |
| `langfuse-db` (Postgres) | projects, users, prompts, API keys |
| `langfuse-clickhouse` | the traces themselves |
| `langfuse-redis` | the ingestion queue between web and worker |
| `langfuse-minio` | span payload blobs (S3-compatible) |

This is the cost ADR-004 accepted when it chose to self-host rather than send
grower content to a vendor, and the reason ADR-010 initially recorded "Langfuse is
not deployed" as a permanent debt: four stateful services is ADR-003's argument
against a second one, several times over.

## In a cluster

Langfuse is **not** part of the vinea chart, and should not be. It is a separate
product with its own Helm chart; vendoring it in would make `helm upgrade vinea`
responsible for someone else's database migrations.

Deploy it alongside, from the official chart:

```bash
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm install langfuse langfuse/langfuse --namespace langfuse --create-namespace \
  --set langfuse.salt.value=… \
  --set langfuse.encryptionKey.value=… \
  --set langfuse.nextauth.secret.value=…
```

Then point vinea at it. The host is not a secret and goes in values; the keys are
and go in the Secret the chart already reads:

```bash
kubectl create secret generic vinea-secrets \
  --from-literal=DATABASE_URL=… \
  --from-literal=VINEA_API_KEYS=… \
  --from-literal=VINEA_OPS_KEY=… \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-… \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-…

helm upgrade --install vinea infra/chart \
  --set langfuse.host=http://langfuse-web.langfuse.svc.cluster.local:3000
```

All three must be present. Two out of three builds no exporter at all rather than a
half-configured one — a partially wired tracer that silently drops spans is worse
than none, because it looks like it is working.

**Mint real keys through the UI.** The compose keypair is a local-dev convenience
baked into a tracked file; using it in a cluster would put a known credential in
front of your grower traces.

### Data residency

ADR-004 self-hosts specifically so grower content does not leave infrastructure you
operate. Two things follow:

- `include_content=False` is the default in `configure_tracing`, so span payloads
  carry *that* a model was called, its token cost and its version — never the
  prompts, which contain depletion figures and block locations.
- Deploy Langfuse in the same region the `region` validation in `infra/tofu/`
  enforces for everything else. A trace store outside the EU defeats a residency
  rule the rest of the stack keeps.

## The e2e does not start Langfuse

`infra/kind-e2e.sh` deploys vinea without tracing, and that is deliberate rather
than an omission. Standing up four stateful services in every pipeline run to
re-prove an export path that `tests/test_langfuse_live.py` already covers is a poor
trade — and the e2e's job is the deploy path: the migration hook, the probes, the
RLS enforcement, the SLI recording.

The consequence is stated in ADR-010: **a cluster deployed by the e2e has
`trace_id` NULL.** A cluster deployed with the values above does not.
