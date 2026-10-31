"""AA-1018 (reliability part 1) — the connection-level circuit (shared/llm_client/breaker.py ``connection_health``).

Pure derivation from deployment rows; the breaker end to end in process (conftest stubs the DB, so the transitions run
on the in-process copy, the same fallback the S4 tests use); the plan and ``admit`` skipping every deployment of an
open connection; the alert on the transition to OPEN (bell + ops e-mail) against a fake connection. Refusals use the
documented provider bodies: Bedrock "Error 002" access denied, the Anthropic API tier spend cap
(429 ``enforced_spend_limit_reached``), and OpenAI ``insufficient_quota``.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shared.llm_client import breaker, vendor_route
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.breaker import CLOSED, CREDENTIAL, HALF_OPEN, OPEN, Health, connection_health, state_of
from shared.llm_client.connections import ConnectionCredentialError
from shared.llm_client.errors import ACCESS_DENIED, QUOTA_EXHAUSTED, LLMAccessDenied, classify
from shared.llm_client import catalog
from tests.unit.test_aa1002_s3_deployment_routing import EMBED, GPT6, HAIKU, _dep, world  # noqa: F401

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

# Bedrock on the LLM account since 29/10/2026 (the botocore ClientError text of a Converse call).
ERROR_002 = RuntimeError("An error occurred (AccessDeniedException) when calling the Converse operation: "
                         "Error 002: Access to Bedrock models is not allowed for this account")
# OpenAI API, no credit left (the documented insufficient_quota body as the SDK renders it).
NO_CREDIT = RuntimeError("RateLimitError: Error code: 429 - {'error': {'message': 'You exceeded your current quota, "
                         "please check your plan and billing details.', 'type': 'insufficient_quota', "
                         "'code': 'insufficient_quota'}}")
THROTTLE = RuntimeError("An error occurred (ThrottlingException) when calling the Converse operation: Too many "
                        "requests, please wait before trying again.")


def _spend_cap() -> am.AnthropicAPIError:
    """The Anthropic API tier spend cap: 429 rate_limit_error, error.details.error_code enforced_spend_limit_reached,
    classified by the adapter's own rule (status_error_class)."""
    body = {"type": "error", "error": {"type": "rate_limit_error",
                                       "message": "You have reached the monthly spend limit for your usage tier.",
                                       "details": {"error_code": "enforced_spend_limit_reached"}}}
    cls = am.status_error_class(429, "rate_limit_error", body["error"]["message"], am.error_code_of(body))
    return am.AnthropicAPIError(f"{am.SPEND_LIMIT_ERROR_CODE}: {body['error']['message']}", cls, status_code=429,
                                error_type="rate_limit_error")


def _row(model, cls, at=T0, conn="acc3", open_s=300, ok_at=None):
    return Health(conn, model, last_ok_at=ok_at, last_error_at=at, last_error_class=cls, last_error="boom",
                  consecutive_failures=1, circuit_open_until=at + timedelta(seconds=open_s), open_streak=1,
                  opened_at=at)


# ── pure derivation ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_one_model_refused_does_not_trip_the_connection():
    assert connection_health([_row("gpt-6-luna", ACCESS_DENIED)], "acc3", T0) is None


def test_access_denied_on_two_models_trips_it_until_the_newest_open_period_ends():
    rows = [_row("gpt-6-luna", ACCESS_DENIED), _row("sonnet-5-5", ACCESS_DENIED, at=T0 + timedelta(seconds=60))]
    h = connection_health(rows, "acc3", T0 + timedelta(seconds=60))
    assert h.model_key == breaker.CONNECTION_MODEL == "*"
    assert h.last_error_class == ACCESS_DENIED and h.consecutive_failures == 2 and h.opened_at == T0
    assert h.circuit_open_until == T0 + timedelta(seconds=360)
    assert state_of(h, T0 + timedelta(seconds=359)) == OPEN
    assert state_of(h, T0 + timedelta(seconds=360)) == HALF_OPEN
    assert breaker.connection_members(rows, "acc3", T0) == ["gpt-6-luna", "sonnet-5-5"]


def test_the_same_model_twice_is_still_one_model():
    rows = [_row("gpt-6-luna", ACCESS_DENIED), _row("gpt-6-luna", ACCESS_DENIED, conn="acc2")]
    assert connection_health(rows, "acc3", T0) is None and connection_health(rows, "acc2", T0) is None


@pytest.mark.parametrize("cls", [CREDENTIAL, QUOTA_EXHAUSTED])
def test_a_missing_key_or_no_credit_trips_it_from_one_deployment(cls):
    h = connection_health([_row("sonnet-5-5", cls, conn="anthropic-direct")], "anthropic-direct", T0)
    assert h is not None and h.last_error_class == cls and state_of(h, T0) == OPEN


def test_the_class_names_the_strongest_signal():
    rows = [_row("a", ACCESS_DENIED), _row("b", ACCESS_DENIED), _row("c", QUOTA_EXHAUSTED)]
    assert connection_health(rows, "acc3", T0).last_error_class == QUOTA_EXHAUSTED


@pytest.mark.parametrize("cls", ["throttled", "other"])
def test_throttles_and_outages_never_trip_the_connection(cls):
    rows = [_row(m, cls) for m in ("a", "b", "c")]
    assert connection_health(rows, "acc3", T0) is None


def test_an_answered_call_after_the_refusals_closes_it():
    rows = [_row("a", ACCESS_DENIED), _row("b", ACCESS_DENIED),
            Health("acc3", "c", last_ok_at=T0 + timedelta(seconds=5))]
    assert connection_health(rows, "acc3", T0 + timedelta(seconds=10)) is None
    # a refusal newer than that success counts again
    rows.append(_row("d", QUOTA_EXHAUSTED, at=T0 + timedelta(seconds=20)))
    assert connection_health(rows, "acc3", T0 + timedelta(seconds=30)).last_error_class == QUOTA_EXHAUSTED


def test_closed_deployment_rows_never_count():
    closed = Health("acc3", "a", last_error_at=T0, last_error_class=ACCESS_DENIED, consecutive_failures=0)
    assert connection_health([closed, _row("b", ACCESS_DENIED)], "acc3", T0) is None
    assert breaker.connection_state([closed], "acc3", T0) == CLOSED


# ── the breaker in process ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def alerts(monkeypatch):
    seen = []
    monkeypatch.setattr(breaker, "on_connection_open", lambda h, models: seen.append((h, models)))
    monkeypatch.setattr(breaker, "_now", lambda: T0)
    return seen


@pytest.mark.real_breaker
def test_error_002_on_two_models_opens_the_connection_once(alerts):
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    assert breaker.connection_open("acc3") is None and alerts == []
    assert breaker.admit("acc3", "haiku") is True                       # one model: the others still run
    breaker.record_failure("acc3", "sonnet-5-5", ERROR_002)
    assert breaker.connection_open("acc3") == ACCESS_DENIED
    assert breaker.admit("acc3", "haiku") is False                      # a model that never failed is skipped too
    assert breaker.open_class("acc3", "haiku") == ACCESS_DENIED
    breaker.record_failure("acc3", "haiku", ERROR_002)                  # a call in flight: no second alert
    (h, models), = alerts
    assert h.connection_key == "acc3" and h.last_error_class == ACCESS_DENIED
    assert models == ["gpt-6-luna", "sonnet-5-5"]


@pytest.mark.real_breaker
def test_another_connection_is_not_affected(alerts):
    for model in ("gpt-6-luna", "sonnet-5-5"):
        breaker.record_failure("acc3", model, ERROR_002)
    assert breaker.connection_open("acc3") == ACCESS_DENIED
    assert breaker.admit("acc2", "cohere-embed-v4") is True and breaker.connection_open("acc2") is None


@pytest.mark.real_breaker
def test_one_refused_model_on_acc2_keeps_its_embeddings(alerts):
    breaker.record_failure("acc2", "haiku", RuntimeError(
        "AccessDeniedException: anthropic.claude-haiku is not available for this account."))
    assert breaker.connection_open("acc2") is None and breaker.admit("acc2", "cohere-embed-v4") is True
    assert alerts == []


@pytest.mark.real_breaker
def test_the_anthropic_spend_cap_stops_the_connection(alerts):
    exc = _spend_cap()
    assert classify(exc) == QUOTA_EXHAUSTED
    breaker.record_failure("anthropic-direct", "sonnet-5-5", exc)
    assert breaker.connection_open("anthropic-direct") == QUOTA_EXHAUSTED
    assert breaker.admit("anthropic-direct", "haiku-5-5") is False
    assert alerts[0][0].last_error_class == QUOTA_EXHAUSTED


@pytest.mark.real_breaker
def test_openai_insufficient_quota_stops_the_connection(alerts):
    assert classify(NO_CREDIT) == QUOTA_EXHAUSTED
    breaker.record_failure("openai-direct", "gpt-6-luna-openai", NO_CREDIT)
    assert breaker.connection_open("openai-direct") == QUOTA_EXHAUSTED
    assert breaker.admit("openai-direct", "gpt-4.1") is False


@pytest.mark.real_breaker
def test_an_unreadable_key_stops_the_connection(alerts):
    breaker.record_failure("anthropic-direct", "sonnet-5-5", ConnectionCredentialError("no secret value"))
    assert breaker.connection_open("anthropic-direct") == CREDENTIAL


@pytest.mark.real_breaker
def test_throttling_never_opens_the_connection(alerts):
    for model in ("a", "b", "c"):
        for _ in range(3):
            breaker.record_failure("acc3", model, THROTTLE)
    assert breaker.connection_open("acc3") is None and alerts == []


@pytest.mark.real_breaker
def test_after_the_open_period_the_deployment_circuits_decide(monkeypatch, alerts):
    clock = {"now": T0}
    monkeypatch.setattr(breaker, "_now", lambda: clock["now"])
    for model in ("gpt-6-luna", "sonnet-5-5"):
        breaker.record_failure("acc3", model, ERROR_002)
    clock["now"] = T0 + timedelta(seconds=301)
    assert breaker.connection_open("acc3") is None                    # half-open: not blocking
    assert breaker.admit("acc3", "haiku") is True                       # a model that never failed runs
    assert breaker.admit("acc3", "gpt-6-luna") is True                  # the probe of a tripping row
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)             # the probe fails: 600 s, connection open
    assert breaker.connection_open("acc3") == ACCESS_DENIED
    assert len(alerts) == 2                                             # a new opening is a new alert (bell throttles)


@pytest.mark.real_breaker
def test_a_success_closes_the_connection_at_once(alerts):
    for model in ("gpt-6-luna", "sonnet-5-5"):
        breaker.record_failure("acc3", model, ERROR_002)
    breaker.record_success("acc3", "haiku")                             # e.g. the admin's Test connection
    assert breaker.connection_open("acc3") is None
    assert breaker.is_open("acc3", "gpt-6-luna")                       # the model's own circuit stays open


def test_the_switch_off_answers_closed(monkeypatch):
    monkeypatch.setenv(breaker.SWITCH_ENV, "off")
    with breaker._lock:
        breaker._rows.update({("acc3", m): _row(m, ACCESS_DENIED) for m in ("a", "b")})
    monkeypatch.setattr(breaker, "_now", lambda: T0)
    assert breaker.connection_open("acc3") is None and breaker.admit("acc3", "c") is True


def test_the_plan_checks_the_connection_hook():
    assert vendor_route.connection_circuit_open is breaker.connection_open


# ── through the plan and LLMClient (the S3 fake world) ──────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_the_plan_skips_every_deployment_of_an_open_connection(world, alerts):
    for model in ("gpt-6-luna", "sonnet-5-5"):
        breaker.record_failure("acc3", model, ERROR_002)
    plan = vendor_route.deployment_plan(HAIKU)
    assert list(plan) == [] and plan.circuit_skips == [("acc3", ACCESS_DENIED)]
    embed_plan = vendor_route.deployment_plan(EMBED)
    assert [d.connection.connection_key for d in embed_plan] == ["acc2"]


@pytest.mark.real_breaker
def test_a_job_on_a_blocked_connection_fails_fast_without_a_provider_call(world, alerts, monkeypatch):
    # a third catalog model on acc3 (invented key), so the route goes through the deployment plan
    monkeypatch.setitem(catalog._models, "gpt-6-astra", replace(GPT6, model_key="gpt-6-astra"))
    world.deps.append(_dep("gpt-6-astra", "acc3", "global.openai.gpt-6-astra", 10, origin="bedrock_profile"))
    world.reset()
    world.fail = {"acc3": ERROR_002}
    for model in ("gpt-6-luna", "sonnet-5-5"):
        with pytest.raises(LLMAccessDenied):
            world.generate(model_tier=model)
    n = len(world.calls)
    with pytest.raises(LLMAccessDenied, match="circuit open on acc3"):
        world.generate(model_tier="gpt-6-astra")                        # never failed itself: still not called
    assert len(world.calls) == n


# ── the alert (bell + e-mail) ───────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, recent=False):
        self.recent = recent
        self.executed = []

    async def fetchval(self, sql, *args):
        assert "shared.notifications" in sql
        return 1 if self.recent else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_the_alert_writes_one_bell_and_queues_the_email(monkeypatch):
    from shared import ops_alerts
    sent = []

    async def enqueue(conn, *, details, now=None):
        sent.append(details)
        return "job-1"
    monkeypatch.setattr(ops_alerts, "enqueue_llm_circuit_email", enqueue)
    h = connection_health([_row("a", ACCESS_DENIED), _row("b", ACCESS_DENIED)], "acc3", T0)
    conn = _Conn()
    assert await breaker.alert_connection_open(conn, h, ["a", "b"]) is True
    (sql, args), = conn.executed
    assert "INSERT INTO shared.notifications" in sql and "ARRAY['admin']" in sql
    assert args[1] == "llm_circuit_open" and args[2] == "llm_connection:acc3"
    assert sent[0]["connection_key"] == "acc3" and sent[0]["models"] == ["a", "b"]
    assert "paused" in sent[0]["message"]


@pytest.mark.asyncio
async def test_an_unread_bell_of_the_last_24_hours_throttles_it(monkeypatch):
    from shared import ops_alerts
    monkeypatch.setattr(ops_alerts, "enqueue_llm_circuit_email", None)   # must not be reached
    h = connection_health([_row("a", QUOTA_EXHAUSTED)], "acc3", T0)
    conn = _Conn(recent=True)
    assert await breaker.alert_connection_open(conn, h, ["a"]) is False and conn.executed == []


@pytest.mark.real_breaker
def test_a_failing_alert_never_breaks_the_call_path(monkeypatch):
    def boom(h, models):
        raise RuntimeError("db down")
    monkeypatch.setattr(breaker, "on_connection_open", boom)
    monkeypatch.setattr(breaker, "_now", lambda: T0)
    breaker.record_failure("anthropic-direct", "sonnet-5-5", _spend_cap())
    assert breaker.connection_open("anthropic-direct") == QUOTA_EXHAUSTED


def test_the_circuit_email_template_is_rendered():
    from services.jobs import ops_alert_email_job
    from shared import ops_alerts
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_LLM_CIRCUIT_OPEN] is ops_alerts.render_llm_circuit_open
    msg = ops_alerts.render_llm_circuit_open(
        {"connection_key": "openai-direct", "error_class": "quota_exhausted", "models": ["gpt-4.1"],
         "open_until": "2026-10-30T12:05:00+00:00", "period": "2026-10-30"},
        to=("ops@example.com",), base_url="https://admin.example.com", environment="dev")
    assert msg.subject == "[AA-CIS dev] LLM connection openai-direct paused (quota_exhausted)"
    assert "Top up or raise the cap" in msg.text and "https://admin.example.com/admin/settings?tab=models" in msg.text
    assert "<table" in msg.html


@pytest.mark.asyncio
async def test_the_circuit_email_is_once_per_connection_class_and_day(monkeypatch):
    from shared import ops_alerts
    from shared.jobs import queue
    keys = []

    async def enqueue(conn, kind, payload, **kw):
        keys.append(kw["idempotency_key"])
        return "job-1", len(keys) == 1
    monkeypatch.setattr(queue, "enqueue", enqueue)
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: object())
    details = {"connection_key": "acc3", "error_class": "access_denied", "models": ["a", "b"]}
    assert await ops_alerts.enqueue_llm_circuit_email(None, details=details, now=T0) == "job-1"
    assert await ops_alerts.enqueue_llm_circuit_email(None, details=details, now=T0) is None
    assert keys == ["ops_alert_email:llm_circuit_open:acc3:access_denied:2026-10-30"] * 2
