"""AA-828 — operator e-mail for spend budget alerts: the cost guard queues one ops_alert_email job
when it writes the bell row; the worker sends it through SES. Mock-only (stub SES client, fake conn)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import cost_guard as cg
from shared import ops_alerts
from shared.aws_client import ses_mailer
from shared.jobs import registry
from shared.jobs.queue import Job

MAIL_ENV = {"ALERT_EMAIL_TO": "ops@example.com", "SES_FROM_ADDRESS": "no-reply@lumiguides.it.com",
            "SES_CONFIGURATION_SET": "aa-email-dev-transactional"}


@pytest.fixture
def mail_env(monkeypatch):
    for k, v in MAIL_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def no_mail_env(monkeypatch):
    for k in MAIL_ENV:
        monkeypatch.delenv(k, raising=False)


def _budget(**kw):
    base = dict(provider="bedrock", scope="job:place_extract", per_run_usd=2.0, per_day_usd=10.0,
                day_spent_at_start=8.5)
    base.update(kw)
    return cg.RunBudget(**base)


def _pool(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _conn(job_row=None):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)          # no unread bell row yet
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=job_row)        # queue.enqueue INSERT ... RETURNING id
    return conn


class StubSes:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"MessageId": "msg-1"}


class ClientError(Exception):  # botocore-shaped: .response["Error"]["Code"]
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


# ── SES helper ─────────────────────────────────────────────────────────────────────────────────

def test_config_none_when_unset(no_mail_env):
    assert ses_mailer.alert_mail_config() is None


def test_config_none_without_sender(no_mail_env, monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.com")
    assert ses_mailer.alert_mail_config() is None


def test_config_from_env(mail_env):
    cfg = ses_mailer.alert_mail_config()
    assert cfg.to == "ops@example.com" and cfg.sender == "no-reply@lumiguides.it.com"
    assert cfg.configuration_set == "aa-email-dev-transactional"


@pytest.mark.asyncio
async def test_send_builds_sesv2_request():
    client = StubSes()
    msg = ses_mailer.Message(to="ops@example.com", subject="S", text="T", html="<p>H</p>")
    assert await ses_mailer.send(client, msg, sender="no-reply@lumiguides.it.com",
                                 configuration_set="aa-email-dev-transactional") == "msg-1"
    kw = client.calls[0]
    assert kw["FromEmailAddress"] == "no-reply@lumiguides.it.com"
    assert kw["Destination"] == {"ToAddresses": ["ops@example.com"]}
    assert kw["ConfigurationSetName"] == "aa-email-dev-transactional"
    assert kw["Content"]["Simple"]["Body"]["Text"]["Data"] == "T"


@pytest.mark.asyncio
async def test_send_without_configuration_set_omits_it():
    client = StubSes()
    await ses_mailer.send(client, ses_mailer.Message("a@b.c", "S", "T", "H"), sender="x@lumiguides.it.com")
    assert "ConfigurationSetName" not in client.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,permanent", [("MessageRejected", True), ("TooManyRequestsException", False),
                                            ("AccessDeniedException", False)])
async def test_send_maps_ses_errors(code, permanent):
    with pytest.raises(ses_mailer.MailError) as info:
        await ses_mailer.send(StubSes(ClientError(code)), ses_mailer.Message("a@b.c", "S", "T", "H"),
                              sender="x@lumiguides.it.com")
    assert info.value.code == code and info.value.permanent is permanent


@pytest.mark.asyncio
async def test_send_reraises_non_ses_errors():
    with pytest.raises(ConnectionError):
        await ses_mailer.send(StubSes(ConnectionError("down")), ses_mailer.Message("a@b.c", "S", "T", "H"),
                              sender="x@lumiguides.it.com")


# ── enqueue (cost guard side) ──────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_enqueue_noop_when_not_configured(no_mail_env):
    conn = _conn({"id": "job-1"})
    out = await ops_alerts.enqueue_spend_budget_email(conn, summary=_budget().summary(), alert_pct=80,
                                                      stopped=False, message="m", used_pct=85.0, now=NOW)
    assert out is None
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_uses_budget_threshold_day_key(mail_env):
    conn = _conn({"id": "job-1"})
    out = await ops_alerts.enqueue_spend_budget_email(conn, summary=_budget().summary(), alert_pct=80,
                                                      stopped=False, message="m", used_pct=85.04, now=NOW)
    assert out == "job-1"
    args = conn.fetchrow.await_args.args
    assert args[1] == "ops_alert_email"
    assert '"used_pct": 85.0' in args[2] and '"period": "2026-10-15"' in args[2]
    assert args[3] == "ops_alert_email:spend_budget:bedrock:job:place_extract:80pct:2026-10-15"
    assert args[4] == ops_alerts.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_enqueue_same_key_twice_returns_none(mail_env):
    conn = _conn(None)                                     # ON CONFLICT DO NOTHING -> no row
    conn.fetchval = AsyncMock(return_value="job-1")       # the existing job for that key
    out = await ops_alerts.enqueue_spend_budget_email(conn, summary=_budget().summary(), alert_pct=80,
                                                      stopped=True, message="m", used_pct=None, now=NOW)
    assert out is None
    assert conn.fetchrow.await_args.args[3].endswith(":stop:2026-10-15")


@pytest.mark.asyncio
async def test_maybe_alert_queues_email_with_bell_row(mail_env):
    conn = _conn({"id": "job-1"})
    assert await cg.maybe_alert(_pool(conn), _budget()) is True
    conn.execute.assert_awaited_once()                     # the bell row
    key = conn.fetchrow.await_args.args[3]
    assert key.startswith("ops_alert_email:spend_budget:bedrock:job:place_extract:80pct:")


@pytest.mark.asyncio
async def test_maybe_alert_stop_reason_uses_stop_threshold(mail_env):
    conn = _conn({"id": "job-1"})
    assert await cg.maybe_alert(_pool(conn), _budget(day_spent_at_start=0.0), reason="per-run budget reached")
    assert ":stop:" in conn.fetchrow.await_args.args[3]


@pytest.mark.asyncio
async def test_maybe_alert_no_email_when_bell_throttled(mail_env):
    conn = _conn({"id": "job-1"})
    conn.fetchval = AsyncMock(return_value=1)              # an unread bell row exists
    assert await cg.maybe_alert(_pool(conn), _budget()) is False
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_alert_survives_enqueue_failure(mail_env):
    conn = _conn()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    assert await cg.maybe_alert(_pool(conn), _budget()) is True
    conn.execute.assert_awaited_once()


# ── render ─────────────────────────────────────────────────────────────────────────────────────

def _payload(**kw):
    b = _budget()
    p = {"template": "spend_budget", "period": "2026-10-15", "threshold": "80pct", "alert_pct": 80,
         "used_pct": 85.0, "message": "bedrock spend today $8.50 is 85% of the $10.00 daily budget.",
         "budget": b.summary()}
    p.update(kw)
    return p


def test_render_spend_budget_content():
    msg = ops_alerts.render_spend_budget(_payload(), to="ops@example.com", base_url="https://admin.example",
                                         environment="dev")
    assert msg.to == "ops@example.com"
    assert msg.subject == "[AA-CIS dev] Spend budget bedrock / job:place_extract: 85% of daily limit"
    for part in ("bedrock / job:place_extract", "2026-10-15 (UTC day)", "$8.50 of daily limit $10.00",
                 "alert threshold 80%", "https://admin.example/admin/llm-usage"):
        assert part in msg.text
    assert 'href="https://admin.example/admin/llm-usage"' in msg.html


def test_render_stop_and_escaping():
    msg = ops_alerts.render_spend_budget(_payload(threshold="stop", message="<b>x</b>"), to="o@e.c",
                                         base_url="https://a", environment="dev")
    assert msg.subject.endswith(": run stopped") and "hard stop" in msg.text
    assert "&lt;b&gt;x&lt;/b&gt;" in msg.html and "<b>x</b>" not in msg.html


# ── job handler ────────────────────────────────────────────────────────────────────────────────

def _ctx(payload, ses=None):
    from services.jobs import ops_alert_email_job  # noqa: F401 — registers the kind

    job = Job(id="job-1", kind="ops_alert_email", payload=payload, status="running", attempt=1,
              max_attempts=5, progress={}, cost_usd=0.0)
    return registry.JobContext(MagicMock(), job, {"ses": ses} if ses else {})


def test_kind_registered_not_background():
    from services.jobs import ops_alert_email_job  # noqa: F401

    k = registry.kinds()["ops_alert_email"]
    assert k.max_attempts == ops_alerts.MAX_ATTEMPTS and not k.background


@pytest.mark.asyncio
async def test_handler_sends(mail_env):
    from services.jobs.ops_alert_email_job import run

    ses = StubSes()
    out = await run(_ctx(_payload(), ses))
    assert out["status"] == "sent" and out["message_id"] == "msg-1"
    assert ses.calls[0]["Destination"] == {"ToAddresses": ["ops@example.com"]}


@pytest.mark.asyncio
async def test_handler_skips_when_unset(no_mail_env):
    from services.jobs.ops_alert_email_job import run

    ses = StubSes()
    out = await run(_ctx(_payload(), ses))
    assert out["status"] == "skipped" and not ses.calls


@pytest.mark.asyncio
async def test_handler_permanent_error_is_nonretryable(mail_env):
    from services.jobs.ops_alert_email_job import run

    with pytest.raises(registry.NonRetryable):
        await run(_ctx(_payload(), StubSes(ClientError("MessageRejected"))))


@pytest.mark.asyncio
async def test_handler_transient_error_retries(mail_env):
    from services.jobs.ops_alert_email_job import run

    with pytest.raises(ses_mailer.MailError):
        await run(_ctx(_payload(), StubSes(ClientError("TooManyRequestsException"))))


@pytest.mark.asyncio
async def test_handler_unknown_template(mail_env):
    from services.jobs.ops_alert_email_job import run

    with pytest.raises(registry.NonRetryable):
        await run(_ctx(_payload(template="nope"), StubSes()))
