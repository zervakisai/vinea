"""The HTTP API: a thin layer over a stable contract.

THE RULE, stated once here and enforced everywhere below: the API does not
run agents and does not compute anything. It enqueues work and reads the database.
Every write is `queue.enqueue`; every read is a repository call. If you deleted
this package, nothing upstream would break -- the worker, the graph, the
guardrails, the contracts all stand on their own. That is the same relationship
the Open-Meteo adapter has to `WeatherRow`: a thin skin over a seam that was stable
before the skin arrived.

The consequence worth internalising: a `POST` returns in milliseconds with a 202
and a task id, *before* any model runs, because running the model is the worker's
job, reached through the queue. The API's latency is a database write, not an
LLM call.
"""
