"""AA-913 — platform alert recipients: shared/alert_recipients.py (pure rules, SNS wrapper, reconcile),
api/services/alert_recipients.py (rate limits) and the ops_alert_email job's recipients.

No AWS, no DB: SNS is a real botocore client under Stubber (so the request shapes are checked against the SNS model)
or a small stub; the table is an in-memory fake (tests/unit/aa913_fakes.py). The routes and their authorization are
in test_aa913_admin_alert_recipients.py; the SQL itself runs in tests/integration/test_aa913_alert_recipient_sql.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.stub import Stubber

from api.services import alert_recipients as svc
from shared import alert_recipients as ar
from shared import ops_alerts
from shared.aws_client import ses_mailer
from shared.jobs import registry
from shared.jobs.queue import Job
from tests.unit.aa913_fakes import (
    BOOTSTRAP,
    TOPIC_ARN,
    AlertDB,
    StubSns,
    sub_arn,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", TOPIC_ARN)
    monkeypatch.setenv("ALERT_BOOTSTRAP_EMAIL", BOOTSTRAP)
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    ar.clear_cache()
    yield
    ar.clear_cache()


# ── normalize_email / note ───────────────────────────────────────────────────────────────────────

def test_normalize_lower_cases_and_strips():
    assert ar.normalize_email("  On.Call@Adventure.Asia ") == "on.call@adventure.asia"


@pytest.mark.parametrize("raw", [
    "a@x.com,b@y.com", "a@x.com b@y.com", "a@x.com;b@y.com", "no-at.example.com", "two@@example.com",
    "a@b@example.com", "<a@example.com>", "a@nodot", "Name <a@example.com>", '"a"@example.com',
    "x" * 250 + "@example.com", "", "   ", None, 42,
])
def test_normalize_refuses(raw):
    with pytest.raises(ValueError):
        ar.normalize_email(raw)


def test_normalize_matches_the_table_check_length():
    ok = "x" * (254 - len("@example.com")) + "@example.com"
    assert ar.normalize_email(ok) == ok
    with pytest.raises(ValueError):
        ar.normalize_email("x" + ok)


def test_note_is_one_line_and_capped():
    assert ar.normalize_note("  on-call\n phone ") == "on-call phone"
    assert ar.normalize_note("   ") is None and ar.normalize_note(None) is None
    with pytest.raises(ValueError):
        ar.normalize_note("n" * 81)


# ── configuration ────────────────────────────────────────────────────────────────────────────────

def test_topic_config_from_env_never_prints_the_arn():
    cfg = ar.topic_config()
    assert cfg.topic_name == "aa-cis-dev-acp-alerts" and cfg.bootstrap_email == BOOTSTRAP
    assert cfg.region == "us-west-1"
    assert "arn:" not in repr(cfg)


def test_topic_config_none_when_unset(monkeypatch):
    monkeypatch.delenv("ALERTS_TOPIC_ARN")
    assert ar.topic_config() is None


def test_no_arn_or_account_id_in_the_source():
    """Public repo rule: the topic ARN comes from the environment only."""
    import inspect
    import re

    from api.routers import admin_alert_recipients as router_mod

    for mod in (ar, svc, router_mod):
        assert not re.search(r"arn:aws:sns:[a-z0-9-]+:\d{12}", inspect.getsource(mod))


# ── SNS wrapper (real botocore client under Stubber: request shapes match the SNS model) ─────────

@pytest.fixture
def stub():
    client = boto3.client("sns", region_name="us-west-1", aws_access_key_id="x", aws_secret_access_key="y")
    s = Stubber(client)
    s.activate()
    yield client, s
    s.deactivate()


@pytest.mark.asyncio
async def test_list_paginates_and_keeps_email_only(stub):
    client, s = stub
    s.add_response("list_subscriptions_by_topic", {
        "Subscriptions": [
            {"SubscriptionArn": sub_arn("a"), "Owner": "1", "Protocol": "email", "Endpoint": "A@x.com",
             "TopicArn": TOPIC_ARN},
            {"SubscriptionArn": sub_arn("q"), "Owner": "1", "Protocol": "sqs", "Endpoint": "arn:queue",
             "TopicArn": TOPIC_ARN},
        ], "NextToken": "t2"}, {"TopicArn": TOPIC_ARN})
    s.add_response("list_subscriptions_by_topic", {
        "Subscriptions": [{"SubscriptionArn": "PendingConfirmation", "Owner": "1", "Protocol": "email",
                           "Endpoint": "b@y.com", "TopicArn": TOPIC_ARN}]}, {"TopicArn": TOPIC_ARN, "NextToken": "t2"})
    subs = await ar.list_email_subscriptions(client, TOPIC_ARN)
    assert [(x.endpoint, x.state) for x in subs] == [("a@x.com", "confirmed"), ("b@y.com", "pending")]
    s.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_subscribe_asks_for_the_arn_with_email_protocol(stub):
    client, s = stub
    s.add_response("subscribe", {"SubscriptionArn": sub_arn("n")},
                   {"TopicArn": TOPIC_ARN, "Protocol": "email", "Endpoint": "n@x.com", "ReturnSubscriptionArn": True})
    assert await ar.subscribe(client, TOPIC_ARN, "n@x.com") == sub_arn("n")


@pytest.mark.asyncio
async def test_subscription_pending_and_not_found(stub):
    client, s = stub
    s.add_response("get_subscription_attributes", {"Attributes": {"PendingConfirmation": "true"}},
                   {"SubscriptionArn": sub_arn("p")})
    s.add_client_error("get_subscription_attributes", service_error_code="NotFound", http_status_code=404)
    assert await ar.subscription_pending(client, sub_arn("p")) is True
    assert await ar.subscription_pending(client, sub_arn("gone")) is None


@pytest.mark.asyncio
async def test_sns_errors_carry_the_code_not_the_arn(stub):
    client, s = stub
    s.add_client_error("unsubscribe", service_error_code="AuthorizationError", http_status_code=403,
                       service_message=f"User is not authorized on {TOPIC_ARN}")
    with pytest.raises(ar.SnsError) as err:
        await ar.unsubscribe(client, sub_arn("x"))
    assert err.value.code == "AuthorizationError" and "arn:" not in str(err.value)


# ── plan_reconcile (pure) ────────────────────────────────────────────────────────────────────────

def _row(rid, email, status="pending", **kw):
    return {"id": rid, "email": email, "status": status, "removed_reason": kw.get("reason"),
            "subscription_arn": kw.get("arn"), "confirmed_at": None,
            "last_confirmation_sent_at": kw.get("sent", NOW - timedelta(hours=1)), "created_at": NOW}


def _sub(email, arn):
    return ar.SnsSub(arn=arn, endpoint=email)


def _by_id(plan):
    return {u.id: u for u in plan.updates}


def test_plan_confirmed_learns_the_arn():
    plan = ar.plan_reconcile([_row("1", "a@x.com")], [_sub("a@x.com", sub_arn("a"))], BOOTSTRAP, NOW)
    u = _by_id(plan)["1"]
    assert (u.to_status, u.seen, u.subscription_arn, u.changed) == ("confirmed", "confirmed", sub_arn("a"), True)


def test_plan_confirmed_row_already_known_is_unchanged():
    plan = ar.plan_reconcile([_row("1", "a@x.com", "confirmed", arn=sub_arn("a"))],
                             [_sub("a@x.com", sub_arn("a"))], BOOTSTRAP, NOW)
    u = _by_id(plan)["1"]
    assert not u.changed and u.subscription_arn is None and plan.changes == []


def test_plan_pending_stays_pending():
    plan = ar.plan_reconcile([_row("1", "a@x.com")], [_sub("a@x.com", "PendingConfirmation")], BOOTSTRAP, NOW)
    u = _by_id(plan)["1"]
    assert (u.to_status, u.seen, u.changed) == ("pending", "pending", False)


def test_plan_missing_within_grace_waits():
    plan = ar.plan_reconcile([_row("1", "a@x.com", sent=NOW - timedelta(minutes=2))], [], BOOTSTRAP, NOW)
    u = _by_id(plan)["1"]
    assert (u.to_status, u.seen, u.changed) == ("pending", "missing", False)


def test_plan_pending_missing_expires():
    plan = ar.plan_reconcile([_row("1", "a@x.com", sent=NOW - timedelta(days=3, minutes=1))], [], BOOTSTRAP, NOW)
    u = _by_id(plan)["1"]
    assert (u.to_status, u.removed_reason, u.seen) == ("removed", "expired", "missing")


def test_plan_confirmed_missing_is_unsubscribed():
    plan = ar.plan_reconcile([_row("1", "a@x.com", "confirmed", arn=sub_arn("a"))], [], BOOTSTRAP, NOW)
    assert (_by_id(plan)["1"].to_status, _by_id(plan)["1"].removed_reason) == ("removed", "unsubscribed")


def test_plan_deleted_arn_counts_as_missing():
    plan = ar.plan_reconcile([_row("1", "a@x.com", "confirmed", arn=sub_arn("a"))],
                             [_sub("a@x.com", "Deleted")], BOOTSTRAP, NOW)
    assert _by_id(plan)["1"].removed_reason == "unsubscribed"
    assert plan.unmanaged == []


def test_plan_matches_endpoints_case_insensitively():
    plan = ar.plan_reconcile([_row("1", "A@X.com")], [_sub("a@x.com", sub_arn("a"))], BOOTSTRAP, NOW)
    assert _by_id(plan)["1"].to_status == "confirmed"


def test_plan_removed_by_admin_then_confirmed_is_unsubscribed_again():
    rows = [_row("old", "late@x.com", "removed", reason="admin")]
    plan = ar.plan_reconcile(rows, [_sub("late@x.com", sub_arn("late"))], BOOTSTRAP, NOW)
    assert plan.unsubscribe == [ar.Unsubscribe("old", "late@x.com", sub_arn("late"))]
    assert plan.unmanaged == []


def test_plan_removed_but_active_again_is_not_unsubscribed():
    rows = [_row("old", "a@x.com", "removed", reason="admin"), _row("new", "a@x.com")]
    plan = ar.plan_reconcile(rows, [_sub("a@x.com", sub_arn("a"))], BOOTSTRAP, NOW)
    assert plan.unsubscribe == [] and _by_id(plan)["new"].to_status == "confirmed"


def test_plan_removed_still_pending_in_sns_is_left_to_expire():
    rows = [_row("old", "p@x.com", "removed", reason="admin")]
    plan = ar.plan_reconcile(rows, [_sub("p@x.com", "PendingConfirmation")], BOOTSTRAP, NOW)
    assert plan.unsubscribe == [] and plan.unmanaged == [{"email": "p@x.com", "state": "pending"}]


def test_plan_bootstrap_is_locked_and_never_unsubscribed():
    rows = [_row("old", BOOTSTRAP, "removed", reason="admin")]
    plan = ar.plan_reconcile(rows, [_sub(BOOTSTRAP, sub_arn("boot"))], BOOTSTRAP, NOW)
    assert plan.locked == {"email": BOOTSTRAP, "state": "confirmed"}
    assert plan.unsubscribe == [] and plan.unmanaged == []


def test_plan_bootstrap_missing_is_reported():
    assert ar.plan_reconcile([], [], BOOTSTRAP, NOW).locked == {"email": BOOTSTRAP, "state": "missing"}


def test_plan_unknown_subscription_is_unmanaged():
    plan = ar.plan_reconcile([], [_sub("console@x.com", sub_arn("c"))], BOOTSTRAP, NOW)
    assert plan.unmanaged == [{"email": "console@x.com", "state": "confirmed"}]


# ── reconcile (apply) ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_applies_and_audits_changes():
    db = AlertDB()
    keep = db.insert("keep@x.com", "confirmed", arn=sub_arn("keep"))
    gone = db.insert("gone@x.com", "confirmed", arn=sub_arn("gone"))
    new = db.insert("new@x.com", "pending", arn=sub_arn("new"))
    old = db.insert("late@x.com", "removed", reason="admin")
    sns = StubSns(subs={"keep@x.com": sub_arn("keep"), "new@x.com": sub_arn("new"), "late@x.com": sub_arn("late"),
                        BOOTSTRAP: sub_arn("boot")})
    result = await ar.reconcile(db, sns, ar.topic_config(), now=NOW)
    assert db.row(gone)["status"] == "removed" and db.row(gone)["removed_reason"] == "unsubscribed"
    assert db.row(gone)["removed_by"] == "reconcile"
    assert db.row(new)["status"] == "confirmed" and db.row(new)["confirmed_at"] is not None
    assert db.row(keep)["last_seen_state"] == "confirmed"
    assert sns.unsubscribed == [sub_arn("late")]
    assert result.unsubscribed == ["late@x.com"] and result.locked["state"] == "confirmed"
    actions = [(a["action"], a["resource_id"], a["details"].get("to")) for a in db.audit]
    assert ("alert_recipient.reconciled", gone, "removed") in actions
    assert ("alert_recipient.reconciled", new, "confirmed") in actions
    assert ("alert_recipient.reconciled", old, "removed") in actions          # the re-unsubscribe
    assert not any(a["resource_id"] == keep for a in db.audit)
    assert "arn:" not in repr(result.summary())


@pytest.mark.asyncio
async def test_reconcile_throttled_uses_the_cache_and_force_skips_it():
    db = AlertDB()
    sns = StubSns(subs={BOOTSTRAP: sub_arn("boot")})
    cfg = ar.topic_config()
    first = await ar.reconcile_throttled(db, sns, cfg)
    second = await ar.reconcile_throttled(db, sns, cfg)
    assert not first.cached and second.cached and sns.list_calls == 1
    await ar.reconcile_throttled(db, sns, cfg, force=True)
    assert sns.list_calls == 2


@pytest.mark.asyncio
async def test_reconcile_throttled_reports_sns_errors_and_changes_nothing():
    db = AlertDB()
    rid = db.insert("a@x.com", "confirmed", arn=sub_arn("a"))
    result = await ar.reconcile_throttled(db, StubSns(list_error="AuthorizationError"), ar.topic_config())
    assert result.error == "AuthorizationError" and db.row(rid)["status"] == "confirmed"


class _Redis:
    def __init__(self, fail=False):
        self.data, self.fail = {}, fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value


@pytest.mark.asyncio
async def test_reconcile_cache_in_redis_and_survives_redis_down():
    db, sns, cfg = AlertDB(), StubSns(subs={}), ar.topic_config()
    redis = _Redis()
    await ar.reconcile_throttled(db, sns, cfg, redis=redis)
    assert ar.RECONCILE_CACHE_KEY in redis.data and "arn:" not in redis.data[ar.RECONCILE_CACHE_KEY]
    ar.clear_cache()
    assert (await ar.reconcile_throttled(db, sns, cfg, redis=redis)).cached and sns.list_calls == 1
    out = await ar.reconcile_throttled(db, sns, cfg, redis=_Redis(fail=True), force=True)
    assert out.error is None and sns.list_calls == 2


# ── ops recipients ───────────────────────────────────────────────────────────────────────────────

def test_merge_recipients_dedups_lower_cases_and_caps():
    assert ar.merge_recipients(["A@x.com", "b@x.com", "a@x.com"], "B@X.com") == ["a@x.com", "b@x.com"]
    assert ar.merge_recipients([], None) == []
    assert len(ar.merge_recipients([f"u{i}@x.com" for i in range(60)], "f@x.com")) == ar.MAX_OPS_RECIPIENTS


@pytest.mark.asyncio
async def test_ops_recipients_are_confirmed_rows_plus_the_fallback(monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "Ops@Adventure.Asia")
    db = AlertDB()
    db.insert("c@x.com", "confirmed", arn=sub_arn("c"))
    db.insert("p@x.com", "pending", arn=sub_arn("p"))
    db.insert("r@x.com", "removed", reason="admin")
    assert await ar.ops_recipients(db) == (["c@x.com", "ops@adventure.asia"], None)


@pytest.mark.asyncio
async def test_ops_recipients_fall_back_when_the_table_cannot_be_read(monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    db = AlertDB()
    db.fail_reads = True
    to, error = await ar.ops_recipients(db)
    assert to == ["ops@adventure.asia"] and error


# ── rate limits (pure) ───────────────────────────────────────────────────────────────────────────

def test_rate_limit_interval():
    out = svc.rate_limit(1, NOW - timedelta(minutes=4), NOW - timedelta(minutes=4), NOW,
                         min_interval_s=ar.RESEND_MIN_INTERVAL_S, max_per_day=ar.RESEND_MAX_PER_DAY, what="Resend")
    assert out is not None and out.retry_after_s == 6 * 60


def test_rate_limit_per_day():
    first = NOW - timedelta(hours=20)
    out = svc.rate_limit(3, first, NOW - timedelta(hours=1), NOW, min_interval_s=ar.RESEND_MIN_INTERVAL_S,
                         max_per_day=ar.RESEND_MAX_PER_DAY, what="Resend")
    assert out is not None and out.retry_after_s == 4 * 3600


def test_rate_limit_allows():
    assert svc.rate_limit(2, NOW - timedelta(hours=5), NOW - timedelta(minutes=11), NOW,
                          min_interval_s=ar.RESEND_MIN_INTERVAL_S, max_per_day=ar.RESEND_MAX_PER_DAY,
                          what="Resend") is None
    assert svc.rate_limit(0, None, None, NOW, min_interval_s=600, max_per_day=3, what="x") is None


def test_rate_limit_adds_per_day_and_test_interval():
    assert svc.rate_limit(10, NOW - timedelta(hours=2), NOW, NOW, min_interval_s=None,
                          max_per_day=ar.MAX_ADDS_PER_DAY, what="Adding") is not None
    assert svc.rate_limit(9, NOW - timedelta(hours=2), NOW, NOW, min_interval_s=None,
                          max_per_day=ar.MAX_ADDS_PER_DAY, what="Adding") is None
    assert svc.rate_limit(1, NOW - timedelta(minutes=1), NOW - timedelta(minutes=1), NOW,
                          min_interval_s=ar.TEST_MIN_INTERVAL_S, max_per_day=None, what="Test") is not None


def test_ops_test_key_is_per_utc_minute():
    assert svc.ops_test_key(NOW) == "ops_alert_email:test:2026-10-20T12:00"


# ── ops_alert_email job ──────────────────────────────────────────────────────────────────────────

MAIL_ENV = {"SES_FROM_ADDRESS": "no-reply@lumiguides.it.com", "SES_CONFIGURATION_SET": "aa-email-dev-transactional"}


class StubSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "msg-1"}


def _ctx(db, payload, ses, sns=None):
    from services.jobs import ops_alert_email_job  # noqa: F401 — registers the kind

    job = Job(id="job-1", kind="ops_alert_email", payload=payload, status="running", attempt=1,
              max_attempts=5, progress={}, cost_usd=0.0)
    resources = {"ses": ses}
    if sns is not None:
        resources["sns"] = sns
    return registry.JobContext(db, job, resources)


@pytest.fixture
def mail_env(monkeypatch):
    for k, v in MAIL_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.mark.asyncio
async def test_job_mails_confirmed_rows_and_the_fallback_in_one_message(mail_env, monkeypatch):
    from services.jobs.ops_alert_email_job import run

    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    db = AlertDB()
    db.insert("c@x.com", "confirmed", arn=sub_arn("c"))
    db.insert("p@x.com", "pending", arn=sub_arn("p"))
    ses = StubSes()
    sns = StubSns(subs={"c@x.com": sub_arn("c"), "p@x.com": "PendingConfirmation", BOOTSTRAP: sub_arn("b")})
    out = await run(_ctx(db, {"template": "test", "requested_by": "Nghiep"}, ses, sns))
    assert out["status"] == "sent" and out["to"] == ["c@x.com", "ops@adventure.asia"]
    assert out["recipients_count"] == 2 and "reconcile_error" not in out
    assert ses.calls[0]["Destination"] == {"ToAddresses": ["c@x.com", "ops@adventure.asia"]}
    assert ses.calls[0]["Content"]["Simple"]["Subject"]["Data"].endswith("Test alert e-mail")
    assert sns.list_calls == 1


@pytest.mark.asyncio
async def test_job_drops_an_address_that_unsubscribed(mail_env, monkeypatch):
    from services.jobs.ops_alert_email_job import run

    db = AlertDB()
    db.insert("gone@x.com", "confirmed", arn=sub_arn("gone"))
    db.insert("c@x.com", "confirmed", arn=sub_arn("c"))
    ses = StubSes()
    out = await run(_ctx(db, {"template": "test"}, ses, StubSns(subs={"c@x.com": sub_arn("c")})))
    assert out["to"] == ["c@x.com"]


@pytest.mark.asyncio
async def test_job_skips_with_nobody_to_mail(mail_env):
    from services.jobs.ops_alert_email_job import run

    ses = StubSes()
    out = await run(_ctx(AlertDB(), {"template": "test"}, ses, StubSns(subs={})))
    assert out["status"] == "skipped" and "no confirmed recipient" in out["reason"] and not ses.calls


@pytest.mark.asyncio
async def test_job_reconcile_failure_still_mails_the_table(mail_env, monkeypatch):
    from services.jobs.ops_alert_email_job import run

    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    db = AlertDB()
    db.insert("c@x.com", "confirmed", arn=sub_arn("c"))
    ses = StubSes()
    out = await run(_ctx(db, {"template": "test"}, ses, StubSns(list_error="Throttled")))
    assert out["status"] == "sent" and out["to"] == ["c@x.com", "ops@adventure.asia"]
    assert out["reconcile_error"] == "Throttled"


@pytest.mark.asyncio
async def test_job_without_topic_uses_table_and_fallback(mail_env, monkeypatch):
    from services.jobs.ops_alert_email_job import run

    monkeypatch.delenv("ALERTS_TOPIC_ARN")
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    ses = StubSes()
    out = await run(_ctx(AlertDB(), {"template": "test"}, ses))
    assert out["to"] == ["ops@adventure.asia"]


@pytest.mark.asyncio
async def test_job_skips_without_ses_sender(monkeypatch):
    from services.jobs.ops_alert_email_job import run

    monkeypatch.delenv("SES_FROM_ADDRESS", raising=False)
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@adventure.asia")
    out = await run(_ctx(AlertDB(), {"template": "test"}, StubSes()))
    assert out == {"status": "skipped", "reason": "SES_FROM_ADDRESS unset"}


def test_render_test_template():
    msg = ops_alerts.render_test({"requested_by": "Nghiep <b>"}, to=("a@x.com", "b@x.com"),
                                 base_url="https://admin.example", environment="dev")
    assert msg.subject == "[AA-CIS dev] Test alert e-mail"
    assert "Platform > Alert recipients by Nghiep <b>" in msg.text
    assert "https://admin.example/admin/platform/alerts" in msg.text
    assert "Nghiep &lt;b&gt;" in msg.html and "no-reply@sns.amazonaws.com" in msg.text
    assert ses_mailer.to_addresses(msg.to) == ["a@x.com", "b@x.com"]


def test_single_address_message_still_works():
    assert ses_mailer.to_addresses("ops@example.com") == ["ops@example.com"]


@pytest.mark.asyncio
async def test_enqueue_gate_needs_only_the_sender(mail_env, monkeypatch):
    """AA-913: ALERT_EMAIL_TO may be empty now (recipients come from the table at send time)."""
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "job-1"})
    out = await ops_alerts.enqueue_spend_budget_email(conn, summary={"provider": "bedrock", "scope": "global"},
                                                      alert_pct=80, stopped=False, message="m", used_pct=81.0,
                                                      now=NOW)
    assert out == "job-1"
