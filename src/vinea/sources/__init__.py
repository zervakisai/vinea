"""Weather sources: everything that produces `WeatherRow`s, behind one seam.

The architectural claim is that a data source is an *adapter*, not a
rewrite: a new source is a new module in here that emits rows matching the
`WeatherRow` contract, and nothing downstream -- not features, not agents, not
the graph -- changes or even notices. `base.py` states the seam as a Protocol;
`csv_source.py` and `open_meteo.py` are two implementations of it; the seam test
runs the same pipeline over both and asserts the types out are identical.

See ADR-002.
"""

from vinea.sources.base import WeatherSource

__all__ = ["WeatherSource"]
