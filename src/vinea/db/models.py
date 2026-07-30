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

from pgvector.sqlalchemy import Vector
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

# Width of the reserved `corpus_chunks.embedding` column. A constant rather than a
# lookup: a migration cannot ask a model at runtime how wide its output is, so
# changing embedder would be a schema change and should feel like one.
EMBEDDING_DIM = 256


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
    # phase 18. The SLO is "an advisory by 06:00 LOCAL", and a vineyard in Nemea
    # and one in Mendoza do not share a morning. An IANA zone name, not an offset:
    # offsets move twice a year and a stored one is wrong for half of it.
    #
    # On the tenant record rather than in `Deps`, alongside `region` and for the
    # same reason -- it describes where a grower is, not how a crop behaves, and
    # `Deps` is the crop's contract. It is also protected by the invariant, which
    # settles the question rather than merely arguing it.
    #
    # Nullable with a default of UTC applied on read: existing rows predate the
    # column, and a fabricated local time would silently move every SLI. Empty
    # means "we do not know this grower's morning", and the SLI says so.
    timezone: str | None = Field(default=None, sa_column=Column(Text))

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

    # --- cost (phase 14) ----------------------------------------------------
    # ADR-001's test, applied to money: could this be recomputed from surviving
    # inputs? The tokens could. The *price in force the night the call was made*
    # could not -- providers reprice, and `tokens x price_today` computed in March
    # against a January advisory returns a number that was never charged to
    # anyone. So cost is stored as evidence, not derived on read.
    #
    # All four are NULL whenever nothing in the path knew the answer: no model was
    # called (the router skipped it, or there was no key), or no gateway was
    # configured to report cost. A zero would claim a call was made and was free.
    # And `cost_per_token` is deliberately absent for the same reason `raw_mm` is
    # absent from grower_config -- it is cost_usd / (input_tokens + output_tokens),
    # and a derived value with its own column gets a chance to disagree with itself.
    input_tokens: int | None = Field(default=None, sa_column=Column(Integer))
    output_tokens: int | None = Field(default=None, sa_column=Column(Integer))
    cost_usd: float | None = Field(default=None, sa_column=Column(Float))
    # True only when EVERY model call in the run was served from the gateway's
    # exact-match cache -- the column answers "did this advisory cost anything
    # new?", and two thirds cached still bought a completion.
    cache_hit: bool | None = Field(default=None, sa_column=Column(Boolean))
    # phase 16. Characters of the assembled request, recorded at the same instant
    # as `input_tokens` and NULL whenever that is. The pair is what makes a
    # chars-per-token calibration a measurement; either number alone restates an
    # assumption. Not recomputable -- the retrieved passages that night depend on
    # a corpus that may since have been re-chunked (ADR-001, same argument as
    # `advisory_citations.locator`).
    context_chars: int | None = Field(default=None, sa_column=Column(Integer))
    # What the provider reported as serving the call, when a gateway is in the
    # path. `model_id` above records what we *asked* for -- which behind a gateway
    # is an alias like `vinea-nightly`, and an alias does not identify a model a
    # year later. This is the concrete answer when the gateway supplies one, and
    # NULL when it does not.
    served_model: str | None = Field(default=None, sa_column=Column(Text))

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


class CorpusChunk(SQLModel, table=True):
    """One retrievable passage of FAO-56, with its vector and its locator.

    **A cache, not truth** — the same category as `feature_cache`, and for the
    same reason: every row here is reproducible by running
    `scripts/fetch_corpus.py` and re-embedding. `TRUNCATE corpus_chunks;` is
    always safe. What is NOT reproducible is which passages were shown to the
    model on a given night, and that is why `advisory_citations` below is a
    separate table rather than a join through this one.

    The embedding width is a constant (`rag.embedding.EMBEDDING_DIM`), not a
    lookup: a migration cannot ask a model at runtime how wide its output is, so
    changing embedder is a schema change and should feel like one.

    `text` is stored beside the vector deliberately. The lexical half of the
    hybrid query runs `to_tsvector` over this column, so meaning-search and
    exact-token search are two `SELECT`s against one table in one database —
    which is what makes hybrid retrieval affordable here at all (ADR-008).
    """

    __tablename__ = "corpus_chunks"
    __table_args__ = (
        # (source, chunk_id) is the natural key: re-ingesting the same corpus
        # overwrites rather than duplicating, exactly like the advisory upsert.
        UniqueConstraint("source", "chunk_id", name="uq_corpus_chunks_natural"),
        # The lexical index. GIN over the expression rather than a stored
        # tsvector column: one fewer column to keep in sync, and the expression
        # in the index must match the one in the query or Postgres silently
        # ignores the index and sequential-scans.
        Index(
            "ix_corpus_chunks_fts",
            text("to_tsvector('english', text)"),
            postgresql_using="gin",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    # Which corpus this came from. TEXT and open-ended, the same reasoning as
    # `weather_observations.source`: a second document is a new adapter's worth
    # of work, not a schema migration.
    source: str = Field(sa_column=Column(Text, nullable=False))
    chunk_id: int = Field(sa_column=Column(Integer, nullable=False))

    chapter: str = Field(sa_column=Column(Text, nullable=False))
    section: str = Field(sa_column=Column(Text, nullable=False, server_default=""))
    # What a citation shows a human so they can go and check. A passage without
    # one is worse than no passage: it moves a claim from unverified to falsely
    # verified.
    locator: str = Field(sa_column=Column(Text, nullable=False))
    text_: str = Field(sa_column=Column("text", Text, nullable=False))

    # Reserved, and currently never written. ADR-008 built dense retrieval here;
    # ADR-011 removed it after measuring that lexical search alone scored better
    # (0.78 vs 0.70 recall@3) on questions phrased the way a grower asks them.
    #
    # The columns stay rather than being dropped because ADR-008's revisit trigger
    # is specific and still stands: a corpus past roughly 10^5 chunks, or one
    # spanning languages, is where vectors start to pay. Keeping them makes that
    # an ingest away instead of a migration away, and a nullable unused column
    # costs nothing but this comment.
    embedding: list[float] | None = Field(default=None, sa_column=Column(Vector(EMBEDDING_DIM)))
    embedding_model: str | None = Field(default=None, sa_column=Column(Text))

    ingested_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


class AdvisoryCitation(SQLModel, table=True):
    """Which passages were shown to the model when producing one advisory.

    Read the table name carefully against what it stores: these are the passages
    the retriever **supplied**, not the ones the model claims to have **used**.
    That distinction is the phase's central honesty, and the choice was between:

      * ask the model which sources it used — a stronger claim, and a *self
        report*. Phase 12 spent a whole phase establishing that self-report is
        not evidence, and a model can name a citation it never read.
      * record what retrieval put in front of it — a weaker claim, and a *fact
        about the run*. It cannot be gamed, needs no model cooperation, and is
        reproducible from this table alone.

    The second, and the UI must say "sources shown to the model" rather than
    "sources used", because the difference is the entire epistemic content.

    Why a table and not a field on `DailyFarmAdvisory`: the contract is protected
    (phase 4's invariant), and more to the point a citation is *about* the
    advisory the way `trace_id`, `model_id` and `cost_usd` are about it — which
    `repository.get_advisory_row` already decided in phase 6. Shaped like
    `annotations` so "which passages get cited most" is a query, not a JSONB scan.
    """

    __tablename__ = "advisory_citations"
    __table_args__ = (
        UniqueConstraint("advisory_id", "leg", "chunk_id", name="uq_advisory_citations_natural"),
        Index("ix_advisory_citations_advisory", "advisory_id"),
        Index("ix_advisory_citations_chunk", "chunk_id"),
        CheckConstraint(
            "leg IN ('irrigation', 'spray', 'reconciliation')",
            name="ck_advisory_citations_leg",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    advisory_id: int = Field(
        sa_column=Column(Integer, ForeignKey("advisories.id", ondelete="CASCADE"), nullable=False)
    )
    # NOT NULL here, unlike `annotations.leg`. A citation is always retrieved for
    # a specific question -- there is no "cited the advisory as a whole".
    leg: str = Field(sa_column=Column(Text, nullable=False))

    # SET NULL, not CASCADE. `corpus_chunks` is a cache and may be truncated or
    # re-chunked at any time; what was cited on a given night is not recomputable
    # and must survive that. CASCADE would delete the whole citation row --
    # including the locator below, which exists precisely so a reader can still
    # find the passage after the index is rebuilt.
    chunk_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("corpus_chunks.id", ondelete="SET NULL")),
    )
    # Denormalised from corpus_chunks, and it is the durable half of the pair:
    # `chunk_id` points into a cache, `locator` is the human-readable citation.
    # After a re-ingest the id may be NULL and this still says "Chapter 8".
    locator: str = Field(sa_column=Column(Text, nullable=False))
    rank: int = Field(sa_column=Column(Integer, nullable=False))

    created_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))


class ApiRequestSample(SQLModel, table=True):
    """One timed API request. The read-latency SLI's raw data.

    Every request, not a histogram, and the traffic profile is what makes that the
    right choice rather than a lazy one: a nightly-advisory product serves the
    grower-facing read a few hundred times a day, so a week is thousands of rows.
    At that size an exact `percentile_cont(0.95)` is cheaper to reason about than
    bucket boundaries, and it keeps the SLI a SQL query (ADR-010) rather than a
    number only a metrics backend can produce.

    No `tenant` column, so no row policy: the SLO is fleet-wide, and adding a
    tenant here would make a latency measurement a per-tenant secret for no gain.

    `route` is the *template* (`/advisories/{tenant}/{run_date}`), never the
    resolved path. Storing resolved paths would make every URL its own series and
    put tenant names in a table that has no policy protecting them.
    """

    __tablename__ = "api_request_samples"
    __table_args__ = (Index("ix_api_request_samples_route_time", "route", "observed_at"),)

    id: int | None = Field(default=None, primary_key=True)
    observed_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))
    route: str = Field(sa_column=Column(Text, nullable=False))
    method: str = Field(sa_column=Column(Text, nullable=False))
    status_code: int = Field(sa_column=Column(Integer, nullable=False))
    duration_ms: float = Field(sa_column=Column(Float, nullable=False))


class SLOBreach(SQLModel, table=True):
    """One recorded breach of one objective. History, not alerting.

    `python -m vinea.slo check` writes a row when an objective is not met. The
    distinction from an alert matters: nothing here notifies anyone, and ADR-010
    declined a notification path until somebody is on a rota.

    What this buys that a dashboard query cannot is *duration*. "Are we in breach"
    is answerable from the samples; "how long have we been" is not, because
    `api_request_samples` is prunable and an advisory's absence leaves no row at
    all. A breach that has persisted for nine days is a different conversation
    from one that started this morning, and only a stored history distinguishes
    them.

    No `tenant`: every objective is fleet-wide.
    """

    __tablename__ = "slo_breaches"
    __table_args__ = (Index("ix_slo_breaches_objective_time", "objective", "detected_at"),)

    id: int | None = Field(default=None, primary_key=True)
    detected_at: datetime | None = Field(default=None, sa_column=_utcnow_column(nullable=False))
    objective: str = Field(sa_column=Column(Text, nullable=False))
    # The measured value, and NULL when the objective could not be measured at all
    # -- which is itself worth recording, because an SLI that stopped reporting
    # looks like a healthy one on every chart.
    value: float | None = Field(default=None, sa_column=Column(Float))
    target: float = Field(sa_column=Column(Float, nullable=False))
    sample_size: int = Field(sa_column=Column(Integer, nullable=False))
    budget_exhausted: bool | None = Field(default=None, sa_column=Column(Boolean))


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
