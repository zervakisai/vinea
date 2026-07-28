"""The eval gate: score the model against the deterministic oracle.

DESIGN.md B2's scoring half. The insight that shapes everything here: for the legs
that have a *correct numerical answer*, the scorer must not be an LLM judging an LLM
-- it's the same deterministic functions `features.py` already has, wrapped for
scoring. A `WaterBalanceOracle` and the spray-window engine score the model's output
against ground truth directly: how many mm off was the depletion, did the chosen
window actually clear every gate.

  oracles.py     the deterministic oracles (wrap features.py, don't reimplement)
  asymmetric.py  the asymmetric-cost evaluator (missed irrigation ~5x, recall-gated)
  golden.py      the golden-replay dataset over the frozen fixtures + the five drift tags
  judge.py       LLM-as-judge, confined to the one subjective artifact (the summary)
  dataset.py     the oracles wrapped as pydantic-evals Evaluators
  run.py         run a dataset, write eval_runs tagged with the five drift tags

The oracle plays two roles that must NOT become one code path (B2's circularity trap,
handled in S4.4): inline it's the output_validator that forces a retry; here, async,
it's the metric an eval scores against -- and it scores the *pre-correction* output
(advisories.pre_correction_output), never the shipped one.
"""
