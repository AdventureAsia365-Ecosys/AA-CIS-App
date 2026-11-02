"""AA-1025 B5 (S263 gateway plan) — the credit ledger of the vendor API connections (shared/provider_credit.py), its
low-credit alert, the two new operator e-mails, the Providers page blocks (balance, circuit) and the admin routes.

- the estimate: the newest balance read (or the first top-up) plus the top-ups after it, minus the spend since;
- the alert: below the connection's low_balance_usd one bell (throttled per connection) and one e-mail job; none
  without a threshold or an estimate; Jev is never in this ledger;
- the Providers page: a ledger connection's row carries the balance and turns "low" below its threshold; a provider
  row with an open circuit turns "open" unless switched off;
- the routes: validation, 404 for a connection without a ledger, 503 before migration 342, the audit row.
Mock-only: no DB (the SQL runs on PostgreSQL in tests/integration/test_aa1025_b3b5_db.py).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.routers import admin_ai_gateway as routes
from api.services import ai_gateway as gw
from services.jobs import ops_alert_email_job
from shared import ops_alerts
from shared import provider_credit as pcr
from shared.llm_client.connections import BUILTIN_PROVIDERS, ProviderConnection

T1 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)


def _e(i, kind, amount, at):
    return {"id": i, "connection_key": "openai-direct", "kind": kind, "amount_usd": amount, "effective_at": at,
            "note": None, "created_by": "admin:test", "created_at": at}


def _openai(threshold=5.0, source="topups_minus_spend", **kw):
    return ProviderConnection(connection_key="openai-direct", kind="openai", auth_mode="api_key", label="OpenAI API",
                              data_scope="vendor_direct", enabled=True, balance_source=source,
                              low_balance_usd=threshold, **kw)


# ── the estimate (pure) ─────────────────────────────────────────────────────────────────────────────────────────

def test_no_entry_no_estimate():
    assert pcr.estimate_from([], None)["estimated_left_usd"] is None
    assert pcr.anchor_of([]) == (None, [])


def test_top_ups_only_start_at_the_first_one():
    entries = [_e(1, "topup", 10.0, T1), _e(2, "topup", 5.0, T2)]
    est = pcr.estimate_from(entries, 3.25)
    assert est == {"recorded_usd": 15.0, "since": T1.isoformat(), "spent_usd": 3.25, "estimated_left_usd": 11.75,
                   "anchor_kind": "topup"}


def test_the_newest_balance_read_restarts_the_estimate():
    entries = [_e(1, "topup", 10.0, T1), _e(2, "opening_balance", 4.2, T2), _e(3, "topup", 20.0, T3)]
    anchor, after = pcr.anchor_of(entries)
    assert anchor["id"] == 2 and [e["id"] for e in after] == [3]
    est = pcr.estimate_from(entries, 1.0)
    assert est["recorded_usd"] == 24.2 and est["estimated_left_usd"] == 23.2 and est["since"] == T2.isoformat()


def test_below_needs_both_a_threshold_and_an_estimate():
    assert pcr.is_below(4.0, 5.0) and not pcr.is_below(5.0, 5.0)
    assert not pcr.is_below(None, 5.0) and not pcr.is_below(4.0, None)


def test_only_topups_minus_spend_rows_and_never_jev():
    assert pcr.ledger_connection(_openai())
    assert not pcr.ledger_connection(_openai(source="none"))
    assert not pcr.ledger_connection(BUILTIN_PROVIDERS["typesafe"])         # Jev keeps shared.jev_credit_topup
    assert BUILTIN_PROVIDERS["typesafe"].balance_source == "topups_minus_spend"


def test_balance_block_has_the_jev_shape():
    b = pcr.balance_block(pcr.estimate_from([_e(1, "opening_balance", 4.2, T2)], 0.2), 5.0)
    assert b == {"usd": 4.0, "estimated": True, "threshold_usd": 5.0, "below_threshold": True, "as_of": None,
                 "source": "topups_minus_logged_spend", "topped_up_usd": 4.2, "since": T2.isoformat()}


# ── the low-credit check ────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    """The SQL the check runs, answered from memory."""

    def __init__(self, entries, spent=0.0, recent=False):
        self.entries, self.spent, self.recent, self.executed = entries, spent, recent, []

    async def fetch(self, sql, *args):
        if "provider_credit_topup" in sql:
            return self.entries
        return []

    async def fetchrow(self, sql, *args):
        assert "coalesce(connection_key, account) = $1" in sql
        return {"spent": self.spent}

    async def fetchval(self, sql, *args):
        return 1 if self.recent else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tx()


@pytest.fixture
def mail(monkeypatch):
    m = AsyncMock(return_value="job-7")
    monkeypatch.setattr(ops_alerts, "enqueue_provider_credit_email", m)
    return m


@pytest.mark.asyncio
async def test_below_the_threshold_one_bell_and_one_mail(mail):
    conn = _Conn([_e(1, "opening_balance", 4.2, T2)], spent=0.2)
    out = await pcr.check_connection(conn, _openai(threshold=5.0), source="test")
    assert out["below_threshold"] and out["alerted"] and out["email_job_id"] == "job-7"
    assert out["estimated_left_usd"] == 4.0 and out["threshold_usd"] == 5.0
    sql, args = [x for x in conn.executed if "INSERT INTO shared.notifications" in x[0]][0]
    assert args[1:4] == ("platform.provider_credit.low", "llm_connection", "llm_connection:openai-direct")
    payload = json.loads(args[4])
    assert payload["label"] == "OpenAI API" and "$4.00" in payload["message"] and args[5] == ["admin"]
    assert mail.await_args.kwargs["details"]["connection_key"] == "openai-direct"


@pytest.mark.asyncio
async def test_a_recent_bell_throttles(mail):
    conn = _Conn([_e(1, "opening_balance", 4.2, T2)], recent=True)
    out = await pcr.check_connection(conn, _openai(threshold=5.0), source="test")
    assert out["below_threshold"] and not out["alerted"]
    mail.assert_not_awaited()


@pytest.mark.parametrize("entries,threshold", [([], 5.0), ([_e(1, "opening_balance", 9.0, T2)], 5.0),
                                               ([_e(1, "opening_balance", 1.0, T2)], None)])
@pytest.mark.asyncio
async def test_no_alert_without_an_estimate_above_it_or_without_a_threshold(mail, entries, threshold):
    conn = _Conn(entries)
    out = await pcr.check_connection(conn, _openai(threshold=threshold), source="test")
    assert not out["alerted"] and conn.executed == []
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_low_credit_walks_the_ledger_connections(monkeypatch, mail):
    conn = _Conn([_e(1, "opening_balance", 1.0, T2)])

    async def ledger_connections(c):
        return [_openai(threshold=5.0)]

    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False
            return _Ctx()
    monkeypatch.setattr(pcr, "ledger_connections", ledger_connections)
    out = await pcr.check_low_credit(_Pool(), source="test")
    assert [r["connection_key"] for r in out] == ["openai-direct"] and out[0]["alerted"]
    assert await pcr.check_low_credit(_Pool(), source="test", connection_key="anthropic-direct") == []


@pytest.mark.asyncio
async def test_add_entry_refuses_a_connection_without_a_ledger_and_bad_amounts(monkeypatch):
    async def ledger_connections(c):
        return [_openai()]
    monkeypatch.setattr(pcr, "ledger_connections", ledger_connections)
    with pytest.raises(LookupError):
        await pcr.add_entry(_Conn([]), "acc3", kind="topup", amount_usd=5, effective_at=None, note=None, actor="a")
    with pytest.raises(ValueError):
        await pcr.add_entry(_Conn([]), "openai-direct", kind="topup", amount_usd=0, effective_at=None, note=None,
                            actor="a")
    with pytest.raises(ValueError):
        await pcr.add_entry(_Conn([]), "openai-direct", kind="refund", amount_usd=5, effective_at=None, note=None,
                            actor="a")


@pytest.mark.asyncio
async def test_a_missing_ledger_table_says_so(monkeypatch):
    class UndefinedTableError(Exception):
        pass

    class _Missing(_Conn):
        async def fetch(self, sql, *args):
            raise UndefinedTableError("relation shared.provider_credit_topup does not exist")
    with pytest.raises(pcr.LedgerUnavailable):
        await pcr.entries_of(_Missing([]), "openai-direct")


# ── e-mails ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_both_new_templates_have_a_renderer():
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_PROVIDER_CIRCUIT_OPEN] is \
        ops_alerts.render_provider_circuit_open
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_PROVIDER_CREDIT_LOW] is \
        ops_alerts.render_provider_credit_low


def test_circuit_mail_masks_ids_and_links_the_providers_page():
    msg = ops_alerts.render_provider_circuit_open(
        {"connection_key": "typesafe", "label": "Jev (TypeSafe)", "error_class": "other", "period": "2026-11-02",
         "consecutive_failures": 5, "last_error": "503 from arn:aws:iam::123456789012:role/Invented"},
        to="ops@example.com", base_url="https://admin.example.com", environment="dev")
    assert msg.subject == "[AA-CIS dev] Jev (TypeSafe) paused by its circuit (other)"
    assert "123456789012" not in msg.text and "123456789012" not in msg.html
    assert "https://admin.example.com/admin/ai-gateway?tab=providers" in msg.text


def test_credit_mail_says_the_estimate_and_the_threshold():
    msg = ops_alerts.render_provider_credit_low(
        {"connection_key": "openai-direct", "label": "OpenAI API", "estimated_left_usd": 4.0, "threshold_usd": 5.0,
         "recorded_usd": 4.2, "spent_usd": 0.2, "since": T2.isoformat(), "period": "2026-11-02"},
        to="ops@example.com", base_url="https://admin.example.com", environment="dev")
    assert msg.subject == "[AA-CIS dev] OpenAI API credit low: about $4.00 left (alert below $5.00)"
    assert "view=credit" in msg.text and "$4.20" in msg.text


@pytest.mark.asyncio
async def test_the_mail_keys_are_one_per_connection_class_and_day(monkeypatch):
    from shared.jobs import queue
    calls = []

    async def enqueue(conn, kind, payload, **kw):
        calls.append((payload["template"], kw["idempotency_key"], kw["created_by"]))
        return "job", True
    monkeypatch.setattr(queue, "enqueue", enqueue)
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: object())
    now = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
    await ops_alerts.enqueue_provider_circuit_email(None, details={"connection_key": "geoapify",
                                                                   "error_class": "other"}, now=now)
    await ops_alerts.enqueue_provider_credit_email(None, details={"connection_key": "openai-direct"}, now=now)
    assert calls == [
        ("provider_circuit_open", "ops_alert_email:provider_circuit_open:geoapify:other:2026-11-02",
         "provider_circuit"),
        ("provider_credit_low", "ops_alert_email:provider_credit_low:openai-direct:real:2026-11-02",
         "provider_credit")]


@pytest.mark.asyncio
async def test_no_mail_when_ses_is_not_configured(monkeypatch):
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: None)
    assert await ops_alerts.enqueue_provider_circuit_email(None, details={"connection_key": "x"}) is None
    assert await ops_alerts.enqueue_provider_credit_email(None, details={"connection_key": "x"}) is None


# ── the Providers page ──────────────────────────────────────────────────────────────────────────────────────────

def test_with_credit_marks_a_low_row_and_keeps_a_switched_off_one():
    row = {"key": "llm:openai-direct", "status": "unknown", "status_reason": None, "balance": None}
    low = gw.with_credit(row, {"usd": 4.0, "below_threshold": True})
    assert low["status"] == "low" and low["status_reason"] == "below_threshold" and low["balance"]["usd"] == 4.0
    gone = gw.with_credit(row, {"usd": -0.5, "below_threshold": True})
    assert gone["status"] == "cap_reached" and gone["status_reason"] == "credit_used_up"
    off = gw.with_credit({**row, "status": "disabled"}, {"usd": 1.0, "below_threshold": True})
    assert off["status"] == "disabled" and off["balance"]["usd"] == 1.0
    assert gw.with_credit(row, None) is row


def test_with_circuit_opens_the_row_unless_off():
    row = {"key": "jev", "status": "ok", "status_reason": None}
    assert gw.with_circuit(row, None) == {**row, "circuit": None}
    opened = gw.with_circuit(row, {"state": "open"})
    assert (opened["status"], opened["status_reason"]) == ("open", "circuit_open")
    assert gw.with_circuit(row, {"state": "half_open"})["status"] == "open"
    assert gw.with_circuit(row, {"state": "closed", "consecutive_failures": 2})["status"] == "ok"
    off = gw.with_circuit({**row, "status": "disabled", "status_reason": "switched_off_in_registry"},
                          {"state": "open"})
    assert off["status"] == "disabled"


def test_read_providers_lays_the_circuit_and_the_balance_on_the_rows(monkeypatch):
    from tests.unit import test_aa804_ai_gateway_api as base
    from shared import jev_credit

    async def conns(pool, now):
        return {"connections": [dict(c) for c in base.CONNS], "health": base.HEALTH, "monthly": {}}

    async def fixed(value):
        return value
    monkeypatch.setattr(gw, "_read_connections", conns)
    monkeypatch.setattr(jev_credit, "estimate", lambda pool: fixed(
        {"estimated_left_usd": 9.0, "total_topped_up_usd": 10.0, "first_topup_on": "2026-02-01",
         "spent_since_first_topup_usd": 1.0}))
    monkeypatch.setattr(gw, "geo_settings", lambda pool: fixed((True, 2500)))
    monkeypatch.setattr(gw, "dfs_threshold", lambda pool: fixed(10.0))
    monkeypatch.setattr(jev_credit, "low_credit_threshold", lambda pool: fixed(3.0))
    monkeypatch.setattr(gw, "_read_circuits", lambda pool, now: fixed({"dataforseo": {"state": "open"}}))
    monkeypatch.setattr(gw, "_read_credit", lambda pool: fixed(
        {"openai-direct": {"usd": 4.0, "below_threshold": True, "threshold_usd": 5.0}}))
    out = asyncio.run(gw.read_providers(base._provider_pool(), now=base.NOW))
    by = {p["key"]: p for p in out["providers"]}
    assert by["dataforseo"]["status"] == "open" and by["dataforseo"]["circuit"] == {"state": "open"}
    assert by["jev"]["circuit"] is None and by["geoapify"]["circuit"] is None
    assert by["llm:openai-direct"]["balance"]["usd"] == 4.0
    assert by["llm:llm-main"]["balance"] is None and out["errors"] == []


# ── the routes ──────────────────────────────────────────────────────────────────────────────────────────────────

class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(conn))))


def test_body_validation():
    with pytest.raises(ValueError):
        routes.CreditEntryBody(kind="topup", amount_usd=0)
    with pytest.raises(ValueError):
        routes.CreditEntryBody(kind="refund", amount_usd=1)
    with pytest.raises(ValueError):
        routes.CreditEntryBody(kind="topup", amount_usd=1, extra=True)
    b = routes.CreditEntryBody(kind="opening_balance", amount_usd=0, note="  ")
    assert b.amount_usd == 0 and b.note is None
    assert routes.CreditEntryBody(amount_usd=1.239).amount_usd == 1.24


@pytest.mark.asyncio
async def test_post_entry_audits_and_checks_at_once(monkeypatch):
    conn = _Conn([])
    entry = {"id": 11, "connection_key": "openai-direct", "kind": "opening_balance", "amount_usd": 4.2,
             "effective_at": T2.isoformat(), "note": None, "created_by": "admin:u1", "created_at": T2.isoformat()}
    add = AsyncMock(return_value=entry)
    check = AsyncMock(return_value=[{"connection_key": "openai-direct", "alerted": True}])
    audit = AsyncMock()
    monkeypatch.setattr(pcr, "add_entry", add)
    monkeypatch.setattr(pcr, "check_low_credit", check)
    monkeypatch.setattr(routes, "write_audit_log", audit)
    out = await routes.post_credit_entry(routes.CreditEntryBody(kind="opening_balance", amount_usd=4.2),
                                         _request(conn), connection_key="openai-direct", x_admin_user_id="u1")
    assert out == {"entry": entry, "check": {"connection_key": "openai-direct", "alerted": True}}
    assert add.await_args.kwargs["actor"] == "admin:u1"
    assert audit.await_args.kwargs["action"] == "provider_credit.entry_added"
    assert check.await_args.kwargs == {"source": "entry_added", "connection_key": "openai-direct"}


@pytest.mark.parametrize("exc,status", [(LookupError("x"), 404), (ValueError("bad"), 422),
                                        (pcr.LedgerUnavailable("342"), 503)])
@pytest.mark.asyncio
async def test_post_entry_errors(monkeypatch, exc, status):
    monkeypatch.setattr(pcr, "add_entry", AsyncMock(side_effect=exc))
    with pytest.raises(HTTPException) as e:
        await routes.post_credit_entry(routes.CreditEntryBody(amount_usd=1), _request(_Conn([])),
                                       connection_key="openai-direct", x_admin_user_id=None)
    assert e.value.status_code == status


@pytest.mark.asyncio
async def test_delete_entry_404_and_audit(monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr(routes, "write_audit_log", audit)
    monkeypatch.setattr(pcr, "delete_entry", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as e:
        await routes.delete_credit_entry(_request(_Conn([])), entry_id=9, x_admin_user_id="u1")
    assert e.value.status_code == 404 and audit.await_count == 0
    gone = {"id": 9, "connection_key": "openai-direct", "kind": "topup", "amount_usd": 5.0, "effective_at": "x"}
    monkeypatch.setattr(pcr, "delete_entry", AsyncMock(return_value=gone))
    out = await routes.delete_credit_entry(_request(_Conn([])), entry_id=9, x_admin_user_id="u1")
    assert out == {"deleted": True, "entry": gone}
    assert audit.await_args.kwargs["action"] == "provider_credit.entry_deleted"


@pytest.mark.asyncio
async def test_get_credit_503_before_the_migration(monkeypatch):
    monkeypatch.setattr(pcr, "read_all", AsyncMock(side_effect=pcr.LedgerUnavailable("342")))
    with pytest.raises(HTTPException) as e:
        await routes.get_credit(_request(_Conn([])))
    assert e.value.status_code == 503


def test_dataclass_fields_used_exist():
    names = {f.name for f in dataclasses.fields(ProviderConnection)}
    assert {"balance_source", "low_balance_usd", "label", "kind", "enabled", "connection_key"} <= names
