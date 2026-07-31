"""Tell a person when a promise breaks.

Recording a breach and telling somebody are different things, and until now only
the first existed. `slo check` wrote a row and exited non-zero, which is legible to
cron and invisible to a human at 06:05 -- and a breach nobody hears about is a
measurement, not an alert.

ADR-010 deferred this: *"the right first step when someone is on a rota, and
pointless before that."* One operator watching one channel is a rota of one, and
the amendment says so rather than pretending the condition changed by itself.

One webhook, deliberately. Not Alertmanager -- that is three more services for a
rota of one, and ADR-003's argument stands. Not email, which needs a relay, a
sender identity, and a deliverability problem. A POST to a URL is the smallest
thing that reaches a phone, and every service anyone would plausibly point it at --
Slack, Discord, ntfy, Teams, a Lambda -- accepts one.

## What it will not do

**Fail the check.** A webhook that is down must not change the exit code. The
breach is real whether or not the message landed, and a `slo check` that exits 1
for a DNS failure teaches an operator to distrust its exit code -- which costs more
than the notification was ever worth.

**Retry.** The check runs once a day. A transient failure is covered by tomorrow's
run, and a retry loop inside a CronJob with `backoffLimit: 0` is how a five-second
command becomes one that hangs until the next schedule and is never noticed.

**Send an all-clear.** Only breaches go out. A daily "everything is fine" is a
notification people filter, and a filtered channel is a channel nobody reads on the
morning it matters.

**Deduplicate.** A breach that persists for a week notifies seven times, once per
morning. That is the intended behaviour, not an oversight: the availability window
is 30 days, so a breach can persist for weeks, and going quiet after the first
message would make an unresolved breach indistinguishable from a resolved one.
Suppression is the reader's job -- every target here has a mute button, and none of
them can un-drop a message this process decided not to send.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from vinea.slo.objectives import SLIResult, error_budget

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "VINEA_ALERT_WEBHOOK_URL"

# Short by design: this runs inside a daily job holding a database session, and a
# notification that hangs is worse than one that never arrives.
TIMEOUT_SECONDS = 10.0

# Which runbook answers which breach. The link is most of the message's value: one
# that says only "SLO breached" makes the reader go and find the runbook at exactly
# the moment they are least able to.
RUNBOOKS = {
    "advisory_availability": "docs/runbooks/advisory-missing.md",
    "degraded_rate": "docs/runbooks/degraded-rate.md",
    "read_latency_p95_ms": "docs/runbooks/queue-not-draining.md",
}

RUNBOOK_BASE = "https://github.com/zervakisai/vinea/blob/main"


class WebhookNotConfigured(Exception):
    """Raised only by `require_webhook`, for the explicit `--notify` path."""


def webhook_url() -> str | None:
    """The configured target, or None.

    Read per call rather than at import, so a test can set it and so a long-lived
    process picks up a restart's environment without a reload path.

    A Slack or Discord webhook URL is a **bearer credential wearing a URL's
    clothes**: anyone holding it can post as you. It therefore arrives from the
    environment (the Secret, in the chart), never from `values.yaml`, and never
    appears in a log line -- see `_log_failure`.
    """
    url = os.environ.get(WEBHOOK_ENV) or None
    if url is None:
        return None
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        # A typo that produces `file:///etc/passwd` should be a startup complaint,
        # not a silently different behaviour inside urlopen.
        logger.warning("%s is not an http(s) URL; notifications disabled", WEBHOOK_ENV)
        return None
    return url


def require_webhook() -> str:
    url = webhook_url()
    if url is None:
        raise WebhookNotConfigured(
            f"{WEBHOOK_ENV} is not set to an http(s) URL, so there is nowhere to notify."
        )
    return url


def format_breach(result: SLIResult) -> str:
    """One breach, written to be read in a phone notification preview.

    Because that is where it will actually be read. First line: how urgent, which
    objective, what it measured, what it promised, and where the runbook is. Second
    line: the error budget, because *how bad* is the follow-up question, not the
    first one.
    """
    objective = result.objective
    if objective.unit == "milliseconds":
        measured = f"{result.value:.0f} ms" if result.value is not None else "unmeasured"
        target = f"under {objective.target:.0f} ms"
    else:
        measured = f"{result.value * 100:.1f}%" if result.value is not None else "unmeasured"
        comparison = "at least" if objective.higher_is_better else "at most"
        target = f"{comparison} {objective.target * 100:.1f}%"

    headline = "SLO BREACH" if objective.pages else "SLO notice"
    runbook = RUNBOOKS.get(objective.key)
    link = f"\nRunbook: {RUNBOOK_BASE}/{runbook}" if runbook else ""
    line = (
        f"{headline}: {objective.key} is {measured}, promised {target} "
        f"over {objective.window_days} days (n={result.sample_size})."
    )

    budget = error_budget(result)
    if budget is not None:
        state = "EXHAUSTED" if budget.exhausted else f"{budget.remaining:.1f} left"
        line += (
            f"\nBudget: {budget.observed_failures} of {budget.allowed_failures:.1f} "
            f"allowed — {state}. {budget.policy}"
        )
    return line + link


def build_payload(results: list[SLIResult]) -> dict:
    """The JSON body, shaped so one configuration works for several targets.

    `text` and `content` carry the same string because Slack and Teams read the
    first and Discord reads the second; sending both means the operator picks a URL
    rather than a vendor adapter. `breaches` carries the structured version for
    anything that parses -- notably `urgent`, so a consumer can route the
    degraded-rate notice somewhere quieter without re-deriving which of the three
    objectives is allowed to interrupt a person.
    """
    body = "\n\n".join(format_breach(r) for r in results)
    return {
        "text": body,
        "content": body,
        "breaches": [
            {
                "objective": r.objective.key,
                "value": r.value,
                "target": r.objective.target,
                "unit": r.objective.unit,
                "sample_size": r.sample_size,
                "window_days": r.objective.window_days,
                "urgent": r.objective.pages,
                "runbook": RUNBOOKS.get(r.objective.key),
            }
            for r in results
        ],
    }


def _log_failure(exc: Exception) -> None:
    """Say what went wrong without saying where.

    `str(exc)` on a `URLError` can carry the host, and the host is half the
    credential. Type and HTTP status are enough to tell a misconfigured URL (404,
    403) from an unreachable one (URLError) from a slow one (timeout), which is
    every diagnosis this log line needs to support.
    """
    if isinstance(exc, urllib.error.HTTPError):
        logger.warning("alert webhook rejected the post: HTTP %s", exc.code)
    else:
        logger.warning("alert webhook unreachable: %s", type(exc).__name__)


def post(url: str, payload: dict) -> bool:
    """POST once. Returns whether it landed. Never raises."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status >= 300:
                logger.warning("alert webhook returned HTTP %s", response.status)
                return False
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # Never fatal, for the reason in the module docstring: the breach is real
        # whether or not the message landed.
        _log_failure(exc)
        return False
    return True


def notify(results: list[SLIResult]) -> bool:
    """Send the breaches among `results`, if there are any and a target is set.

    Returns True only when something was actually delivered. The caller does not
    branch on it -- `slo check`'s exit code is about the breach, not the message --
    but it lets a test distinguish "no webhook configured" from "posted nothing"
    from "posted", which are three different states that all look like silence.
    """
    url = webhook_url()
    breaches = [r for r in results if r.met is False]
    if url is None or not breaches:
        return False
    return post(url, build_payload(breaches))
