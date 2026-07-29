"""phase 6 -- tests-as-specification for the persistence layer.

These need a real Postgres (they skip without one) because the things worth
testing here are the things SQLite would lie about: ON CONFLICT against a named
constraint, JSONB, and a native ENUM. A "fast" in-memory database that doesn't
support the feature you're relying on is not a test, it's a rehearsal.

The theme is ADR-001. Most of these assert some form of "the thing you cannot
recompute survived the round trip, and the thing you can was not mistaken for
truth."
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError
from sqlmodel import select

from vinea.contracts import (
    DailyFarmAdvisory,
    IrrigationAdvice,
    SprayAdvice,
    SprayWindow,
)
from vinea.db import repository
from vinea.db.mapping import (
    deps_hash,
    observation_to_weather_row,
    row_to_advisory,
    weather_row_to_observation,
)
from vinea.db.models import Advisory, GrowerConfig, WeatherObservation
from vinea.deps import WINE_GRAPES, Deps
from vinea.ingest import WeatherRow

pytestmark = pytest.mark.db

TENANT = "acme-vineyards"
LOCATION = "block-7"
RUN_DATE = date(2025, 2, 8)
TARGET_DATE = date(2025, 2, 9)


def _advisory(*, depletion: float = 150.0, plan: str = "irrigate at dawn") -> DailyFarmAdvisory:
    return DailyFarmAdvisory(
        date=TARGET_DATE,
        irrigation=IrrigationAdvice(
            target_date=TARGET_DATE,
            should_irrigate_tomorrow=True,
            recommended_depth_mm=18.0,
            current_depletion_mm=depletion,
            confidence=0.72,
            rationale="depletion is at field capacity; irrigate",
        ),
        spray=SprayAdvice(
            target_date=TARGET_DATE,
            can_spray_tomorrow=True,
            recommended_windows=[
                SprayWindow(
                    start=datetime(2025, 2, 9, 7, 0),
                    end=datetime(2025, 2, 9, 12, 0),
                    reason="delta_t",
                )
            ],
            confidence=0.61,
            rationale="morning window clears all four gates",
        ),
        summary=plan,
        conflicts_resolved=["drip wets the root zone, not the canopy"],
        overall_confidence=0.61,
    )


# --- phase 6: the advisory round trip --------------------------------------------


def test_advisory_round_trips_through_the_database(ops_session):
    original = _advisory()
    repository.save_advisory(
        ops_session, original, tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )

    restored = repository.get_advisory(ops_session, tenant=TENANT, run_date=RUN_DATE)

    # Not "close enough" -- the same contract, field for field, including the
    # nested legs and the flattened reconciliation.
    assert restored == original


def test_missing_advisory_is_none_not_an_error(ops_session):
    assert repository.get_advisory(ops_session, tenant=TENANT, run_date=date(1999, 1, 1)) is None


# --- phase 6: provenance is stored alongside, not inside, the contract -----------


def test_provenance_is_stored_and_is_not_part_of_the_advisory_contract(ops_session):
    repository.save_advisory(
        ops_session,
        _advisory(),
        tenant=TENANT,
        run_date=RUN_DATE,
        deps=WINE_GRAPES,
        model_id="openai:gpt-4o-mini",
        prompt_name="agronomy_advisor",
        prompt_version="7",
        prompt_source="registry",
        code_sha="abc123",
        trace_id="trace-xyz",
    )

    row = repository.get_advisory_row(ops_session, tenant=TENANT, run_date=RUN_DATE)
    assert row.model_id == "openai:gpt-4o-mini"
    assert row.prompt_version == "7"
    assert row.prompt_source == "registry"
    assert row.trace_id == "trace-xyz"
    assert row.deps_hash == deps_hash(WINE_GRAPES)

    # The advisory itself never grew a trace_id: storage wanted it, the contract
    # didn't.
    assert "trace_id" not in DailyFarmAdvisory.model_fields


def test_confidences_are_promoted_to_columns_for_aggregation(ops_session):
    advisory = _advisory()
    repository.save_advisory(
        ops_session, advisory, tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )
    row = repository.get_advisory_row(ops_session, tenant=TENANT, run_date=RUN_DATE)

    # Denormalised on write so S6 can chart them without a JSONB path...
    assert row.irrigation_confidence == pytest.approx(advisory.irrigation.confidence)
    assert row.spray_confidence == pytest.approx(advisory.spray.confidence)
    assert row.overall_confidence == pytest.approx(advisory.overall_confidence)
    # ...but the JSONB stays authoritative, and they agree.
    assert row.irrigation["confidence"] == pytest.approx(row.irrigation_confidence)


# --- phase 6 / S3.2: the idempotency key -----------------------------------------


def test_saving_the_same_run_date_twice_upserts_instead_of_duplicating(ops_session):
    repository.save_advisory(
        ops_session, _advisory(plan="first answer"), tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )
    repository.save_advisory(
        ops_session, _advisory(plan="second answer"), tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )

    rows = ops_session.exec(
        select(Advisory).where(Advisory.tenant == TENANT, Advisory.run_date == RUN_DATE)
    ).all()
    assert len(rows) == 1, "a re-run must not leave the grower with two advisories for one day"

    # Last writer wins: a re-run is normally a correction, so the newer answer is
    # the one worth keeping.
    restored = repository.get_advisory(ops_session, tenant=TENANT, run_date=RUN_DATE)
    assert restored.summary == "second answer"


def test_different_tenants_and_dates_are_separate_advisories(ops_session):
    repository.save_advisory(
        ops_session, _advisory(), tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )
    repository.save_advisory(
        ops_session, _advisory(), tenant="other-farm", run_date=RUN_DATE, deps=WINE_GRAPES
    )
    repository.save_advisory(
        ops_session, _advisory(), tenant=TENANT, run_date=date(2025, 2, 7), deps=WINE_GRAPES
    )

    assert len(repository.list_advisory_rows(ops_session, tenant=TENANT)) == 2
    assert len(repository.list_advisory_rows(ops_session, tenant="other-farm")) == 1


def test_history_can_be_windowed_by_date(ops_session):
    for day in (5, 6, 7, 8):
        repository.save_advisory(
            ops_session, _advisory(), tenant=TENANT, run_date=date(2025, 2, day), deps=WINE_GRAPES
        )

    rows = repository.list_advisory_rows(
        ops_session, tenant=TENANT, start=date(2025, 2, 6), end=date(2025, 2, 7)
    )
    assert [r.run_date for r in rows] == [date(2025, 2, 7), date(2025, 2, 6)]


# --- phase 6: the database is storage, not an authority on shape -----------------


def test_a_corrupted_row_fails_validation_on_read_rather_than_reaching_a_grower(ops_session):
    repository.save_advisory(
        ops_session, _advisory(), tenant=TENANT, run_date=RUN_DATE, deps=WINE_GRAPES
    )
    row = repository.get_advisory_row(ops_session, tenant=TENANT, run_date=RUN_DATE)

    # Someone hand-edits JSONB in psql, or an older schema wrote this.
    row.irrigation = {**row.irrigation, "unexpected_field": "nonsense"}

    with pytest.raises(ValidationError):
        row_to_advisory(row)


# --- phase 6 / ADR-001: derived values are not stored ----------------------------


def test_raw_mm_is_recomputed_not_stored(ops_session):
    repository.save_grower_config(
        ops_session, WINE_GRAPES, tenant=TENANT, location=LOCATION, region="eu-west-1"
    )
    restored = repository.get_current_deps(ops_session, tenant=TENANT, location=LOCATION)

    # The irrigation trigger survives the round trip...
    assert restored.raw_mm == pytest.approx(WINE_GRAPES.raw_mm)
    # ...without ever being a column. It is mad_fraction * taw_mm; storing it
    # would give the trigger a second home and a chance to disagree.
    assert "raw_mm" not in GrowerConfig.model_fields
    assert restored.raw_mm == pytest.approx(restored.mad_fraction * restored.taw_mm)


# --- phase 6: Deps as rows -------------------------------------------------------


def test_deps_round_trip_is_the_identity(ops_session):
    olive = Deps(crop="olive", kc=0.85, taw_mm=120.0, mad_fraction=0.5)
    repository.save_grower_config(
        ops_session, olive, tenant=TENANT, location="grove-1", region="eu-south-1"
    )

    restored = repository.get_current_deps(ops_session, tenant=TENANT, location="grove-1")
    # Frozen dataclass equality: every threshold, including the tuples that had to
    # be flattened into column pairs and rebuilt.
    assert restored == olive


def test_adding_a_crop_is_an_insert_not_a_code_change(ops_session):
    """The claim deps.py makes, exercised: a second crop needs no new code."""
    repository.save_grower_config(
        ops_session, WINE_GRAPES, tenant=TENANT, location="block-7", region="eu-west-1"
    )
    repository.save_grower_config(
        ops_session,
        Deps(crop="olive", kc=0.85, taw_mm=120.0),
        tenant=TENANT,
        location="grove-1",
        region="eu-south-1",
    )

    assert repository.get_current_deps(ops_session, tenant=TENANT, location="block-7").crop == WINE_GRAPES.crop
    assert repository.get_current_deps(ops_session, tenant=TENANT, location="grove-1").crop == "olive"


def test_unconfigured_block_is_none_not_a_silent_default(ops_session):
    # Falling back to WINE_GRAPES here would advise a grower using another crop's
    # thresholds. Better to have no answer than a confident wrong one.
    assert repository.get_current_deps(ops_session, tenant=TENANT, location="nowhere") is None


# --- phase 6: config is versioned, so old advisories stay explicable -------------


def test_changing_config_opens_a_new_version_and_closes_the_old(ops_session):
    first = repository.save_grower_config(
        ops_session, WINE_GRAPES, tenant=TENANT, location=LOCATION, region="eu-west-1"
    )
    thirstier = Deps(taw_mm=180.0)
    second = repository.save_grower_config(
        ops_session, thirstier, tenant=TENANT, location=LOCATION, region="eu-west-1"
    )

    assert first.id != second.id
    assert first.valid_to is not None, "the superseded version must be closed, not deleted"
    assert second.valid_to is None
    assert repository.get_current_deps(ops_session, tenant=TENANT, location=LOCATION) == thirstier

    # The old thresholds are still reachable by the hash an old advisory stored --
    # which is the entire point of keeping them.
    recovered = repository.get_deps_by_hash(
        ops_session, tenant=TENANT, deps_fingerprint=deps_hash(WINE_GRAPES)
    )
    assert recovered == WINE_GRAPES


def test_resaving_identical_config_does_not_open_a_noisy_new_version(ops_session):
    first = repository.save_grower_config(
        ops_session, WINE_GRAPES, tenant=TENANT, location=LOCATION, region="eu-west-1"
    )
    again = repository.save_grower_config(
        ops_session, WINE_GRAPES, tenant=TENANT, location=LOCATION, region="eu-west-1"
    )
    assert first.id == again.id


# --- phase 6: deps_hash, one of B2's five drift tags -----------------------------


def test_deps_hash_is_stable_and_sensitive():
    assert deps_hash(WINE_GRAPES) == deps_hash(Deps())
    # The B2 case this exists for: an intentional constant change must move the
    # hash, so a moved eval score is attributable to it rather than mistaken for
    # model drift.
    assert deps_hash(Deps(effective_rain_fraction=0.75)) != deps_hash(WINE_GRAPES)
    # Tuple fields count too -- they'd be easy to drop from a hand-written hash.
    assert deps_hash(Deps(wind_ideal_ms=(1.0, 4.2))) != deps_hash(WINE_GRAPES)


# --- phase 6 / S2: WeatherRow is the seam, and it survives a round trip -----------


def test_weather_row_round_trip_preserves_missing_readings_and_naive_timestamps(ops_session):
    # An hour with no ET0 and no delta-T: exactly the row phase 1/phase 2 care about.
    original = WeatherRow(
        timestamp=datetime(2025, 2, 9, 14, 0),
        temp_c=24.5,
        et0_mm=None,
        precip_mm=0.0,
        delta_t_c=None,
        wind_ms=2.2,
        spray_index=55.0,
    )
    observation = weather_row_to_observation(
        original, tenant=TENANT, location=LOCATION, kind="forecast", source="csv"
    )
    ops_session.add(observation)
    ops_session.flush()
    ops_session.refresh(observation)

    restored = observation_to_weather_row(observation)

    # None survives as None: the database preserves "we didn't observe this"
    # rather than laundering it into a zero, which is what would silently turn a
    # skipped hour into a claim the vine wasn't thirsty.
    assert restored.et0_mm is None
    assert restored.delta_t_c is None
    assert restored.precip_mm == 0.0  # a real zero stays a real zero
    # Naive in, naive out -- byte-identical, despite timestamptz storage.
    assert restored.timestamp == original.timestamp
    assert restored.timestamp.tzinfo is None
    assert restored == original


def test_forecast_and_history_for_the_same_hour_coexist(ops_session):
    """The natural key carries `kind`, so tonight's forecast can't overwrite what
    actually happened -- which is what keeps the golden dataset honest."""
    stamp = datetime(2025, 2, 9, 14, 0)
    for kind, temp in (("forecast", 24.0), ("history", 26.5)):
        ops_session.add(
            weather_row_to_observation(
                WeatherRow(timestamp=stamp, temp_c=temp),
                tenant=TENANT,
                location=LOCATION,
                kind=kind,
                source="csv",
            )
        )
    ops_session.flush()

    rows = ops_session.exec(
        select(WeatherObservation).where(
            WeatherObservation.tenant == TENANT, WeatherObservation.location == LOCATION
        )
    ).all()
    assert len({r.kind for r in rows}) == 2
    assert {r.temp_c for r in rows} == {24.0, 26.5}
    # Same hour, two rows, neither having overwritten the other.
    assert len({r.observed_at for r in rows}) == 1


# --- phase 7 / S2.3: idempotent upsert of fetched observations -------------------


def test_upsert_observations_is_idempotent_on_the_natural_key(ops_session):
    """Re-fetching an overlapping window must not duplicate rows.

    A 30-day history re-fetched daily overlaps 29 days with yesterday's pull.
    Without idempotency that's thousands of duplicate rows a week; with it, the
    second write refreshes in place.
    """
    from vinea.sources.persist import upsert_observations

    rows = [
        WeatherRow(timestamp=datetime(2025, 2, 9, h, 0), temp_c=20.0 + h, et0_mm=0.3)
        for h in range(24)
    ]
    n1 = upsert_observations(
        ops_session, rows, tenant=TENANT, location=LOCATION, kind="history", source="open_meteo"
    )
    n2 = upsert_observations(
        ops_session, rows, tenant=TENANT, location=LOCATION, kind="history", source="open_meteo"
    )
    assert n1 == 24 and n2 == 24  # both writes "wrote" 24, but...

    total = ops_session.exec(
        select(WeatherObservation).where(
            WeatherObservation.tenant == TENANT, WeatherObservation.kind == "history"
        )
    ).all()
    assert len(total) == 24, "the second fetch must not have created a second copy"


def test_upsert_refreshes_a_revised_reading_in_place(ops_session):
    """ERA5 revises values as more data arrives; the newer number should win."""
    from vinea.sources.persist import upsert_observations

    stamp = datetime(2025, 2, 9, 12, 0)
    upsert_observations(
        ops_session,
        [WeatherRow(timestamp=stamp, temp_c=20.0)],
        tenant=TENANT,
        location=LOCATION,
        kind="history",
        source="open_meteo",
    )
    upsert_observations(
        ops_session,
        [WeatherRow(timestamp=stamp, temp_c=21.5)],  # revised
        tenant=TENANT,
        location=LOCATION,
        kind="history",
        source="open_meteo",
    )
    row = ops_session.exec(
        select(WeatherObservation).where(WeatherObservation.observed_at == stamp.replace(tzinfo=None))
    ).one()
    assert row.temp_c == pytest.approx(21.5)
