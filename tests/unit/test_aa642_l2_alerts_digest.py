"""AA-642 (S263 lane L2, B4) — e-mail for the DataForSEO low balance and the LLM cost reconcile mismatch, and the
digest's balances section.

- Both e-mails are queued only when the caller just wrote its bell row, at most once per UTC day, and render without
  a secret or an account ID; the ops_alert_email job knows both templates.
- The daily DataForSEO balance check is the provider's probe (last_probe_at + access_state) and calls nothing while
  the connection is switched off.
- The digest reads the balances from stored rows (money_sources.balances, the provider rows, geo.geocode_call, the
  reconcile rows) and flags a low balance, a refusing or switched-off provider, the Geoapify limit and a mismatch.
Mock-only: no provider call, no DB, no SES.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.jobs import ops_alert_email_job
from services.llm_cost import reconcile
from services.reports import digest, registry
from shared import ops_alerts
from shared.llm_client import connections
from shared.llm_client.connections import BUILTIN_PROVIDERS

UTC = timezone.utc
T0 = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)
D = date(2026, 10, 29)


@pytest.fixture
def queued(monkeypatch):
    from shared.jobs import queue
    calls = []

    async def enqueue(conn, kind, payload, **kw):
        calls.append((kind, payload, kw))
        return f"job-{len(calls)}", kw["idempotency_key"] not in [c[2]["idempotency_key"] for c in calls[:-1]]
    monkeypatch.setattr(queue, "enqueue", enqueue)
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: object())
    return calls


# ── the two e-mails ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_dfs_low_balance_email_is_once_per_day(queued):
    details = {"balance_usd": 4.5, "threshold_usd": 10.0, "currency": "USD", "fetched_at": "2026-10-30T05:00:00Z"}
    assert await ops_alerts.enqueue_dfs_balance_low_email(None, details=details, now=T0) == "job-1"
    assert await ops_alerts.enqueue_dfs_balance_low_email(None, details=details, now=T0) is None
    kind, payload, kw = queued[0]
    assert kind == ops_alerts.KIND and kw["idempotency_key"] == "ops_alert_email:dfs_balance_low:2026-10-30"
    assert payload["template"] == "dfs_balance_low" and payload["balance_usd"] == 4.5


@pytest.mark.asyncio
async def test_the_mismatch_email_keeps_at_most_ten_rows_and_counts_all(queued):
    rows = [{"connection_key": f"conn-{i}", "day": "2026-10-28", "logged_usd": 1.0, "provider_usd": 2.0,
             "diff_usd": -1.0} for i in range(14)]
    await ops_alerts.enqueue_llm_cost_mismatch_email(None, details={"window": {}, "mismatches": rows}, now=T0)
    payload = queued[0][1]
    assert len(payload["mismatches"]) == ops_alerts.MISMATCH_ROWS_IN_MAIL and payload["mismatch_count"] == 14
    assert queued[0][2]["idempotency_key"] == "ops_alert_email:llm_cost_mismatch:2026-10-30"


@pytest.mark.asyncio
async def test_no_email_when_mail_is_not_configured(monkeypatch):
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: None)
    assert await ops_alerts.enqueue_dfs_balance_low_email(None, details={}) is None
    assert await ops_alerts.enqueue_llm_cost_mismatch_email(None, details={}) is None


def test_both_templates_render_and_are_known_to_the_job():
    for template in (ops_alerts.TEMPLATE_DFS_BALANCE_LOW, ops_alerts.TEMPLATE_LLM_COST_MISMATCH):
        assert template in ops_alert_email_job._RENDERERS
    msg = ops_alerts.render_dfs_balance_low(
        {"template": "dfs_balance_low", "period": "2026-10-30", "balance_usd": 4.5, "threshold_usd": 10,
         "currency": "USD", "fetched_at": "2026-10-30T05:00:00Z"}, to="ops@example.com", base_url="https://x.test",
        environment="dev")
    assert "DataForSEO balance low" in msg.subject and "4.50" in msg.subject
    assert "https://x.test/admin/cost-ops?tab=spend&view=dfs" in msg.text
    rows = [{"connection_key": "openai-direct", "day": "2026-10-28", "logged_usd": 1.0, "provider_usd": 1.6,
             "diff_usd": -0.6}]
    msg = ops_alerts.render_llm_cost_mismatch(
        {"template": "llm_cost_mismatch", "period": "2026-10-30", "mismatches": rows, "mismatch_count": 12,
         "window": {"start": "2026-10-27", "end": "2026-10-29"}}, to="ops@example.com", base_url="https://x.test",
        environment="dev")
    assert "12 connection-day(s)" in msg.subject and "openai-direct" in msg.subject
    assert "... and 11 more" in msg.text and "/admin/ai-gateway?tab=providers&view=connections" in msg.text


# ── the callers queue it only with a new bell ─────────────────────────────────────────────────────────────────────

def _bell_conn(existing):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=existing)
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_a_new_dfs_bell_queues_the_email_and_a_throttled_one_does_not(monkeypatch):
    from api.routers import admin_llm_ops
    email = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ops_alerts, "enqueue_dfs_balance_low_email", email)
    assert await admin_llm_ops._maybe_alert_low_balance(_bell_conn(None), 4.5, 10.0, currency="USD") is True
    assert email.await_args.kwargs["details"]["balance_usd"] == 4.5
    email.reset_mock()
    assert await admin_llm_ops._maybe_alert_low_balance(_bell_conn(1), 4.5, 10.0) is False
    email.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_email_enqueue_keeps_the_bell(monkeypatch):
    from api.routers import admin_llm_ops
    monkeypatch.setattr(ops_alerts, "enqueue_dfs_balance_low_email", AsyncMock(side_effect=RuntimeError("x")))
    assert await admin_llm_ops._maybe_alert_low_balance(_bell_conn(None), 4.5, 10.0) is True


@pytest.mark.asyncio
async def test_a_new_mismatch_bell_queues_the_email(monkeypatch):
    email = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ops_alerts, "enqueue_llm_cost_mismatch_email", email)
    mismatches = [{"connection_key": "openai-direct", "day": "2026-10-28", "logged_usd": 1.0, "provider_usd": 1.6,
                   "diff_usd": -0.6}]
    window = {"start": "2026-10-27", "end": "2026-10-29", "days": 3}
    assert await reconcile._mismatch_bell(_bell_conn(None), mismatches, window) is True
    assert email.await_args.kwargs["details"] == {"window": window, "mismatches": mismatches}
    email.reset_mock()
    assert await reconcile._mismatch_bell(_bell_conn(1), mismatches, window) is False
    email.assert_not_awaited()


# ── the daily DataForSEO balance check is the provider's probe ───────────────────────────────────────────────────

def _request(pool=None):
    request = MagicMock()
    request.app.state.pool = pool or MagicMock()
    return request


@pytest.fixture
def stamps(monkeypatch):
    from api.routers import admin_llm_ops
    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    monkeypatch.setattr(admin_llm_ops, "dfs_balance_threshold", AsyncMock(return_value=10.0))
    writes = []

    async def writer(key, state, probed):
        writes.append((key, state, probed))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    return writes


@pytest.mark.asyncio
async def test_the_balance_check_calls_nothing_while_switched_off(monkeypatch, stamps):
    from api.routers import admin_llm_ops

    async def provider_connection(db, key):
        return dataclasses.replace(BUILTIN_PROVIDERS["dataforseo"], enabled=False)
    monkeypatch.setattr(connections, "provider_connection", provider_connection)
    with patch("services.seo_intelligence.dataforseo_client.DataForSEOClient.fetch_balance",
               new=AsyncMock()) as fetch:
        out = await admin_llm_ops.check_dfs_balance(_request(), x_admin_secret="x")
    fetch.assert_not_awaited()
    assert "switched off" in out["skipped"] and out["balance_usd"] is None
    assert stamps == []


@pytest.mark.asyncio
async def test_a_good_balance_read_stamps_the_probe(monkeypatch, stamps):
    from api.routers import admin_llm_ops
    monkeypatch.setattr(admin_llm_ops, "record_balance_snapshot",
                        AsyncMock(return_value={"fetched_at": "2026-10-30T05:00:00+00:00"}))
    with patch("services.seo_intelligence.dataforseo_client.DataForSEOClient.fetch_balance",
               new=AsyncMock(return_value={"balance": 25.0, "currency": "USD"})):
        out = await admin_llm_ops.check_dfs_balance(_request(), x_admin_secret="x")
    assert out["balance_usd"] == 25.0 and out["below_threshold"] is False
    assert stamps == [("dataforseo", "ok", True)]


@pytest.mark.asyncio
async def test_a_payment_refusal_stamps_quota_exhausted(stamps):
    from api.routers import admin_llm_ops
    from services.seo_intelligence.dataforseo_client import DFSCallError
    with patch("services.seo_intelligence.dataforseo_client.DataForSEOClient.fetch_balance",
               new=AsyncMock(side_effect=DFSCallError("payment", fatal=True, status_code=402))):
        with pytest.raises(HTTPException) as exc:
            await admin_llm_ops.check_dfs_balance(_request(), x_admin_secret="x")
    assert exc.value.status_code == 502
    assert stamps == [("dataforseo", "quota_exhausted", True)]


# ── the digest's balances section ────────────────────────────────────────────────────────────────────────────────

def _balances(**over):
    b = {"dfs": {"balance_usd": 25.0, "fetched_at": "2026-10-29T05:00:00+00:00", "below_threshold": False},
         "dfs_threshold_usd": 10.0,
         "jev": {"estimated_left_usd": 12.0, "total_topped_up_usd": 20.0}, "jev_threshold_usd": 3.0,
         "geoapify": {"credits_used_today": 120, "daily_credit_limit": 2500},
         "credits": {"acc3": {"remaining_usd": 249.9, "expires_on": "2026-12-31"}},
         "providers": [{"connection_key": k, "enabled": True, "access_state": "ok",
                        "last_probe_at": "2026-10-29T05:00:00+00:00"} for k in ("typesafe", "dataforseo", "geoapify")],
         "reconcile": {"ready": True, "days": 3, "mismatches": []}}
    b.update(over)
    return b


def test_green_balances_add_no_attention_and_render_a_section():
    assert digest.balance_attention(_balances()) == []
    md = "\n".join(digest.render_balances(_balances()))
    assert "## Balances and paid providers" in md and "| DataForSEO | 25.00 USD | 10.00 USD |" in md
    assert "| Geoapify | 2,380 credits today |" in md and "no settled mismatch" in md
    assert digest.render_balances(None) == [] and digest.balance_attention(None) == []


def test_every_balance_problem_is_an_attention_line():
    b = _balances(dfs={"balance_usd": 4.0, "fetched_at": "t"}, jev={"estimated_left_usd": 1.0},
                  geoapify={"credits_used_today": 2500, "daily_credit_limit": 2500},
                  providers=[{"connection_key": "typesafe", "enabled": False, "access_state": "ok"},
                             {"connection_key": "dataforseo", "enabled": True, "access_state": "quota_exhausted"},
                             {"connection_key": "geoapify", "enabled": None, "access_state": None}],
                  reconcile={"ready": True, "days": 3, "mismatches": [
                      {"connection_key": "openai-direct", "day": "2026-10-28", "logged_usd": 1.0,
                       "provider_usd": 1.6, "diff_usd": -0.6}]})
    items = digest.balance_attention(b)
    assert any(i.startswith("DataForSEO balance 4.00 USD is below") for i in items)
    assert any(i.startswith("Jev credit about 1.00 USD left") for i in items)
    assert any(i.startswith("Geoapify daily credits used up") for i in items)
    assert "Provider typesafe is switched off: its stages run without it" in items
    assert "Provider dataforseo refuses (quota_exhausted)" in items
    assert any("LLM cost reconcile: 1 settled connection-day(s)" in i for i in items)
    assert len(items) == 6


def test_the_digest_carries_the_section_and_its_attention():
    from tests.unit.test_aa1003_r2_digest_email import _green_facts
    facts = _green_facts()
    facts["balances"] = _balances(dfs={"balance_usd": 4.0, "fetched_at": "t"})
    ops12 = registry.load().report("OPS-12")
    out = digest.render_digest(registry.load(), ops12, D, facts, [], T0)
    assert any("DataForSEO balance" in i for i in out.output["attention"])
    assert "## Balances and paid providers" in out.markdown


@pytest.mark.asyncio
async def test_load_balances_reads_stored_rows_only(monkeypatch):
    from services.places import store as place_store
    from services.reports import money_sources

    monkeypatch.setattr(money_sources, "balances", AsyncMock(return_value={
        "dfs": {"balance_usd": 8.0, "fetched_at": "t", "below_threshold": True},
        "jev": {"estimated_left_usd": 5.0}, "credits": {}}))

    async def provider_connection(db, key):
        row = BUILTIN_PROVIDERS[key]
        return dataclasses.replace(row, low_balance_usd=7.5) if key == "dataforseo" else row
    monkeypatch.setattr(connections, "provider_connection", provider_connection)
    monkeypatch.setattr(place_store, "geocoder_requests_today", AsyncMock(return_value=42))
    monkeypatch.setattr(reconcile, "read_recent", AsyncMock(return_value={
        "openai-direct": [{"day": "2026-10-29", "status": "mismatch", "logged_usd": 1.0, "provider_usd": 2.0,
                           "diff_usd": -1.0, "details": {}},
                          {"day": "2026-10-28", "status": "mismatch", "logged_usd": 1.0, "provider_usd": 2.0,
                           "diff_usd": -1.0, "details": {"provisional": True}}],
        "acc2": [{"day": "2026-10-29", "status": "ok", "details": {}}]}))
    out = await digest.load_balances(MagicMock(), T0)
    assert out["dfs_threshold_usd"] == 7.5 and out["jev_threshold_usd"] == 3.0
    assert out["geoapify"] == {"credits_used_today": 42, "daily_credit_limit": 2500}
    assert [p["connection_key"] for p in out["providers"]] == ["typesafe", "dataforseo", "geoapify"]
    assert out["reconcile"]["mismatches"] == [{"connection_key": "openai-direct", "day": "2026-10-29",
                                               "logged_usd": 1.0, "provider_usd": 2.0, "diff_usd": -1.0}]
