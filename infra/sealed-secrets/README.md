# Secrets

The house rule is that secrets never live in tracked files. In Kubernetes the
manifests **are** tracked, so the rule needs a mechanism rather than discipline —
a `kind: Secret` in a chart is base64, which is an encoding, not encryption, and
`base64 -d` is not an attack.

## The mechanism

[Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets): a controller in
the cluster holds a private key; `kubeseal` encrypts with the public half. The
**SealedSecret** is safe to commit — only that cluster's controller can decrypt
it — and the controller unseals it into an ordinary `Secret` that workloads read.

The chart never templates a Secret. It references one by name
(`values.yaml: secret.name`), and how that name comes to exist is this file's
problem, not the chart's.

```bash
# once per cluster
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets -n kube-system

# per secret: create locally, seal, throw the plaintext away, commit the sealed form
kubectl create secret generic vinea-secrets \
  --dry-run=client -o yaml \
  --from-literal=DATABASE_URL='postgresql+psycopg://vinea:...@10.0.0.3:5432/vinea' \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...' \
  | kubeseal --format yaml > infra/sealed-secrets/vinea-secrets.yaml

kubectl apply -f infra/sealed-secrets/vinea-secrets.yaml
```

Note the pipe: the plaintext Secret is never written to disk. `kubectl create
--dry-run=client -o yaml | kubeseal` is one process boundary, not two files.

**API keys are not in here.** They are rows in `api_keys` (ADR-012), minted with
`python -m vinea.keys issue` after the migration has run. Sealing them into this
Secret would work and would give back the property the table exists for: revoking
one would mean re-sealing, committing, applying, and restarting every pod that
reads it.

## Why this and not External Secrets Operator

ESO is the better answer at a company — rotation without a commit, one place to
audit access, and the secret never has a second home. It was rejected here for
one reason: it needs a cloud secret manager behind it, and therefore a billing
account. This phase's binding constraint is that everything runs free, including
on a laptop's `kind` cluster. Sealed Secrets needs nothing but the cluster.

Revisit this when the deployment stops being ephemeral. The seam is small: both
produce a `Secret` the chart looks up by name.

## What this costs — read before relying on it

**Rotation is a commit.** Changing an API key means re-sealing and pushing. There
is no rotate-without-deploy story, which is precisely the property B3's prompt
registry was built to have for prompts. Secrets do not get that here.

**The controller's private key becomes a backup target.** Lose it — cluster
rebuilt, namespace deleted, disaster recovery to a fresh cluster — and every
SealedSecret in this repository is unrecoverable ciphertext. Not "hard to
recover": the plaintext was deliberately never written down. Back it up, or
accept that recovery means re-issuing every credential from its source:

```bash
kubectl get secret -n kube-system \
  -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.backup
# ...which is itself a plaintext private key. Store it where you store those.
```

That last line is the honest shape of the trade. The mechanism moves the secret
out of git and into one key; the key is now the thing that must not be lost, and
its backup is exactly as sensitive as everything it protects.

**Sealed to one cluster.** A SealedSecret encrypted for cluster A is inert on
cluster B (by design — it is what makes committing it safe). Multi-cluster means
sealing per cluster, or the controller's key being copied between them, which
gives back some of what the scheme bought.

## The ephemeral cluster does none of this

`infra/kind-e2e.sh` creates its Secret imperatively with `kubectl create secret`,
from literals, in the script. That is fine and it is not a lapse: the cluster is
created and destroyed by the same script, the values are `vinea/vinea` against a
throwaway Postgres, and none of it is a credential to anything. The rule is about
secrets; those are not secrets.

The important part is that the *chart* cannot tell the difference. It reads
`secret.name` either way.
