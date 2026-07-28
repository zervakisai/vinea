"""Vinea — agronomic decision agents for a vineyard (irrigation + spray advisory).

Package layout (built milestone by milestone — see the GitHub issues / ROADMAP):
  config.py    — env + paths + model string                                 (phase 1 #1)
  ingest.py    — CSV -> validated WeatherRow + DataQuality degradation       (phase 1 #2)
  pipeline.py  — orchestration entrypoint (phase 1: stub; phase 2/phase 3: FeatureBuilder + 3-agent graph)
  cli.py       — `vinea` / `python -m vinea` one-command entrypoint          (phase 1 #1)
"""

__version__ = "0.1.0"
