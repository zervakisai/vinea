"""Persisting fetched observations, idempotently.

When a source is a live API, the rows it returns are worth keeping --
they're the append-only ground truth ADR-001 is about, and re-fetching them
tomorrow won't reproduce today's forecast. So `--source api` can write into
`weather_observations`.

The write is an idempotent upsert on the natural key
(tenant, location, observed_at, kind, source). Fetching the same window twice --
which happens constantly, since a 30-day history re-fetched daily overlaps 29
days with yesterday's -- must not create 29 days of duplicate rows. ON CONFLICT
DO UPDATE means the second fetch refreshes the reading in place: if the archive
revised a value (ERA5 does, as more data arrives), the newer number wins, and if
it didn't, the write is a no-op that cost one statement.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from vinea.db.mapping import weather_row_to_observation
from vinea.db.models import WeatherObservation
from vinea.ingest import WeatherRow


def upsert_observations(
    session: Session,
    rows: list[WeatherRow],
    *,
    tenant: str,
    location: str,
    kind: str,
    source: str,
) -> int:
    """Upsert rows into weather_observations. Returns the number written.

    One multi-row INSERT ... ON CONFLICT rather than a loop of them: a 30-day
    hourly fetch is 720 rows, and 720 round trips to refresh mostly-unchanged
    data is the kind of thing that's fine in a test and miserable in a nightly
    job across every tenant.

    Does not commit -- the caller owns the transaction, same rule as the
    repository, so persisting observations and recording the run that fetched
    them can be made atomic together.
    """
    if not rows:
        return 0

    values = []
    for row in rows:
        obs = weather_row_to_observation(
            row, tenant=tenant, location=location, kind=kind, source=source
        )
        values.append(obs.model_dump(exclude={"id", "ingested_at"}))

    statement = pg_insert(WeatherObservation).values(values)
    # Refresh every reading on conflict; the natural-key columns are what we
    # matched on, so they're excluded from the update set.
    key_columns = {"tenant", "location", "observed_at", "kind", "source"}
    update_columns = {
        col.name: statement.excluded[col.name]
        for col in WeatherObservation.__table__.columns
        if col.name not in key_columns and col.name not in ("id", "ingested_at")
    }
    statement = statement.on_conflict_do_update(
        constraint="uq_weather_observations_natural", set_=update_columns
    )
    session.execute(statement)
    session.flush()
    return len(values)
