"""The notification path, against a real HTTP server on a real socket.

Not a mocked `urlopen`. A mock proves that the code calls the function it was
written to call, which is the one thing nobody doubted; it cannot catch a wrong
method, a missing content type, a body that is not JSON, or a URL joined wrongly.
Those are the failures a webhook actually has, and every one of them needs a
listener to detect. `http.server` on port 0 is twenty lines and catches all four.

The server also lets the *hostile* cases be real rather than simulated: a target
that returns 500, one that hangs past the timeout, and one that is not listening at
all. All three must leave the check's exit code alone, which is the property this
file exists to defend -- an alert path that can fail the thing it monitors is worse
than no alert path.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vinea.slo import notify
from vinea.slo.objectives import AVAILABILITY, JUDGEMENT_RATE, READ_LATENCY, SLIResult


class _Collector(BaseHTTPRequestHandler):
    """Records what it was sent; answers however the test told it to."""

    received: list[dict] = []
    status = 200
    delay = 0.0

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Collector.received.append(
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        if _Collector.delay:
            time.sleep(_Collector.delay)
        self.send_response(_Collector.status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        """Silence. The default writes every request to stderr."""


@pytest.fixture
def webhook(monkeypatch):
    """A live collector, with `VINEA_ALERT_WEBHOOK_URL` pointed at it."""
    _Collector.received = []
    _Collector.status = 200
    _Collector.delay = 0.0
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/services/T000/B000/xxxx"
    monkeypatch.setenv(notify.WEBHOOK_ENV, url)
    try:
        yield _Collector
    finally:
        server.shutdown()
        server.server_close()


def _breach(objective, value, sample_size=100) -> SLIResult:
    result = SLIResult(objective=objective, value=value, sample_size=sample_size)
    assert result.met is False, "the fixture must actually be a breach"
    return result


def _ok(objective, value, sample_size=100) -> SLIResult:
    result = SLIResult(objective=objective, value=value, sample_size=sample_size)
    assert result.met is True
    return result


def test_a_breach_reaches_the_listener_as_a_json_post(webhook):
    """Method, content type, and a parseable body — the four ways a webhook breaks."""
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is True

    assert len(webhook.received) == 1
    request = webhook.received[0]
    assert request["path"] == "/services/T000/B000/xxxx", "the path was not preserved"
    assert request["content_type"] == "application/json"

    payload = json.loads(request["body"])
    assert "advisory_availability" in payload["text"]
    # Both keys, same string: Slack and Teams read `text`, Discord reads `content`.
    # One configuration, several targets.
    assert payload["content"] == payload["text"]


def test_the_message_carries_the_number_the_target_and_the_runbook(webhook):
    """What a person needs at 06:05, in the order they need it."""
    notify.notify([_breach(AVAILABILITY, 0.90)])
    text = json.loads(webhook.received[0]["body"])["text"]

    assert "90.0%" in text, text  # what it measured
    assert "99.0%" in text, text  # what it promised
    assert "30 days" in text, text  # over what window
    assert "runbooks/advisory-missing.md" in text, text  # where to look
    assert "EXHAUSTED" in text, text  # 10 misses against 1.0 allowed


def test_only_breaches_are_sent(webhook):
    """A met objective is not news, and a daily all-clear is a channel people mute."""
    assert notify.notify([_ok(AVAILABILITY, 1.0), _ok(JUDGEMENT_RATE, 0.0)]) is False
    assert webhook.received == []


def test_an_unmeasured_objective_is_not_a_breach(webhook):
    """`met is None` must not notify.

    The same distinction ADR-010 draws for the exit code: "we could not tell" is
    not "it failed". A notifier that filtered on `not r.met` would page every
    morning the latency table happened to be empty, and the channel would be
    trained to ignore it within a week.
    """
    unmeasured = SLIResult(objective=READ_LATENCY, value=None, sample_size=0)
    assert unmeasured.met is None
    assert notify.notify([unmeasured]) is False
    assert webhook.received == []


def test_the_degraded_rate_is_sent_but_not_as_urgent(webhook):
    """ADR-010's rule, enforced instead of narrated.

    "The degraded-rate objective never pages, because nothing is broken for a
    grower when it breaches." It is still sent — the fleet going fully
    deterministic is worth knowing — but flagged so a consumer can route it
    somewhere quieter.
    """
    notify.notify([_breach(JUDGEMENT_RATE, 0.80)])
    payload = json.loads(webhook.received[0]["body"])

    assert payload["breaches"][0]["urgent"] is False
    assert "SLO notice" in payload["text"]
    assert "SLO BREACH" not in payload["text"]

    _Collector.received = []
    notify.notify([_breach(AVAILABILITY, 0.90)])
    payload = json.loads(webhook.received[0]["body"])
    assert payload["breaches"][0]["urgent"] is True
    assert "SLO BREACH" in payload["text"]


def test_several_breaches_go_in_one_message(webhook):
    """One POST, not one per objective. Three notifications is three chances to mute."""
    notify.notify([_breach(AVAILABILITY, 0.90), _breach(JUDGEMENT_RATE, 0.80)])

    assert len(webhook.received) == 1
    payload = json.loads(webhook.received[0]["body"])
    assert {b["objective"] for b in payload["breaches"]} == {
        "advisory_availability",
        "degraded_rate",
    }


def test_a_rejecting_webhook_is_survivable(webhook):
    """HTTP 500 returns False and raises nothing."""
    webhook.status = 500
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False
    assert len(webhook.received) == 1, "it was sent; the target refused it"


def test_a_hanging_webhook_is_survivable(webhook, monkeypatch):
    """A target that never answers must not hold the daily job open.

    The timeout is patched down to keep the suite fast; the code path under test is
    the same one `TIMEOUT_SECONDS` drives in production.
    """
    monkeypatch.setattr(notify, "TIMEOUT_SECONDS", 0.2)
    webhook.delay = 3.0

    started = time.monotonic()
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False
    assert time.monotonic() - started < 2.0, "the timeout did not fire"


def test_an_unreachable_webhook_is_survivable(monkeypatch):
    """Nothing listening at all. Connection refused, no traceback."""
    # Port 1 on loopback: privileged, and nothing binds it.
    monkeypatch.setenv(notify.WEBHOOK_ENV, "http://127.0.0.1:1/hook")
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False


def test_no_webhook_configured_is_a_supported_state(monkeypatch):
    """Unset is the default, and it means "record the breach and say nothing"."""
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    assert notify.webhook_url() is None
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False


def test_a_non_http_url_disables_notification_rather_than_being_obeyed(monkeypatch):
    """A typo must not become a different behaviour inside urlopen.

    `file:///…` is a valid URL that `urlopen` will happily act on. Rejecting
    anything but http(s) keeps a fat-fingered environment variable from turning a
    notification into a filesystem read.
    """
    monkeypatch.setenv(notify.WEBHOOK_ENV, "file:///etc/passwd")
    assert notify.webhook_url() is None
    assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False


def test_the_failure_log_never_carries_the_url(monkeypatch, caplog):
    """A Slack webhook URL is a bearer credential; it must not land in a log.

    Anyone holding the URL can post as the workspace. `str(URLError)` can carry the
    host, so the failure path logs the exception *type* and nothing else — which is
    still enough to tell "unreachable" from "rejected" from "timed out".
    """
    secret = "http://127.0.0.1:1/services/T000/B000/SUPERSECRETTOKEN"
    monkeypatch.setenv(notify.WEBHOOK_ENV, secret)
    with caplog.at_level("WARNING"):
        assert notify.notify([_breach(AVAILABILITY, 0.90)]) is False

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "the failure was silent, which is the other way to get this wrong"
    assert "SUPERSECRETTOKEN" not in logged, logged
    assert "127.0.0.1" not in logged, logged


def test_require_webhook_says_which_variable_is_missing(monkeypatch):
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    with pytest.raises(notify.WebhookNotConfigured, match="VINEA_ALERT_WEBHOOK_URL"):
        notify.require_webhook()


# --------------------------------------------------------------------------- #
# Through the command, against a database                                     #
#                                                                             #
# Everything above tests `notify()` in isolation. These test the thing that   #
# actually runs at 06:05: `python -m vinea.slo check`, which has to measure,  #
# record, notify and return an exit code in an order that survives each of    #
# those failing on its own.                                                   #
# --------------------------------------------------------------------------- #

pytest_db = pytest.mark.db


def _breaching_latency(engine) -> None:
    """Twenty slow reads, which puts p95 far past the 300 ms target."""
    from sqlalchemy import text
    from sqlmodel import Session

    from vinea.db.session import scope_to_ops
    from vinea.slo.queries import SLO_READ_ROUTE

    with Session(engine) as session:
        scope_to_ops(session)
        for _ in range(20):
            session.execute(
                text(
                    "INSERT INTO api_request_samples (route, method, status_code, duration_ms) "
                    "VALUES (:r, 'GET', 200, 900.0)"
                ),
                {"r": SLO_READ_ROUTE},
            )
        session.commit()


@pytest_db
def test_check_records_the_breach_and_then_tells_someone(committing_db, webhook, monkeypatch):
    """The whole path: measure, write the row, post the message, exit 1."""
    from sqlalchemy import func, select
    from sqlmodel import Session

    from vinea.db.models import SLOBreach
    from vinea.db.session import scope_to_ops
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    _breaching_latency(committing_db)

    assert main(["check", "--today", "2026-07-28"]) == 1

    with Session(committing_db) as session:
        scope_to_ops(session)
        recorded = session.execute(select(func.count()).select_from(SLOBreach)).scalar_one()
    assert recorded == 1, "the row is the durable record and must exist regardless"

    assert len(webhook.received) == 1
    payload = json.loads(webhook.received[0]["body"])
    assert payload["breaches"][0]["objective"] == "read_latency_p95_ms"
    assert "900" in payload["text"]


@pytest_db
def test_a_dead_webhook_does_not_change_the_exit_code(committing_db, webhook, monkeypatch):
    """The property this whole file defends.

    An alert path that can fail the check it monitors is worse than no alert path:
    the operator learns that a red `slo check` might just mean Slack was down, and
    the exit code stops carrying information.
    """
    from sqlalchemy import func, select
    from sqlmodel import Session

    from vinea.db.models import SLOBreach
    from vinea.db.session import scope_to_ops
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    _breaching_latency(committing_db)
    webhook.status = 503

    assert main(["check", "--today", "2026-07-28"]) == 1, "1 for the breach, not for Slack"

    with Session(committing_db) as session:
        scope_to_ops(session)
        recorded = session.execute(select(func.count()).select_from(SLOBreach)).scalar_one()
    assert recorded == 1, "the breach was recorded even though nobody heard about it"


@pytest_db
def test_a_healthy_check_notifies_nobody(committing_db, webhook, monkeypatch):
    """Exit 0, and silence. A daily all-clear is how a channel gets muted."""
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    assert main(["check", "--today", "2026-07-28"]) == 0
    assert webhook.received == []


@pytest_db
def test_strict_does_not_notify(committing_db, webhook, monkeypatch):
    """"The query returned no rows" is a message to a release gate, not to a channel.

    `--strict` exits 1 on an *unmeasured* objective, which on an empty database is
    all three. Posting that would tell an operator watching for grower-visible
    problems that three SLOs broke, when what happened is that CI ran a gate.
    """
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    assert main(["check", "--strict", "--today", "2026-07-28"]) == 1
    assert webhook.received == []


@pytest_db
def test_no_notify_suppresses_the_post_but_not_the_row(committing_db, webhook, monkeypatch):
    """For debugging inside the cluster, where the variable is set and unwelcome."""
    from sqlalchemy import func, select
    from sqlmodel import Session

    from vinea.db.models import SLOBreach
    from vinea.db.session import scope_to_ops
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    _breaching_latency(committing_db)

    assert main(["check", "--no-notify", "--today", "2026-07-28"]) == 1
    assert webhook.received == []

    with Session(committing_db) as session:
        scope_to_ops(session)
        recorded = session.execute(select(func.count()).select_from(SLOBreach)).scalar_one()
    assert recorded == 1


@pytest_db
def test_no_record_implies_no_notify(committing_db, webhook, monkeypatch):
    """The one combination that helps nobody.

    Announcing a breach you have explicitly declined to record tells the channel
    about something no query can later confirm happened — and `--no-record` is a
    dry-run flag, so its user is not expecting to be heard.
    """
    from sqlalchemy import func, select
    from sqlmodel import Session

    from vinea.db.models import SLOBreach
    from vinea.db.session import scope_to_ops
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    _breaching_latency(committing_db)

    assert main(["check", "--no-record", "--today", "2026-07-28"]) == 1
    assert webhook.received == []

    with Session(committing_db) as session:
        scope_to_ops(session)
        recorded = session.execute(select(func.count()).select_from(SLOBreach)).scalar_one()
    assert recorded == 0


@pytest_db
def test_report_never_notifies(committing_db, webhook, monkeypatch):
    """Looking at the numbers must not page a channel.

    `report` and `check` differ in exactly this: one is a human reading a table,
    the other is a promise being tested. A `report` that posted would make the ops
    dashboard's own refresh a source of alerts.
    """
    from vinea.slo.__main__ import main

    monkeypatch.setattr("vinea.slo.__main__.make_engine", lambda: committing_db)
    _breaching_latency(committing_db)

    assert main(["report", "--today", "2026-07-28"]) == 0
    assert webhook.received == []
