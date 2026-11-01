"""AA-642 (S263 lane L2, B1) — the `enabled` switch of the non-LLM paid providers, and their access state.

- connections.provider_enabled: no row = on, a row with enabled false = off, cached per process, an unreadable read
  keeps the last row; note_provider_access writes once per change (a probe always, with last_probe_at).
- Jev (decide.py): switched off = no TypeSafe call, every asked verdict errors (fail-open), one 0 USD refused
  llm_call_log row with connection_key / account 'typesafe'; an answered ask is logged with the connection too; the
  access state follows the outcome (ok, quota_exhausted on a 402). The daily canary does not call while off.
- DataForSEO: switched off = no HTTP request, a refused 0 USD dfs_call_log row naming the connection, a fatal
  DFSCallError; the access state follows the outcome.
- Geoapify: switched off (row read at job start) = GeocoderUnavailable before any request, in the geocoder and the
  place-details client.
- The admin PATCH allowlist takes `enabled` for typesafe / dataforseo / geoapify (no secret read), not for
  bedrock_guardrails.
Mock-only: no provider call, no DB.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services import llm_connections
from services.places import geocoder as gc
from services.places import shape_sources
from services.seo_intelligence import dataforseo_client as dfs_mod
from services.seo_intelligence.dataforseo_client import DataForSEOClient, DFSCallError
from shared.llm_client import call_log, connections
from shared.llm_client import decide as d
from shared.llm_client.connections import BUILTIN_PROVIDERS
from tests.unit.test_aa1002_s2_admin_switch import FakeDB
from tests.unit.test_aa1002_s2_admin_switch import _row as _conn_row


def _row(key: str, enabled: bool):
    import dataclasses
    return dataclasses.replace(BUILTIN_PROVIDERS[key], enabled=enabled)


# ── connections: the switch read and the access-state writer ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_row_means_on(monkeypatch):
    assert await connections.provider_enabled("dataforseo") is True


@pytest.mark.asyncio
async def test_a_row_switched_off_is_off_and_cached(monkeypatch):
    loader = AsyncMock(return_value=_row("dataforseo", False))
    monkeypatch.setattr(connections, "provider_row_loader", loader)
    assert await connections.provider_enabled("dataforseo") is False
    assert await connections.provider_enabled("dataforseo") is False
    loader.assert_awaited_once()                         # cached for the TTL
    connections.invalidate_providers()
    loader.return_value = _row("dataforseo", True)
    assert await connections.provider_enabled("dataforseo") is True


@pytest.mark.asyncio
async def test_an_unreadable_row_keeps_the_last_one(monkeypatch):
    loader = AsyncMock(return_value=_row("geoapify", False))
    monkeypatch.setattr(connections, "provider_row_loader", loader)
    assert await connections.provider_enabled("geoapify") is False
    connections._provider_rows["geoapify"] = (time.monotonic() - 999, connections._provider_rows["geoapify"][1])
    loader.side_effect = RuntimeError("db down")
    assert await connections.provider_enabled("geoapify") is False
    loader.side_effect, loader.return_value = None, None     # the row is gone (or the read answered nothing)
    connections._provider_rows["geoapify"] = (time.monotonic() - 999, connections._provider_rows["geoapify"][1])
    assert await connections.provider_enabled("geoapify") is False


@pytest.mark.asyncio
async def test_the_switch_reads_through_a_given_db(monkeypatch):
    seen = []

    async def provider_connection(db, key):
        seen.append(db)
        return _row("typesafe", False)
    monkeypatch.setattr(connections, "provider_connection", provider_connection)
    pool = object()
    assert await connections.provider_enabled("typesafe", db=pool) is False
    assert seen == [pool]


@pytest.mark.asyncio
async def test_access_state_is_written_once_per_change_and_always_for_a_probe(monkeypatch):
    writes = []

    async def writer(key, state, probed):
        writes.append((key, state, probed))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    for _ in range(3):
        await connections.note_provider_access("dataforseo", "ok")
    await connections.note_provider_access("dataforseo", "quota_exhausted")
    await connections.note_provider_access("dataforseo", None)            # nothing lasting: nothing written
    await connections.note_provider_access("dataforseo", "nonsense")
    await connections.note_provider_access("dataforseo", "ok", probed=True)
    await connections.note_provider_access("dataforseo", "ok", probed=True)
    await connections.note_provider_access("typesafe", None, probed=True)  # a probe that proves nothing: time only
    assert writes == [("dataforseo", "ok", False), ("dataforseo", "quota_exhausted", False),
                      ("dataforseo", "ok", True), ("dataforseo", "ok", True), ("typesafe", None, True)]


@pytest.mark.asyncio
async def test_a_failed_state_write_backs_off_and_never_raises(monkeypatch):
    writer = AsyncMock(side_effect=RuntimeError("column missing"))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    await connections.note_provider_access("geoapify", "ok")
    await connections.note_provider_access("geoapify", "ok")
    assert writer.await_count == 1                        # backed off, not retried on every call


def test_the_state_sql_touches_only_columns_cis_app_may_update():
    for sql in (connections._SET_PROVIDER_STATE_SQL, connections._STAMP_PROVIDER_PROBE_SQL,
                connections._STAMP_PROBE_ONLY_SQL, d._SET_ACCESS_SQL):
        sets = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        assert {s.split("=")[0].strip() for s in sets.split(",")} <= {"access_state", "last_probe_at"}


# ── Jev (decide.py) ───────────────────────────────────────────────────────────────────────────────────────────────

def _q(key="kw_belongs"):
    return d.Question(key=key, stage="a3_research", kind="noul", instructions="Is it about this valley?",
                      criteria=None, mode="enforce", accept_floor=0.8, reject_ceiling=0.2, threshold_version=1)


def _conn():
    conn = MagicMock()

    async def fetch(sql, *args):
        if "decision_question" in sql:
            q = _q()
            return [{"question_key": q.key, "stage": q.stage, "kind": q.kind, "instructions": q.instructions,
                     "criteria": q.criteria, "mode": q.mode, "accept_floor": q.accept_floor,
                     "reject_ceiling": q.reject_ceiling, "threshold_version": q.threshold_version,
                     "question_hash": "h-" + q.key}]
        return []
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    return conn


@pytest.fixture
def jev_row(monkeypatch):
    """Set Jev's cached connection row (as _load_provider would) and keep it fresh for the test."""
    def _set(**fields):
        monkeypatch.setattr(d, "_provider", dict(fields))
        monkeypatch.setattr(d, "_provider_loaded_at", time.monotonic() + 3600)
    d._questions_loaded_at = 0.0
    d._allowlist_loaded_at = 0.0
    return _set


_ANSWER = {"model": "jev-1.13.0", "usage": {"input_tokens": 1000},
           "answers": {"kw_belongs": {"type": "noul", "noul": 0.9}}}


async def _ask(conn, *, call=None):
    writes = []

    async def writer(db, state):
        writes.append(state)
    with patch.object(d, "_call_jev", new=call or AsyncMock(return_value=_ANSWER)) as m_call, \
         patch.object(d, "jev_budget_blocks", new=AsyncMock(return_value=False)), \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(d, "jev_access_writer", new=writer), \
         patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        dec = await d._decide(d._SingleConn(conn), "a3_research", "kw:US:quiet valley", "quiet valley",
                              ["kw_belongs"], None)
    return dec, m_call, m_log, writes


@pytest.mark.asyncio
async def test_jev_switched_off_sends_nothing_and_logs_one_refused_row_with_the_account(jev_row):
    jev_row(base_url=None, timeout_s=None, max_retries=None, enabled=False)
    dec, m_call, m_log, writes = await _ask(_conn())
    m_call.assert_not_awaited()
    v = dec.verdicts["kw_belongs"]
    assert (v.zone, v.error) == ("error", d.JEV_SWITCHED_OFF)
    assert not dec.accepted("kw_belongs") and not dec.rejected("kw_belongs")      # fail-open: the stage's own rule
    m_log.assert_awaited_once()
    row = m_log.await_args.kwargs
    assert row["status"] == "refused" and row["cost_usd"] == 0.0 and row["provider"] == "typesafe"
    assert row["connection_key"] == "typesafe" and row["account"] == "typesafe"
    assert writes == []                                    # the switch is not an access state


@pytest.mark.asyncio
async def test_jev_switched_on_asks_and_logs_the_answered_row_with_the_connection(jev_row):
    jev_row(base_url=None, timeout_s=None, max_retries=None, enabled=True)
    dec, m_call, m_log, writes = await _ask(_conn())
    m_call.assert_awaited_once()
    assert dec.verdicts["kw_belongs"].zone == "accept"
    row = m_log.await_args.kwargs
    assert row["connection_key"] == "typesafe" and row["cost_usd"] > 0
    assert writes == ["ok"]


@pytest.mark.asyncio
async def test_a_row_without_the_switch_column_is_on(jev_row):
    jev_row(base_url=None, timeout_s=None, max_retries=None)
    _, m_call, _, _ = await _ask(_conn())
    m_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_402_writes_quota_exhausted(jev_row, monkeypatch):
    jev_row(enabled=True)
    monkeypatch.setattr(d, "maybe_alert_jev_credit", AsyncMock(return_value=False))
    monkeypatch.setattr(d, "_trip_jev_breaker", lambda: None)
    _, _, m_log, writes = await _ask(_conn(), call=AsyncMock(side_effect=d.JevBillingError("402")))
    assert writes == ["quota_exhausted"]
    assert m_log.await_args.kwargs["account"] == "typesafe"


@pytest.mark.asyncio
async def test_jev_access_is_written_once_per_change():
    writer = AsyncMock()
    with patch.object(d, "jev_access_writer", new=writer):
        for state in ("ok", "ok", None, "ok", "quota_exhausted"):
            await d.note_jev_access(object(), state)
    assert [c.args[1] for c in writer.await_args_list] == ["ok", "quota_exhausted"]


@pytest.mark.asyncio
async def test_load_provider_reads_the_switch():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"base_url": None, "timeout_s": 3, "max_retries": 2, "enabled": False})
    d._provider_loaded_at = 0.0
    try:
        await d._load_provider(conn)
        assert d.jev_enabled() is False
        assert "enabled" in conn.fetchrow.await_args.args[0]
    finally:
        d._provider, d._provider_loaded_at = {}, 0.0


@pytest.mark.asyncio
async def test_the_canary_does_not_call_jev_while_switched_off(monkeypatch):
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    request = MagicMock()
    with patch.object(d, "jev_connection_enabled", new=AsyncMock(return_value=False)), \
         patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
         patch.object(call_log, "record_call_with_pool", new=AsyncMock()) as m_log:
        out = await admin_llm_ops.check_jev_canary(request, x_admin_secret="x")
    m_call.assert_not_awaited()
    m_log.assert_not_awaited()
    assert out["credit_ok"] is None and out["skipped"] == d.JEV_SWITCHED_OFF


@pytest.mark.asyncio
async def test_a_successful_canary_stamps_the_probe_and_logs_the_account(monkeypatch):
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "verify_admin_secret", lambda _s: None)
    writes = []

    async def writer(key, state, probed):
        writes.append((key, state, probed))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    request = MagicMock()
    reply = {"model": "jev-1.13.0", "usage": {"input_tokens": 120}, "answers": {}}
    with patch.object(d, "jev_connection_enabled", new=AsyncMock(return_value=True)), \
         patch.object(d, "_call_jev", AsyncMock(return_value=reply)), patch.object(d, "reset_jev_breaker"), \
         patch.object(d, "jev_breaker_open", return_value=False), \
         patch.object(d, "_price_in_per_mtok", return_value=0.042), \
         patch.object(call_log, "record_call_with_pool", new=AsyncMock()) as m_log:
        out = await admin_llm_ops.check_jev_canary(request, x_admin_secret="x")
    assert out["credit_ok"] is True
    assert m_log.await_args.kwargs["connection_key"] == "typesafe"
    assert writes == [("typesafe", "ok", True)]


# ── DataForSEO ────────────────────────────────────────────────────────────────────────────────────────────────────

class _Http:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body, 0

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, auth=None, json=None):
        self.calls += 1
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))

    async def get(self, url, auth=None):
        return await self.post(url)


@pytest.fixture
def dfs(monkeypatch):
    rows = MagicMock()
    monkeypatch.setattr(dfs_mod, "record_dfs_call_sync", rows)
    writes = []

    async def writer(key, state, probed):
        writes.append((key, state))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    return rows, writes


_OK = {"status_code": 20000, "cost": 0.002, "tasks": [{"status_code": 20000, "result": []}]}


@pytest.mark.asyncio
async def test_dataforseo_switched_off_sends_nothing_and_logs_a_refused_row(monkeypatch, dfs):
    rows, writes = dfs
    monkeypatch.setattr(connections, "provider_row_loader", AsyncMock(return_value=_row("dataforseo", False)))
    http = _Http(body=_OK)
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", http)
    with pytest.raises(DFSCallError) as exc:
        await DataForSEOClient(login="x", password="y", stage="a1_seo")._request(
            "search_volume_bulk", "https://api.dataforseo.com/v3/x", payload=[{}], keyword_count=3)
    assert exc.value.fatal and "switched off" in str(exc.value)
    assert http.calls == 0
    row = rows.call_args.kwargs
    assert row["status"] == "refused" and row["cost_usd"] == 0.0 and row["fetched_live"] is False
    assert row["meta"] == {"connection_key": "dataforseo", "refused": "switched_off"} and row["stage"] == "a1_seo"
    assert writes == []


@pytest.mark.asyncio
async def test_dataforseo_switched_on_calls_and_writes_ok(monkeypatch, dfs):
    _, writes = dfs
    http = _Http(body=_OK)
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", http)
    data, _ms = await DataForSEOClient(login="x", password="y")._request(
        "search_volume_bulk", "https://api.dataforseo.com/v3/x", payload=[{}])
    assert http.calls == 1 and data["cost"] == 0.002
    assert writes == [("dataforseo", "ok")]


@pytest.mark.parametrize("status,state", [(402, "quota_exhausted"), (401, "access_denied"), (403, "access_denied"),
                                          (500, None)])
@pytest.mark.asyncio
async def test_dataforseo_http_failures_set_the_access_state(monkeypatch, dfs, status, state):
    _, writes = dfs
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(status=status, body={}))
    with pytest.raises(httpx.HTTPStatusError):
        await DataForSEOClient(login="x", password="y")._request("x", "https://api.dataforseo.com/v3/x",
                                                                 payload=[{}])
    assert writes == ([("dataforseo", state)] if state else [])


@pytest.mark.asyncio
async def test_a_dataforseo_402_body_sets_quota_exhausted(monkeypatch, dfs):
    _, writes = dfs
    body = {"status_code": 40200, "status_message": "Payment Required.", "tasks": []}
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(body=body))
    await DataForSEOClient(login="x", password="y")._request("x", "https://api.dataforseo.com/v3/x", payload=[{}])
    assert writes == [("dataforseo", "quota_exhausted")]


# ── Geoapify ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _geo_resp(status=200):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = {"results": []}
    return r


@pytest.mark.asyncio
async def test_refresh_reads_the_geoapify_switch(monkeypatch):
    monkeypatch.setattr(gc, "_registry_daily_limit", None)          # restored after the test (refresh sets it)

    async def provider_connection(db, key):
        return _row("geoapify", False)
    monkeypatch.setattr(connections, "provider_connection", provider_connection)
    await gc.refresh_daily_credit_limit(object())
    assert gc.connection_switched_off() is True


@pytest.mark.asyncio
async def test_a_row_without_a_switch_value_is_on(monkeypatch):
    monkeypatch.setattr(gc, "_registry_daily_limit", None)          # restored after the test (refresh sets it)
    import dataclasses

    async def provider_connection(db, key):
        return dataclasses.replace(BUILTIN_PROVIDERS["geoapify"], enabled=None)
    monkeypatch.setattr(connections, "provider_connection", provider_connection)
    await gc.refresh_daily_credit_limit(object())
    assert gc.connection_switched_off() is False


@pytest.mark.asyncio
async def test_geoapify_switched_off_sends_nothing(monkeypatch):
    monkeypatch.setattr(gc, "_registry_enabled", False)
    http = MagicMock()
    http.get = AsyncMock(return_value=_geo_resp())
    g = gc.GeoapifyGeocoder(http=http, api_key="test-key")
    with pytest.raises(gc.GeocoderUnavailable, match="switched off"):
        g.ensure_ready()
    with pytest.raises(gc.GeocoderUnavailable, match="switched off"):
        await g.search("Quiet Valley", "la")
    details = shape_sources.GeoapifyDetails(http=http, api_key="test-key")
    with pytest.raises(gc.GeocoderUnavailable, match="switched off"):
        await details.fetch("place-1")
    http.get.assert_not_awaited()
    assert g.requests == 0


@pytest.mark.parametrize("status,state", [(200, "ok"), (401, "access_denied")])
@pytest.mark.asyncio
async def test_geoapify_writes_the_access_state(monkeypatch, status, state):
    writes = []

    async def writer(key, s, probed):
        writes.append((key, s))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    http = MagicMock()
    http.get = AsyncMock(return_value=_geo_resp(status))
    g = gc.GeoapifyGeocoder(http=http, api_key="test-key")
    g._throttle.interval = 0.0
    try:
        await g.search("Quiet Valley", "la")
    except gc.GeocoderUnavailable:
        pass
    assert writes == [("geoapify", state)]


@pytest.mark.asyncio
async def test_a_missing_geoapify_key_writes_unconfigured(monkeypatch):
    writes = []

    async def writer(key, s, probed):
        writes.append((key, s))
    monkeypatch.setattr(connections, "provider_state_writer", writer)
    monkeypatch.setattr("shared.secrets.get_geoapify_key", lambda: "")
    g = gc.GeoapifyGeocoder(http=MagicMock())
    with pytest.raises(gc.GeocoderUnavailable, match="empty"):
        g.ensure_ready()

    def boom():
        raise RuntimeError("no secret")
    monkeypatch.setattr("shared.secrets.get_geoapify_key", boom)
    with pytest.raises(gc.GeocoderUnavailable, match="unavailable"):
        g.ensure_ready()
    await asyncio.sleep(0)                                  # the scheduled write runs
    assert writes == [("geoapify", "unconfigured")]


# ── the admin PATCH allowlist ─────────────────────────────────────────────────────────────────────────────────────

def _update(db, key, changes):
    return asyncio.run(llm_connections.update_connection(db, key, changes, actor="admin:u-1"))


@pytest.mark.parametrize("kind,key,auth,secret,url", [
    ("typesafe", "typesafe", "api_key", "aa-cis/dev/typesafe", "https://api.typesafe.ai"),
    ("dataforseo", "dataforseo", "basic_auth", "aa-cis/dev/dataforseo", "https://api.dataforseo.com/v3"),
    ("geoapify", "geoapify", "api_key", "aa-cis/dev/geoapify", "https://api.geoapify.com/v1"),
])
def test_the_patch_switches_a_provider_without_reading_a_secret(monkeypatch, kind, key, auth, secret, url):
    fetch = MagicMock(side_effect=AssertionError("no secret is read for a provider switch"))
    monkeypatch.setattr(connections, "_fetch_secret", fetch)
    for before, after in ((True, False), (False, True)):
        row = _conn_row(connection_key=key, kind=kind, auth_mode=auth, label=key, secret_name=secret,
                        base_url=url, enabled=before)
        db = FakeDB(row)
        out = _update(db, key, {"enabled": after})
        assert out["changed_fields"] == ["enabled"] and out["enabled"] is after
        (upd, args), (_audit_sql, _audit_args) = db.executed
        assert "enabled = $2" in upd and args[1] is after
    fetch.assert_not_called()


def test_the_patch_drops_the_cached_provider_rows(monkeypatch):
    connections._provider_rows["typesafe"] = (time.monotonic(), _row("typesafe", True))
    row = _conn_row(connection_key="typesafe", kind="typesafe", auth_mode="api_key", label="Jev",
                    secret_name="aa-cis/dev/typesafe", base_url="https://api.typesafe.ai", enabled=True)
    _update(FakeDB(row), "typesafe", {"enabled": False})
    assert "typesafe" not in connections._provider_rows


def test_the_guardrails_row_is_still_not_switchable():
    row = _conn_row(connection_key="bedrock-guardrails", kind="bedrock_guardrails", auth_mode="task_role",
                    label="Guardrails", secret_name=None, base_url=None, region="us-west-1", enabled=False)
    with pytest.raises(llm_connections.ConnectionToggleRefused, match="does not read the switch"):
        _update(FakeDB(row), "bedrock-guardrails", {"enabled": True})
