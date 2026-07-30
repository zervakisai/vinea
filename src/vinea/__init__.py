"""Vinea — agronomic decision agents for a vineyard (irrigation + spray advisory).

Package layout (built milestone by milestone — see the GitHub issues / ROADMAP):
  config.py    — env + paths + model string
  ingest.py    — CSV -> validated WeatherRow + DataQuality degradation
  pipeline.py  — orchestration entrypoint: FeatureBuilder + the three-agent graph
  cli.py       — `vinea` / `python -m vinea` one-command entrypoint
"""

__version__ = "0.1.0"
