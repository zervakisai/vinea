"""phase 7 -- the seam test. The proof that a new source changes nothing downstream.

This is the test the whole stage exists to make pass. It runs the SAME pipeline
over two sources -- the CSV fixtures and a mocked Open-Meteo -- and asserts that
what comes out the far end is the same *kind* of thing: the same types, the same
contract, feature-buildable by the identical code. Not the same *numbers*
(different weather), the same *shape*.

Everything here is offline. The Open-Meteo source is driven by an httpx
MockTransport replaying responses captured from the real API (see
tests/fixtures/open_meteo_*.json), so the suite never touches the network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from vinea.contracts import FarmFeatures
from vinea.deps import WINE_GRAPES
from vinea.features import build_features
from vinea.ingest import WeatherLoadResult, WeatherRow
from vinea.sources.base import WeatherSource
from vinea.sources.csv_source import CsvSource
from vinea.sources.open_meteo import OpenMeteoError, OpenMeteoSource, rows_from_response

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).parent.parent / "data"
RUN_DATE = date(2026, 6, 24)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _csv_source() -> CsvSource:
    history = sorted(DATA.glob("*last-30d*.csv"))[-1]
    forecast = sorted(DATA.glob("*next-7d*.csv"))[-1]
    return CsvSource(history, forecast)


@pytest.fixture
def mocked_open_meteo() -> OpenMeteoSource:
    """An OpenMeteoSource whose HTTP is replayed from captured responses.

    The archive URL gets the archive fixture, the forecast URL the forecast
    fixture -- routed by host, so no test depends on query-string ordering.
    """
    archive = _load_fixture("open_meteo_archive.json")
    forecast = _load_fixture("open_meteo_forecast.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "archive-api" in request.url.host:
            return httpx.Response(200, json=archive)
        return httpx.Response(200, json=forecast)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenMeteoSource(client=client)


# --- the seam itself --------------------------------------------------------


def test_open_meteo_source_satisfies_the_weather_source_protocol(mocked_open_meteo):
    # Structural typing, checked at runtime: the adapter is a WeatherSource
    # without inheriting anything, which is the Protocol's whole reason to be.
    assert isinstance(mocked_open_meteo, WeatherSource)
    assert isinstance(_csv_source(), WeatherSource)


def test_both_sources_produce_the_same_type_out(mocked_open_meteo):
    csv_result = _csv_source().load(run_date=RUN_DATE)
    api_result = mocked_open_meteo.load(
        latitude=-34.75, longitude=138.6, history_days=3, forecast_days=3, run_date=RUN_DATE
    )

    # The seam: same return type, same row type, from two unrelated loaders.
    assert isinstance(csv_result, WeatherLoadResult)
    assert isinstance(api_result, WeatherLoadResult)
    assert all(isinstance(r, WeatherRow) for r in api_result.history)
    assert all(isinstance(r, WeatherRow) for r in api_result.forecast)
    # A CSV row and an API row are the same type with the same fields -- neither
    # source can add or drop one, because both go through WeatherRow.
    csv_row = csv_result.history[0]
    api_row = api_result.history[0]
    assert type(csv_row) is type(api_row) is WeatherRow
    assert type(csv_result.quality) is type(api_result.quality)


def test_the_same_feature_builder_consumes_both_sources(mocked_open_meteo):
    """The claim in one assertion: features.build_features neither knows nor
    cares which source produced its rows."""
    api_result = mocked_open_meteo.load(
        latitude=-34.75, longitude=138.6, history_days=3, forecast_days=3, run_date=RUN_DATE
    )
    run_date = max(r.timestamp for r in api_result.history).date()

    # Identical call signature to the CSV path in graph.FeatureBuilderNode.
    features = build_features(
        list(api_result.history),
        list(api_result.forecast),
        api_result.quality,
        run_date,
        deps=WINE_GRAPES,
    )
    assert isinstance(features, FarmFeatures)
    # The deterministic core ran on API data and produced the same shapes the
    # agents already know how to consume. Nothing downstream was touched.
    assert features.irrigation.taw_mm == WINE_GRAPES.taw_mm
    assert isinstance(features.spray.windows, list)


# --- the mapping's honesty --------------------------------------------------


def test_delta_t_is_reconstructed_as_the_wet_bulb_depression():
    archive = _load_fixture("open_meteo_archive.json")
    rows = rows_from_response(archive)
    h = archive["hourly"]

    for i, row in enumerate(rows):
        t = h["temperature_2m"][i]
        tw = h["wet_bulb_temperature_2m"][i]
        if t is not None and tw is not None:
            # Exact identity, not an approximation: Delta-T IS T - Tw.
            assert row.delta_t_c == pytest.approx(t - tw)
            break
    else:
        pytest.fail("fixture has no hour with both temperature and wet-bulb")


def test_spray_index_is_honestly_none_never_fabricated():
    # Open-Meteo has no spray index. The adapter must not invent one -- a
    # fabricated value would be the silent-pass the whole system refuses.
    rows = rows_from_response(_load_fixture("open_meteo_forecast.json"))
    assert rows, "fixture should yield rows"
    assert all(r.spray_index is None for r in rows)


def test_missing_spray_index_makes_api_days_cost_confidence(mocked_open_meteo):
    """The right consequence of an honest None: this source is genuinely worse,
    and the DataQuality says so rather than pretending the data is complete.

    In Vinea's model the spray-critical fields are delta_t/wind (both present
    from Open-Meteo), so the absent spray_index isn't a 5x spray-critical cell --
    it's an ordinary missing cell, one per hour, and it drives the penalty up all
    the same. Either way the confidence is lower and the source is not waved
    through as complete."""
    api_result = mocked_open_meteo.load(
        latitude=-34.75, longitude=138.6, history_days=3, forecast_days=3, run_date=RUN_DATE
    )
    total_hours = len(api_result.history) + len(api_result.forecast)
    # At least one missing cell per hour -- the spray_index Open-Meteo can't provide.
    assert api_result.quality.nan_cells >= total_hours
    assert api_result.quality.confidence_penalty > 0


def test_none_readings_flow_through_without_being_zero_filled():
    # A hole in the API response must arrive as None, not 0.0 -- the water
    # balance skips None and would be misled by a fabricated zero.
    doctored = _load_fixture("open_meteo_archive.json")
    doctored["hourly"]["et0_fao_evapotranspiration"][0] = None
    rows = rows_from_response(doctored)
    assert rows[0].et0_mm is None


def test_api_error_body_raises_rather_than_returning_empty_weather():
    def handler(request: httpx.Request) -> httpx.Response:
        # Open-Meteo's characteristic 200-with-error-body.
        return httpx.Response(200, json={"error": True, "reason": "invalid value for parameter"})

    source = OpenMeteoSource(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OpenMeteoError, match="invalid value"):
        source.fetch_forecast(latitude=-34.75, longitude=138.6, forecast_days=3)


def test_timestamps_are_naive_local_matching_the_csv_convention():
    # timezone=auto -> naive local times, exactly WeatherRow's convention and
    # what the spray-window walk needs. An aware or UTC-shifted timestamp here
    # would silently move every window by the location's offset.
    rows = rows_from_response(_load_fixture("open_meteo_forecast.json"))
    assert all(r.timestamp.tzinfo is None for r in rows)
