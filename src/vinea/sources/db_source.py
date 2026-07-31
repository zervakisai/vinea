"""A `WeatherSource` that reads what was already fetched.

The other two sources produce rows from outside the system: `CsvSource` parses a
file, `OpenMeteoSource` calls an API. This one reads `weather_observations`, which
is where both of them end up once `persist.upsert_observations` has run.

That makes it the source the nightly batch should use, for a reason worth stating:
a worker that called the weather API directly would refetch the same hours for
every tenant on every retry, and a task retried three times would hit a rate limit
rather than the row it already had. Fetch once, persist, read from the database --
and a retry costs a `SELECT`.

It also means the batch is reproducible. Re-running a night reads the same rows
the first run did, so a re-run produces the same advisory rather than whatever the
provider is serving now.

Ignores `latitude`/`longitude`: rows are addressed by `(tenant, location)`, which
is how they were written. The protocol passes coordinates because an API source
needs them, and a source is allowed not to care -- the seam is the return type
(ADR-002).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from vinea.db.mapping import observation_to_weather_row
from vinea.db.models import WeatherObservation
from vinea.ingest import WeatherLoadResult, WeatherRow, assemble_load_result

# The source label written by the API adapter, and therefore the one to read back.
# `weather_observations` keeps `source` in its natural key so a CSV fixture and a
# live feed can both hold an opinion about the same hour without overwriting each
# other; reading has to pick one.
API_SOURCE = "open-meteo"


class DbSource:
    """Reads persisted observations for one tenant's block.

    Constructed per run rather than held, because it closes over the session the
    caller owns -- the same transaction that will write the advisory.
    """

    def __init__(
        self,
        session: Session,
        *,
        tenant: str,
        location: str,
        source: str = API_SOURCE,
        staleness_threshold_hours: int = 48,
    ) -> None:
        self._session = session
        self._tenant = tenant
        self._location = location
        self._source = source
        self._staleness_threshold_hours = staleness_threshold_hours

    def _rows(self, kind: str, start: datetime, end: datetime) -> list[WeatherRow]:
        observations = self._session.exec(
            select(WeatherObservation)
            .where(
                WeatherObservation.tenant == self._tenant,
                WeatherObservation.location == self._location,
                WeatherObservation.source == self._source,
                WeatherObservation.kind == kind,
                WeatherObservation.observed_at >= start,
                WeatherObservation.observed_at < end,
            )
            .order_by(WeatherObservation.observed_at)
        ).all()
        return [observation_to_weather_row(o) for o in observations]

    def load(
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        history_days: int = 30,
        forecast_days: int = 7,
        run_date: date,
    ) -> WeatherLoadResult:
        """History and forecast around `run_date`, quality-assessed like any source.

        The windows are half-open and anchored on `run_date` so a re-run is
        deterministic: history is the `history_days` before it, forecast is
        `run_date` onward. An hour exactly at midnight on `run_date` belongs to the
        forecast, not the history -- the same convention the committed capture uses,
        and getting it wrong double-counts a day of ETc.

        Quality is assessed by `assemble_load_result`, not here. Whether the data is
        stale or gappy is decided identically for every source, which is the point
        of the seam (ADR-002): this class produces rows and has no opinion about
        whether they are good enough.
        """
        midnight = datetime.combine(run_date, time.min)
        history = self._rows(
            "history", midnight - timedelta(days=history_days), midnight
        )
        forecast = self._rows(
            "forecast", midnight, midnight + timedelta(days=forecast_days)
        )
        return assemble_load_result(
            history,
            forecast,
            run_date,
            staleness_threshold_hours=self._staleness_threshold_hours,
        )

    def has_rows(self, *, run_date: date, history_days: int = 30) -> bool:
        """Is there anything to read at all?

        Distinct from `load` returning empty lists, because the caller needs to
        decide *before* assessing quality: no rows is "fall back to the bundled
        capture", while a few gappy rows is "use them and let the confidence
        penalty say so". Collapsing those two would make an empty database look
        like severely degraded data.
        """
        midnight = datetime.combine(run_date, time.min)
        found = self._session.exec(
            select(WeatherObservation.id)
            .where(
                WeatherObservation.tenant == self._tenant,
                WeatherObservation.location == self._location,
                WeatherObservation.source == self._source,
                WeatherObservation.observed_at >= midnight - timedelta(days=history_days),
            )
            .limit(1)
        ).first()
        return found is not None
