# syntax=docker/dockerfile:1.7
#
# Two images, one file (phase 13).
#
#   docker build --target app -t vinea:dev .      # API + worker
#   docker build --target ui  -t vinea-ui:dev .   # Streamlit dashboard
#
# Why two and not one: the UI stack measures 220 MB installed (pyarrow 119,
# pandas 48, numpy 24, streamlit 29) and neither the API nor the worker imports
# any of it. ADR-005 already forbids anything upstream from importing the UI, so
# splitting the image follows a line the architecture had already drawn -- see the
# `ui` extra in pyproject.toml.
#
# The size budget is aimed at `app`, the image that runs three of the four
# workloads (API, worker CronJob, migration hook). `ui` cannot meet it and is not
# asked to: pyarrow alone is over a third of the budget.
#
# The budget is aimed at, not met. Phase 13 measured 309 MB against a 300 MB
# target; rebuilt from that same tag on 2026-07-29 it is 389 MB, with no change
# to this repository. PYTHON_VERSION below resolves to a floating tag and the
# base image moved. Pinning it by digest is the fix and it is deliberately NOT
# applied here -- phase 14 records the finding instead, because a size claim that
# rotted silently teaches more than a size claim that cannot.

ARG PYTHON_VERSION=3.12

# Which provider SDK the image carries. VINEA_MODEL picks one provider at run
# time, so shipping all five costs ~50 MB for four that can never be reached.
# Default matches config.MODEL. Build another with --build-arg PROVIDER=openai.
ARG PROVIDER=anthropic

# phase 14: add the OpenAI-wire SDK the gateway needs, whatever PROVIDER says.
# `--build-arg GATEWAY=1` for an image that will be pointed at LiteLLM. Empty (the
# default) keeps the phase-13 image exactly as it was, which is the point: a
# gateway is opt-in all the way down to the bytes shipped.
ARG GATEWAY=



# --------------------------------------------------------------------------- #
# builder -- resolve and install into /app/.venv                              #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Pinned, not :latest -- a floating toolchain in a build that produces a
# deployable artifact is how "it built yesterday" happens.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, project second: the lockfile changes rarely and `src/`
# changes every commit, so this ordering keeps the expensive layer cached.
COPY pyproject.toml uv.lock README.md ./
ARG PROVIDER
ARG GATEWAY
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra "${PROVIDER}" ${GATEWAY:+--extra gateway}

COPY src/ ./src/
# --no-editable: install the project as a real wheel, so the venv is
# self-contained and can be copied without carrying `src/` into the runtime.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra "${PROVIDER}" ${GATEWAY:+--extra gateway}

# NOTE: this stage used to bake a ~30 MB embedding model and set HF_HUB_OFFLINE,
# so a nightly CronJob never reached out to a model hub at 02:00. All of it went
# with the dense retriever (ADR-011): full-text search needs no model, and the
# image lost 258 MB -- 649 back down to 391.
#
# The UI variant is the same resolution plus one extra, so it reuses every layer
# above rather than resolving from scratch.
FROM builder AS builder-ui
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra "${PROVIDER}" ${GATEWAY:+--extra gateway} --extra ui

# --------------------------------------------------------------------------- #
# runtime-base -- everything both images share                                #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base

# Non-root, with a fixed uid: a rootless container whose uid is assigned at build
# time makes filesystem permissions on a mounted volume unpredictable.
RUN groupadd --system --gid 1001 vinea \
 && useradd --system --uid 1001 --gid vinea --home-dir /app --no-create-home vinea

# NOTE, measured: deleting /usr/share/doc and /usr/share/man here saves nothing.
# Those files live in the BASE image's layer, and layers are additive -- a later
# RUN that deletes them adds whiteout entries on top and the bytes stay in the
# image. You cannot delete your way out of a base image's size; you can only pick
# a smaller base. The trim was tried, measured at 0 MB saved, and removed.

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Migrations ship in the image, because the migration hook (phase 13) runs
# `alembic upgrade head` from this same artifact -- the schema and the code that
# expects it are versioned together by construction.
COPY --chown=vinea:vinea alembic.ini ./
COPY --chown=vinea:vinea migrations/ ./migrations/
# The committed capture: the worker's CsvSource globs this directory. It also
# carries data/corpus/, which `python -m vinea.rag ingest` reads (phase 15).
COPY --chown=vinea:vinea data/ ./data/

USER vinea

# --------------------------------------------------------------------------- #
# app -- API, worker, and the migration hook                                  #
# --------------------------------------------------------------------------- #
FROM runtime-base AS app

COPY --from=builder --chown=vinea:vinea /app/.venv /app/.venv

EXPOSE 8000

# `/health` returns 200 even when the database is unreachable -- deliberately, so
# that a dead database is *reported* rather than raised (api/main.py). That makes
# a status-only healthcheck theatre: it would pass with no database at all. So
# this one reads the body and asserts on it.
#
# Note the split this forces, which Kubernetes then makes explicit: "the process
# is alive" and "this pod can serve traffic" are different questions. Docker has
# one hook, and compose's `service_healthy` means *ready*, so readiness is the
# useful semantic here. The chart uses /health for liveness and /ready for
# readiness instead.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "\
import json,sys,urllib.request;\
r=urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4);\
sys.exit(0 if r.status==200 and json.load(r).get('database')=='ok' else 1)"]

# PORT is read here, in the CMD, and not in config.py -- keeping the
# platform's injected variable out of `src/vinea/` is what lets phase 13 leave
# the core untouched (see docs/phases/13-*.md, "The invariant").
CMD ["sh", "-c", "exec uvicorn vinea.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

# --------------------------------------------------------------------------- #
# ui -- the Streamlit dashboard                                               #
# --------------------------------------------------------------------------- #
FROM runtime-base AS ui

COPY --from=builder-ui --chown=vinea:vinea /app/.venv /app/.venv
# Streamlit runs a *script path*, so this image needs the source tree as well as
# the installed wheel. It is ~200 KB next to a 400 MB venv, and it keeps the
# command identical to the one phase 11 documents.
COPY --chown=vinea:vinea src/ ./src/

EXPOSE 8501

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "\
import sys,urllib.request;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=4).status==200 else 1)"]

CMD ["sh", "-c", "exec streamlit run src/vinea/ui/app.py \
--server.address=0.0.0.0 --server.port=${PORT:-8501} \
--server.headless=true --browser.gatherUsageStats=false"]
