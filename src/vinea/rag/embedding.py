"""Which embedder, and why the *worse* one was chosen deliberately.

The obvious choice is a hosted embedding model through the phase-14 gateway:
better vectors, no new dependency, and cost accounting for free. It was rejected,
and the reason is not embedding quality.

Phase 12 established that a claim without a gate rots. Retrieval quality is
exactly that kind of claim — it degrades one chunking change at a time and
nothing ever goes red. So `recall@k` has to be gated in CI, and CI has no
provider key (phase 14 established that too: it is why the gateway is not
deployed in the e2e). An embedder that needs a secret makes the gate unrunnable,
and an ungated retrieval pipeline is one that silently rots.

So: **a static embedding model.** `model2vec`'s `potion-base-8M` is a distilled
token matrix plus pooling — inference is a tokenizer lookup and a mean, in numpy,
with no torch anywhere. About 30 MB of weights, and it runs in CI with no secret.

The trade is real and stated rather than hidden: these vectors are worse than a
transformer's. Two things pay for that. The lexical half of the hybrid query
carries the exact-token queries where dense retrieval is weakest anyway (`RAW`,
`Kc`, `ETo`, `Table 12`), and the gate means a regression is *visible*. A better
embedder behind a secret would score higher on a number nobody could check.

`HashEmbedder` exists so the retrieval *mechanics* — fusion, filters, citation
binding, the fail-open floor — are testable with no model and no network at all.
It embeds nothing meaningfully and must never be used to measure recall; the
tests that use it assert plumbing, and the tests that measure recall skip without
the real model.
"""

from __future__ import annotations

import hashlib
import os
from typing import Protocol, runtime_checkable

# potion-base-8M's output width. A constant rather than a lookup because it is
# also the pgvector column width, and a migration cannot ask a model at runtime.
EMBEDDING_DIM = 256

DEFAULT_MODEL = "minishlab/potion-base-8M"


@runtime_checkable
class Embedder(Protocol):
    """Text in, unit-norm vectors out. The seam ADR-002 would recognise."""

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(vector: list[float]) -> list[float]:
    magnitude = sum(component * component for component in vector) ** 0.5
    if magnitude == 0:
        return vector
    return [component / magnitude for component in vector]


class StaticEmbedder:
    """`model2vec` static embeddings — no torch, no network at inference.

    The model downloads once from the Hugging Face hub and is cached. That is a
    network dependency at *first use*, not at every use, and it needs no
    credential — which is the property that lets the recall gate run in CI.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        from model2vec import StaticModel

        self._model = StaticModel.from_pretrained(model_name)
        self.model_name = model_name

    def encode(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        vectors = self._model.encode(texts, show_progress_bar=False)
        vectors = np.asarray(vectors, dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        # A zero vector is possible for input the tokenizer reduces to nothing.
        # Dividing by zero would put NaN into a pgvector column, where it poisons
        # every distance it takes part in.
        norms[norms == 0] = 1.0
        return (vectors / norms).tolist()


class HashEmbedder:
    """Deterministic nonsense, for testing the machinery around the model.

    Same text always yields the same vector, different text almost always yields
    a different one — enough for exact-match behaviour, fusion ordering and the
    fail-open floor. It carries no semantics whatsoever, so a recall number
    measured with this is a measurement of nothing. The tests that use it say so.
    """

    model_name = "hash-stub"

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Stretch 32 bytes to EMBEDDING_DIM components, deterministically.
            raw = (digest * (EMBEDDING_DIM // len(digest) + 1))[:EMBEDDING_DIM]
            vectors.append(_normalise([byte / 255.0 - 0.5 for byte in raw]))
        return vectors


def get_embedder(model_name: str | None = None) -> Embedder:
    """The configured embedder, or a clear failure.

    Deliberately NOT falling back to `HashEmbedder` when `model2vec` is missing.
    Every other fail-open path in this system degrades toward a *correct but
    lesser* answer — the deterministic advisory, the bundled prompt, no
    citations. Silently substituting a meaningless embedder degrades toward
    confident nonsense: retrieval would return passages, they would be unrelated,
    and nothing would look wrong.
    """
    name = model_name or os.getenv("VINEA_EMBEDDING_MODEL") or DEFAULT_MODEL
    if name == HashEmbedder.model_name:
        return HashEmbedder()
    try:
        return StaticEmbedder(name)
    except ImportError as exc:  # pragma: no cover - dependency-shape failure
        raise RuntimeError(
            "model2vec is not installed. Install the rag extra (`uv sync`) or set "
            "VINEA_EMBEDDING_MODEL=hash-stub for mechanics-only work."
        ) from exc
