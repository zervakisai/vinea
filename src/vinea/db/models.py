"""The schema. Five core tables, and one deliberate counter-example.

This module is ADR-001 written as DDL. Read it as two lists:

  Cannot be recomputed, therefore stored
    weather_observations  what the sky actually did -- gone if we drop it
    grower_config         the thresholds a human chose for a block
    advisories            what the model said, and everything needed to
                          attribute it later
    eval_runs             what a score was, at a point in time
    annotations           what a human thought

  Can be recomputed, therefore cache
    feature_cache         a pure function of observations x config. Safe to
                          TRUNCATE. It carries a comment saying so, because
                          the day someone treats it as truth is the day the
                          water balance has two sources.

The tables mirror the Pydantic contracts (`ingest.WeatherRow`, `deps.Deps`,
`contracts.DailyFarmAdvisory`) rather than restating them: the advisory legs
are stored as JSONB dumps, and `mapping.py` is the only code that converts
between the two shapes. Flattening the contracts into columns would fork the
schema -- one copy in contracts.py under `extra="forbid"`, one here -- and they
would drift.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow_column(**kwargs: object) -> Column:
    """A timestamptz defaulting to the database's clock, not the app's.

    `func.now()` is evaluated by Postgres, so rows written by a worker with a
    skewed clock still order correctly against each other.
    """
    return Column(DateTime(timezone=True), server_default=func.now(), **kwargs)


class ReviewerRole(enum.StrEnum):
    """Who reviewed an advisory -- a closed set the system branches on.

    An ENUM, not free text, because DESIGN.md's B2 is explicit that these are
    two queues and not one: an agronomist judges *correctness*, a grower judges
    *clarity* of the summary they read, and they will disagree. Code weights
    their disagreements differently when promoting golden cases, so a typo'd
    'agronimist' must fail at write time rather than quietly become a third
    category nothing handles.

    Contrast `WeatherObservation.source` below, which is deliberately TEXT.
    """

    agronomist = "agronomist"
    farmer = "farmer"


# ---------------------------------------------------------------------------
# 1. weather_observations -- ground truth, append-only
# ---------------------------------------------------------------------------


class WeatherObservation(SQLModel, table=True):
    """One hourly reading, from any source. The irreplaceable table.

    Mirrors `ingest.WeatherRow` field for field, and inherits its rule about
    missing data as a NOT NULL constraint: every *reading* is nullable, and
    `observed_at` is not. That is WeatherRow's "a missing cell degrades the
    hour, a missing timestamp disqualifies it" expressed in DDL.

    The natural key carries `kind` and `source` beyond the obvious
    (tenant, location, observed_at). A forecast *for* 3pm and what actually
    *happened* at 3pm are different facts about the same hour; collapsing them
    would let tonight's forecast overwrite last week's history and quietly
    corrupt the golden dataset a replay runs against. Keeping `source` in the
    key means the CSV fixture and the Open-Meteo feed can both hold an opinion
    about the same hour without fighting over one row.
    """

    __tablename__ = "weather_observations"
    __table_args__ = (
        UniqueConstraint(
            "tenant", "location", "observed_at", "kind", "source", name="uq_weather_observations_natural"
        ),
        Index("ix_weather_observations_lookup", "tenant", "location", "kind", "observed_at"),
        CheckConstraint("kind IN ('history', 'forecast')", name="ck_weather_observations_kind"),
    )

    id: int | None = Field(default=None, primary_key=True)

    tenant: str = Field(sa_column=Column(Text, nullable=False))
    location: str = Field(sa_column=Column(Text, nullable=False))
    observed_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))

    kind: str = Field(sa_column=Column(Text, nullable=False))
    # TEXT, not an ENUM, and that is ADR-002 showing up early: "new data source
    # = new adapter behind the same contract". An ENUM would make adding a BoM
    # feed an ALTER TYPE -- a schema migration to describe a code addition that
    # changes no shape. The set is open by design; reviewer_role's is not.
    source: str = Field(sa_column=Column(Text, nullable=False))

    # Every reading nullable (see class docstring). Columns mirror
    # ingest.WeatherRow's snake_case fields one-for-one.
    temp_c: float | None = Field(default=None, sa_column=Column(Float))
    humidity_pct: float | None = Field(default=None, sa_column=Column(Float))
    wind_ms: float | None = Field(default=None, sa_column=Column(Float))
    precip_mm: float | None = Field(default=None, sa_column=Column(Float))
    spray_index: float | None = Field(default=None, sa_column=Column(Float))
    et0_mm: float | None = Field(default=None, sa_column=Column(Float))
    dew_point_c: float | None = Field(default=None, sa_column=Column(Float))
    vpd_kpa: float | None = Field(default=None, sa_column=Column(Float))
    delta_t_c: float | None = Field(default=None, sa_column=Column(Float))
    wind_dir_deg: float | None = Field(default=None, sa_column=Column(Float))
    ghi_wm2: float | None = Field(default=None, sa_column=Column(Float))

    ingested_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


# ---------------------------------------------------------------------------
# 2. grower_config -- Deps as rows
# ---------------------------------------------------------------------------


class GrowerConfig(SQLModel, table=True):
    """`deps.Deps` as data, which makes "new crop = config change" literal.

    deps.py already argues that `Deps(kc=0.85, taw_mm=120)` is a complete "add
    an olive grove" PR. This table finishes the thought: it isn't a PR at all,
    it's an INSERT.

    Two things worth noticing about what is *not* here. `raw_mm` is absent -- it
    is `mad_fraction * taw_mm`, computed in `IrrigationFeatures`, and storing it
    would give the irrigation trigger a second home and a chance to disagree
    with itself (ADR-001 applies inside a row, not just across tables). And the
    tuple fields are flattened -- `deltat_ideal` and `wind_ideal_ms` each become
    two columns -- because SQL has no tuple; `mapping.py` reassembles them in one
    place.

    `valid_from`/`valid_to` exist because an advisory written in March must stay
    reproducible against March's thresholds. Config is not overwritten; a change
    closes the current row and opens a new one, and `advisories.deps_hash` pins
    which row was in play.
    """

    __tablename__ = "grower_config"
    __table_args__ = (
        UniqueConstraint("tenant", "location", "valid_from", name="uq_grower_config_version"),
        # A block has at most ONE open version. A partial unique index says so
        # and lets Postgres enforce it, so "two current configs" is impossible
        # rather than something the repository must remember not to do.
        Index(
            "uq_grower_config_open",
            "tenant",
            "location",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_grower_config_current", "tenant", "location", "valid_to"),
    )

    id: int | None = Field(default=None, primary_key=True)

    tenant: str = Field(sa_column=Column(Text, nullable=False))
    location: str = Field(sa_column=Column(Text, nullable=False))
    # Region as a config value on the tenant record, not a code path -- the
    # EU-residency point from DESIGN.md B1.
    region: str = Field(sa_column=Column(Text, nullable=False))

    # deps.Deps, field for field (tuples flattened). raw_mm intentionally absent.
    crop: str = Field(sa_column=Column(Text, nullable=False))
    irrigation_method: str = Field(sa_column=Column(Text, nullable=False))
    spray_sensitivity: str = Field(sa_column=Column(Text, nullable=False))
    kc: float = Field(sa_column=Column(Float, nullable=False))
    root_depth_m: float = Field(sa_column=Column(Float, nullable=False))
    taw_mm: float = Field(sa_column=Column(Float, nullable=False))
    mad_fraction: float = Field(sa_column=Column(Float, nullable=False))
    initial_depletion_mm: float = Field(sa_column=Column(Float, nullable=False))
    effective_rain_fraction: float = Field(sa_column=Column(Float, nullable=False))
    rain_skip_mm: float = Field(sa_column=Column(Float, nullable=False))
    refill_fraction: float = Field(sa_column=Column(Float, nullable=False))

    deltat_ideal_low: float = Field(sa_column=Column(Float, nullable=False))
    deltat_ideal_high: float = Field(sa_column=Column(Float, nullable=False))
    deltat_inversion_below: float = Field(sa_column=Column(Float, nullable=False))
    deltat_marginal_upper: float = Field(sa_column=Column(Float, nullable=False))
    wind_ideal_low_ms: float = Field(sa_column=Column(Float, nullable=False))
    wind_ideal_high_ms: float = Field(sa_column=Column(Float, nullable=False))
    spray_index_cutoff: float = Field(sa_column=Column(Float, nullable=False))
    spray_index_higher_is_better: bool = Field(sa_column=Column(Boolean, nullable=False))
    rain_fast_hours: int = Field(sa_column=Column(Integer, nullable=False))

    valid_from: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))
    valid_to: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    # Identifies the Deps this row renders to. Advisories store the same hash,
    # which is what makes "which thresholds produced this advice?" answerable a
    # year later.
    deps_hash: str = Field(sa_column=Column(Text, nullable=False, index=True))


# ---------------------------------------------------------------------------
# 3. advisories -- LLM output + provenance. The reason this package exists.
# ---------------------------------------------------------------------------


class Advisory(SQLModel, table=True):
    """One day's `DailyFarmAdvisory`, plus everything needed to attribute it.

    This is the table ADR-001 is really about. Re-running the graph tomorrow
    does not reproduce what the model said tonight: the model is
    non-deterministic, its version moves under you, and the prompt that produced
    this text may have been re-pointed since. So the output is stored, and so is
    the context that explains it.

    `UNIQUE (tenant, run_date)` is not decoration -- it *is* S3.2's idempotency
    key. A night that re-runs UPSERTs onto the same row instead of writing a
    second advisory, which is what makes the whole queue safely re-runnable.

    The legs are JSONB dumps of the contracts: `irrigation` and `spray` are
    `IrrigationAdvice`/`SprayAdvice`, and `reconciliation` is the coordinator's
    fields (summary, conflicts_resolved, overall_confidence) -- which sit flat on
    `DailyFarmAdvisory` but round-trip through a `Reconciliation`-shaped dict. The
    three confidences are *also* promoted to real columns: S6's quality monitor
    aggregates them across thousands of rows, and charting shouldn't reach through
    a JSONB path to do it. That is a deliberate, documented denormalisation --
    one direction only, written by mapping.py, never read back as truth.
    """

    __tablename__ = "advisories"
    __table_args__ = (
        UniqueConstraint("tenant", "run_date", name="uq_advisories_idempotency"),
        Index("ix_advisories_history", "tenant", "run_date"),
    )

    id: int | None = Field(default=None, primary_key=True)

    tenant: str = Field(sa_column=Column(Text, nullable=False))
    # The idempotency key with `tenant`: which nightly run this is.
    run_date: date = Field(sa_column=Column(Date, nullable=False))
    # The day being advised *about* -- normally run_date + 1. Distinct from
    # run_date because a backfill advises about an old day today. Maps to
    # DailyFarmAdvisory.date.
    target_date: date = Field(sa_column=Column(Date, nullable=False))

    irrigation: dict = Field(sa_column=Column(JSONB, nullable=False))
    spray: dict = Field(sa_column=Column(JSONB, nullable=False))
    reconciliation: dict = Field(sa_column=Column(JSONB, nullable=False))

    # Promoted out of JSONB for aggregation. See class docstring.
    irrigation_confidence: float | None = Field(default=None, sa_column=Column(Float))
    spray_confidence: float | None = Field(default=None, sa_column=Column(Float))
    overall_confidence: float | None = Field(default=None, sa_column=Column(Float))

    # --- provenance ---------------------------------------------------------
    # Together with dataset_version these are DESIGN.md B2's five drift tags:
    # when a score moves, exactly one of these moved with it, and the tags say
    # which. Nullable because S1 writes advisories before S4/S7 exist to fill
    # them -- a column that arrives empty is honest; a fabricated default is not.
    prompt_name: str | None = Field(default=None, sa_column=Column(Text))
    prompt_version: str | None = Field(default=None, sa_column=Column(Text))
    # registry | cache | fallback -- S7.2's ladder. Tells you whether this
    # advisory's wording came from the live prompt or yesterday's.
    prompt_source: str | None = Field(default=None, sa_column=Column(Text))
    model_id: str | None = Field(default=None, sa_column=Column(Text))
    deps_hash: str = Field(sa_column=Column(Text, nullable=False))
    code_sha: str | None = Field(default=None, sa_column=Column(Text))
    dataset_version: str | None = Field(default=None, sa_column=Column(Text))

    # S4.3 deep-links the UI to the trace via this.
    trace_id: str | None = Field(default=None, sa_column=Column(Text, index=True))
    # S3.5: features-only, no model was called.
    degraded: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default="false"))

    # The guardrail protects the grower; this column protects the measurement.
    # If the output_validator corrected the model before anything was logged, an
    # async eval would score the *corrected* answer and report success on a model
    # that got it wrong every time (DESIGN.md B2's circularity trap). This is the
    # only record of what the model actually said, not reconstructible from
    # anything else here. Written by S4.4; NULL until then.
    pre_correction_output: dict | None = Field(default=None, sa_column=Column(JSONB))

    created_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


# ---------------------------------------------------------------------------
# 4. eval_runs -- scores, keyed by the five drift tags
# ---------------------------------------------------------------------------


class EvalRun(SQLModel, table=True):
    """One evaluator's score for one run, tagged with everything that could
    have moved it.

    DESIGN.md B2: drift is two questions kept separate -- did the *model* change,
    or did the *inputs*? These five tags are how a moved score gets attributed,
    including the case where nothing about the model changed and the *oracle
    itself* did (a deliberate constant change like effective_rain_fraction should
    visibly move scores and be traceable to that change, not mistaken for model
    drift -- which is why deps_hash and code_sha are here and NOT NULL).
    """

    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_drift", "prompt_version", "model_id", "dataset_version", "code_sha"),
        Index("ix_eval_runs_evaluator", "evaluator", "started_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))

    # The five tags. All NOT NULL: an untagged score is not evidence.
    prompt_version: str = Field(sa_column=Column(Text, nullable=False))
    model_id: str = Field(sa_column=Column(Text, nullable=False))
    deps_hash: str = Field(sa_column=Column(Text, nullable=False))
    code_sha: str = Field(sa_column=Column(Text, nullable=False))
    dataset_version: str = Field(sa_column=Column(Text, nullable=False))

    evaluator: str = Field(sa_column=Column(Text, nullable=False))
    score: float = Field(sa_column=Column(Float, nullable=False))
    # Its own column rather than buried in `detail`: DESIGN.md B2 names this "the
    # number to keep near 100%", and a number you gate promotion on should be
    # queryable without parsing JSON.
    recall_should_irrigate: float | None = Field(default=None, sa_column=Column(Float))
    passed: bool = Field(sa_column=Column(Boolean, nullable=False))
    detail: dict | None = Field(default=None, sa_column=Column(JSONB))

    advisory_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("advisories.id", ondelete="SET NULL"), index=True),
    )


# ---------------------------------------------------------------------------
# 5. annotations -- two queues, not one
# ---------------------------------------------------------------------------


class Annotation(SQLModel, table=True):
    """Human feedback, tagged by which kind of human gave it.

    The `reviewer_role` ENUM is the point of the table. An agronomist judging
    correctness and a grower judging clarity are not two instances of "feedback"
    -- they check different things, they will disagree, and the disagreement is
    signal. Flattening them into one anonymous `reviewer` column would throw away
    the only field that says how to weight the disagreement when it becomes a
    golden case.
    """

    __tablename__ = "annotations"
    __table_args__ = (
        Index("ix_annotations_advisory", "advisory_id"),
        Index("ix_annotations_golden", "promoted_to_golden"),
        CheckConstraint("verdict IN ('agree', 'disagree', 'unclear')", name="ck_annotations_verdict"),
        CheckConstraint(
            "leg IS NULL OR leg IN ('irrigation', 'spray', 'reconciliation')",
            name="ck_annotations_leg",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    advisory_id: int = Field(
        sa_column=Column(Integer, ForeignKey("advisories.id", ondelete="CASCADE"), nullable=False)
    )

    reviewer_role: ReviewerRole = Field(
        sa_column=Column(Enum(ReviewerRole, name="reviewer_role"), nullable=False)
    )
    reviewer_id: str = Field(sa_column=Column(Text, nullable=False))
    verdict: str = Field(sa_column=Column(Text, nullable=False))
    # NULL means "about the advisory as a whole" -- the normal case for a grower
    # judging clarity.
    leg: str | None = Field(default=None, sa_column=Column(Text))
    comment: str | None = Field(default=None, sa_column=Column(Text))

    # How the eval set gets harder over time instead of static (B2).
    promoted_to_golden: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, server_default="false")
    )
    created_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


# ---------------------------------------------------------------------------
# The counter-example. Not truth. Safe to TRUNCATE.
# ---------------------------------------------------------------------------


class FeatureCache(SQLModel, table=True):
    """Deterministic features, cached. **This table is not a source of truth.**

    Everything in here is reproducible from `weather_observations` and a
    `grower_config` row by running `features.build_features`. It exists to save
    recomputation, nothing else.

        TRUNCATE feature_cache;

    is always safe. If that statement ever stops being safe, something has
    started treating a cache as a record and ADR-001 has been violated -- the fix
    is to find that reader, not to start backing this table up.

    It sits in the same module as the five real tables on purpose: ADR-001 is
    easier to internalise as a diff between two neighbours than as a paragraph.
    The keys tell you why -- a cache entry is identified by its *inputs* (tenant,
    run_date, deps_hash), because that is all it is.
    """

    __tablename__ = "feature_cache"

    tenant: str = Field(sa_column=Column(Text, primary_key=True))
    run_date: date = Field(sa_column=Column(Date, primary_key=True))
    # Part of the key, not a column beside it: features computed under different
    # thresholds are different features, not a stale version of the same ones.
    deps_hash: str = Field(sa_column=Column(Text, primary_key=True))

    features: dict = Field(sa_column=Column(JSONB, nullable=False))
    computed_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


# ---------------------------------------------------------------------------
# phase 8: the queue. Not Redis -- see ADR-003.
# ---------------------------------------------------------------------------


class AdvisoryTask(SQLModel, table=True):
    """One unit of overnight work: produce the advisory for (tenant, run_date).

    This table *is* the queue. The claim in ADR-003 is that a Postgres table
    plus `SELECT ... FOR UPDATE SKIP LOCKED` is a better fit here than Redis or
    Celery -- zero new infrastructure, and the state we already have to persist
    (which advisories exist, with what provenance) lives one JOIN away from the
    work that produces them.

    The natural key is `(tenant, run_date)`, the SAME key as `advisories`. That
    is deliberate and load-bearing: enqueuing a night is idempotent (S3.2), so a
    scheduler that fires twice, or a manual re-run, does not create a second
    task. Combined with the advisory UPSERT on the same key, the whole pipeline
    is re-runnable end to end.

    Retry and timeout state lives here rather than in the worker because the
    worker is stateless (ADR-003): everything needed to decide "try again, with
    backoff" or "give up" is columns, so any worker can pick up where a dead one
    left off.
    """

    __tablename__ = "advisory_tasks"
    __table_args__ = (
        UniqueConstraint("tenant", "run_date", name="uq_advisory_tasks_idempotency"),
        # The claim query filters status='queued' and run_after<=now(), newest
        # eligible first. A partial index on exactly that predicate keeps the
        # claim cheap even when the table is mostly 'done' rows.
        Index("ix_advisory_tasks_claim", "run_after", postgresql_where=text("status = 'queued'")),
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_advisory_tasks_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    tenant: str = Field(sa_column=Column(Text, nullable=False))
    run_date: date = Field(sa_column=Column(Date, nullable=False))

    # TEXT + CHECK, not a native ENUM: task status is a closed but *evolving* set
    # (real systems grow 'cancelled', 'dead_letter'). A CHECK enforces the closed
    # set exactly like an ENUM, but extending it is a one-line DROP/ADD CONSTRAINT,
    # where a native ENUM's `ALTER TYPE ... ADD VALUE` can't run in a transaction
    # and can never be removed.
    status: str = Field(sa_column=Column(Text, nullable=False, server_default="queued"))

    # Retry accounting. `attempts` counts *worker* attempts on the day -- NOT the
    # SDK's per-call ModelRetry attempts, a different layer that must not be
    # conflated (the double-retry footgun, S3.3). `max_attempts` is the worker's
    # own give-up threshold.
    attempts: int = Field(sa_column=Column(Integer, nullable=False, server_default="0"))
    max_attempts: int = Field(sa_column=Column(Integer, nullable=False, server_default="3"))

    # A re-enqueued task waits until `run_after` before it can be claimed again --
    # exponential backoff lives here, as data, so it survives a worker crash.
    run_after: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))

    # ONE deadline for the whole day, set at creation and NEVER extended (S3.3).
    # Checked on every attempt; once passed the task fails permanently regardless
    # of remaining attempts, so one stuck day can't consume the night's budget a
    # retry at a time.
    deadline_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    # Who holds the lease, and since when. `locked_at` is what a reaper uses to
    # detect a task whose worker died mid-run (lease expired) and return it to
    # the queue -- the state is in the row, so recovery needs no coordination.
    locked_by: str | None = Field(default=None, sa_column=Column(Text))
    locked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_error: str | None = Field(default=None, sa_column=Column(Text))

    advisory_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("advisories.id", ondelete="SET NULL")),
    )
    created_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


class QueueDepthSample(SQLModel, table=True):
    """A point-in-time snapshot of how deep the queue is. Metrics, in the DB.

    DESIGN.md B1 argues you autoscale this fleet on queue depth, not CPU, because
    the workers are I/O-bound on the model API and CPU sits idle right up until
    the queue backs up. That argument is only actionable if queue depth is a
    number you can *see* over time -- so the worker samples it into this table,
    and S6's operator dashboard charts it. "Autoscale on queue depth" stops being
    a slogan and becomes a line on a graph.

    Arguably a cache (derivable by COUNT(*) over advisory_tasks at each instant)
    -- but only if you never delete task rows, and a real system prunes completed
    tasks. The samples are the *history* that survives that pruning, so they're
    stored, not recomputed. ADR-001's test still applies: could you reconstruct
    this from surviving inputs? Once tasks are pruned, no -- so it's a row.
    """

    __tablename__ = "queue_depth_samples"
    __table_args__ = (Index("ix_queue_depth_samples_time", "sampled_at"),)

    id: int | None = Field(default=None, primary_key=True)
    sampled_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))
    queued: int = Field(sa_column=Column(Integer, nullable=False))
    running: int = Field(sa_column=Column(Integer, nullable=False))
    failed: int = Field(sa_column=Column(Integer, nullable=False))
    done: int = Field(sa_column=Column(Integer, nullable=False))
