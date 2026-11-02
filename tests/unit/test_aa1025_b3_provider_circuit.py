"""AA-1025 B3 (S263 gateway plan) — the shared circuit of the non-LLM providers (shared/provider_circuit.py) and its
three clients: Jev (decide.py), DataForSEO (dataforseo_client._request) and Geoapify (geocoder.search, the place
details client).

- failure_class: 401 / 403 access_denied, 402 quota_exhausted, 429 throttled, 404 / 408 / 5xx / transport other, any
  other 4xx not counted; DataForSEO body codes by their first three digits.
- the circuit (per process here: the conftest's DB runner refuses, which is the fallback the code takes when the
  table is missing): opens after the class threshold, refuses while open, lets exactly one probe through when
  half-open, closes on its success, reopens for twice as long on its failure; raises the alert once per opening.
- the alert: one bell (throttled, under an advisory lock) and one e-mail job; none for a Jev 402 (own alert).
- the clients: an open circuit sends nothing and writes the client's refused 0 USD row; outcomes are recorded once
  per call after the client's own retries.
Mock-only: no provider call, no DB (the SQL runs on PostgreSQL in tests/integration/test_aa1025_b3b5_db.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.places import geocoder as gc
from services.places import shape_sources
from services.seo_intelligence import dataforseo_client as dfs_mod
from services.seo_intelligence.dataforseo_client import DataForSEOClient, DFSCallError
from shared import provider_circuit as pc
from shared.llm_client import connections
from shared.llm_client import decide as d
from shared.llm_client.breaker import Health

T0 = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(pc, "_now", lambda: now["t"])

    def advance(seconds: float):
        now["t"] = now["t"] + timedelta(seconds=seconds)
    return advance


@pytest.fixture
def alerts(monkeypatch):
    seen = []

    async def on_open(h):
        seen.append(h)
    monkeypatch.setattr(pc, "on_open", on_open)
    return seen


# ── classification ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,cls", [
    (401, "access_denied"), (403, "access_denied"), (402, "quota_exhausted"), (429, "throttled"),
    (404, "other"), (405, "other"), (408, "other"), (410, "other"), (500, "other"), (502, "other"),
    (503, "other"), (None, "other"),
    (400, None), (409, None), (413, None), (422, None),
    (40100, "access_denied"), (40200, "quota_exhausted"), (40501, None), (50000, "other"), ("x", "other")])
def test_failure_class(status, cls):
    assert pc.failure_class(status) == cls


# ── the circuit (in process) ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_outage_opens_after_five_failures_then_one_probe_closes_it(clock, alerts):
    for i in range(4):
        await pc.record_failure("typesafe", "other", f"HTTPStatusError: 503 attempt {i}")
        assert await pc.admit("typesafe") is None                 # still closed
    await pc.record_failure("typesafe", "other", "HTTPStatusError: 503")
    refusal = await pc.admit("typesafe")
    assert refusal and "circuit open (other)" in refusal and "nothing was sent" in refusal
    assert pc.open_class("typesafe") == "other"
    assert [h.connection_key for h in alerts] == ["typesafe"]   # one alert for the opening
    await pc.record_failure("typesafe", "other", "a call that started before it opened")
    assert len(alerts) == 1                                     # still open: no second alert
    clock(61)                                                   # the open period (60 s) ran out: half-open
    assert await pc.admit("typesafe") is None                   # the one probe
    assert await pc.admit("typesafe") is not None               # every other call waits for it
    await pc.record_success("typesafe")
    assert await pc.admit("typesafe") is None and pc.open_class("typesafe") is None
    assert {h.connection_key: h for h in pc.snapshot()}["typesafe"].consecutive_failures == 0


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_failed_probe_reopens_for_twice_as_long(clock, alerts):
    for _ in range(5):
        await pc.record_failure("geoapify", "other", "HTTP 502")
    clock(61)
    assert await pc.admit("geoapify") is None
    await pc.record_failure("geoapify", "other", "HTTP 502 again")
    h = {x.connection_key: x for x in pc.snapshot()}["geoapify"]
    assert h.open_streak == 2 and h.circuit_open_until == T0 + timedelta(seconds=61 + 120)
    assert len(alerts) == 2                                     # a new opening (the bell itself is throttled)


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_refused_key_opens_at_once_and_a_bad_request_is_not_counted(clock, alerts):
    for _ in range(10):
        await pc.record_failure("dataforseo", None, "HTTP 422 bad keyword")
    assert await pc.admit("dataforseo") is None
    await pc.record_failure("dataforseo", "access_denied", "HTTP 401")
    assert "access_denied" in (await pc.admit("dataforseo") or "")
    assert await pc.admit("geoapify") is None                   # one circuit per provider


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_bad_request_frees_a_held_probe_lease(clock, alerts):
    for _ in range(5):
        await pc.record_failure("geoapify", "other", "HTTP 500")
    clock(61)
    assert await pc.admit("geoapify") is None                   # the probe took the lease
    await pc.record_failure("geoapify", None, "HTTP 400")       # the probe's request was the problem
    assert await pc.admit("geoapify") is None                   # the next call may probe at once


@pytest.mark.asyncio
async def test_switched_off_by_env_admits_everything_and_writes_nothing(monkeypatch, clock, alerts):
    monkeypatch.setenv(pc.SWITCH_ENV, "off")
    for _ in range(10):
        await pc.record_failure("typesafe", "access_denied", "HTTP 401")
    assert await pc.admit("typesafe") is None and pc.snapshot() == [] and alerts == []


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_the_db_rows_are_shared_when_the_table_answers(monkeypatch, clock, alerts):
    """Another task opened the circuit: this process reads the row and refuses without a failure of its own."""
    row = {"connection_key": "dataforseo", "last_ok_at": None, "last_error_at": T0, "last_error_class": "other",
           "last_error": "HTTP 503", "consecutive_failures": 5, "circuit_open_until": T0 + timedelta(seconds=60),
           "open_streak": 1, "opened_at": T0, "probe_until": None}

    async def runner(fn):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[row])
        return await fn(conn)
    monkeypatch.setattr(pc, "db_runner", runner)
    assert "circuit open (other)" in (await pc.admit("dataforseo") or "")


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_db_error_never_reaches_the_caller(monkeypatch, clock, alerts):
    async def runner(fn):
        raise OSError("db down")
    monkeypatch.setattr(pc, "db_runner", runner)
    assert await pc.admit("typesafe") is None
    await pc.record_success("typesafe")
    await pc.record_failure("typesafe", "other", "x")


# ── the alert ───────────────────────────────────────────────────────────────────────────────────────────────────

def _health(key="dataforseo", cls="other"):
    return Health(connection_key=key, model_key="*", last_error_at=T0, last_error_class=cls,
                  last_error="HTTPStatusError: 503 from arn:aws:iam::123456789012:role/Invented",
                  consecutive_failures=5, circuit_open_until=T0 + timedelta(seconds=60), open_streak=1, opened_at=T0)


class _AlertConn:
    def __init__(self, recent=False):
        self.recent, self.executed = recent, []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchval(self, sql, *args):
        return 1 if self.recent else None


@pytest.mark.asyncio
async def test_alert_writes_one_bell_and_queues_one_mail(monkeypatch):
    from shared import ops_alerts
    mail = AsyncMock(return_value="job-1")
    monkeypatch.setattr(ops_alerts, "enqueue_provider_circuit_email", mail)
    conn = _AlertConn()
    assert await pc.alert_open(conn, _health()) == (True, "job-1")
    assert "pg_advisory_xact_lock" in conn.executed[0][0]
    sql, args = conn.executed[1]
    assert "INSERT INTO shared.notifications" in sql
    assert args[1:4] == ("provider_circuit_open", "provider_connection", "provider_connection:dataforseo")
    payload = json.loads(args[4])
    assert payload["label"] == "DataForSEO" and payload["error_class"] == "other"
    assert "123456789012" not in json.dumps(payload)            # ids masked
    assert mail.await_args.kwargs["details"]["connection_key"] == "dataforseo"


@pytest.mark.asyncio
async def test_alert_is_throttled_by_an_unread_bell(monkeypatch):
    from shared import ops_alerts
    mail = AsyncMock()
    monkeypatch.setattr(ops_alerts, "enqueue_provider_circuit_email", mail)
    conn = _AlertConn(recent=True)
    assert await pc.alert_open(conn, _health()) == (False, None)
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_second_alert_for_a_jev_402(monkeypatch):
    conn = _AlertConn()
    assert await pc.alert_open(conn, _health("typesafe", "quota_exhausted")) == (False, None)
    assert conn.executed == []
    assert (await pc.alert_open(conn, _health("typesafe", "other")))[0] is True


def test_view_says_the_state():
    assert pc.view(None) is None
    v = pc.view(_health(), now=T0 + timedelta(seconds=10))
    assert v["state"] == "open" and v["open_until"] == (T0 + timedelta(seconds=60)).isoformat()
    assert pc.view(_health(), now=T0 + timedelta(seconds=90))["state"] == "half_open"


# ── Jev ─────────────────────────────────────────────────────────────────────────────────────────────────────────

from tests.unit.test_aa642_l2_provider_switch import _ask, _conn, jev_row  # noqa: E402,F401  (fixture)


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_jev_5xx_opens_the_circuit_and_the_next_decide_sends_nothing(jev_row, clock, alerts):
    jev_row(base_url=None, timeout_s=None, max_retries=None, enabled=True)
    req = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
    boom = httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
    for _ in range(5):
        dec, m_call, m_log, _ = await _ask(_conn(), call=AsyncMock(side_effect=boom))
        m_call.assert_awaited_once()
        assert m_log.await_args.kwargs["status"] == "error"
    assert [h.connection_key for h in alerts] == ["typesafe"]
    dec, m_call, m_log, _ = await _ask(_conn())
    m_call.assert_not_awaited()
    v = dec.verdicts["kw_belongs"]
    assert v.zone == "error" and "circuit open" in v.error
    row = m_log.await_args.kwargs
    assert row["status"] == "refused" and row["cost_usd"] == 0.0 and row["error_class"] == "other"
    assert row["connection_key"] == "typesafe"
    clock(61)                                                   # half-open: one decide probes and closes it
    dec, m_call, _, _ = await _ask(_conn())
    m_call.assert_awaited_once()
    assert dec.verdicts["kw_belongs"].zone == "accept" and pc.open_class("typesafe") is None


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_jev_bad_request_is_not_counted(jev_row, clock, alerts):
    jev_row(enabled=True)
    req = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
    bad = httpx.HTTPStatusError("422", request=req, response=httpx.Response(422, request=req))
    for _ in range(6):
        await _ask(_conn(), call=AsyncMock(side_effect=bad))
    assert await pc.admit("typesafe") is None and alerts == []


# ── DataForSEO ──────────────────────────────────────────────────────────────────────────────────────────────────

from tests.unit.test_aa642_l2_provider_switch import _OK, _Http, dfs  # noqa: E402,F401  (fixture)


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_dataforseo_5xx_opens_the_circuit_then_nothing_is_sent(monkeypatch, dfs, clock, alerts):
    rows, _ = dfs
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(status=503, body={}))
    client = DataForSEOClient(login="x", password="y", stage="a1_seo")
    for _ in range(5):
        with pytest.raises(httpx.HTTPStatusError):
            await client._request("search_volume_bulk", "https://api.dataforseo.com/v3/x", payload=[{}])
    http = _Http(body=_OK)
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", http)
    with pytest.raises(DFSCallError) as exc:
        await client._request("search_volume_bulk", "https://api.dataforseo.com/v3/x", payload=[{}],
                              keyword_count=2)
    assert exc.value.fatal and "circuit open" in str(exc.value) and http.calls == 0
    row = rows.call_args.kwargs
    assert row["status"] == "refused" and row["cost_usd"] == 0.0
    assert row["meta"] == {"connection_key": "dataforseo", "refused": "circuit_open"}
    clock(61)
    data, _ms = await client._request("search_volume_bulk", "https://api.dataforseo.com/v3/x", payload=[{}])
    assert http.calls == 1 and data["cost"] == 0.002 and pc.open_class("dataforseo") is None


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_dataforseo_402_body_opens_at_once_a_bad_field_does_not(monkeypatch, dfs, clock, alerts):
    bad_field = {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient", _Http(body=bad_field))
    client = DataForSEOClient(login="x", password="y")
    for _ in range(6):
        await client._request("x", "https://api.dataforseo.com/v3/x", payload=[{}])
    assert await pc.admit("dataforseo") is None
    monkeypatch.setattr(dfs_mod.httpx, "AsyncClient",
                        _Http(body={"status_code": 40200, "status_message": "Payment Required.", "tasks": []}))
    await client._request("x", "https://api.dataforseo.com/v3/x", payload=[{}])
    assert pc.open_class("dataforseo") == "quota_exhausted"


# ── Geoapify ────────────────────────────────────────────────────────────────────────────────────────────────────

def _geo_resp(status=200):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = {"results": []}
    return r


@pytest.fixture
def no_geo_writes(monkeypatch):
    async def writer(key, s, probed):
        return None
    monkeypatch.setattr(connections, "provider_state_writer", writer)


async def _no_sleep(_s):
    return None


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_geoapify_outage_opens_the_circuit_for_search_and_details(no_geo_writes, clock, alerts):
    http = MagicMock()
    http.get = AsyncMock(return_value=_geo_resp(503))
    g = gc.GeoapifyGeocoder(http=http, api_key="test-key", max_retries=1, sleep=_no_sleep, rate_per_s=1000)
    for _ in range(5):
        with pytest.raises(gc.GeocoderError):
            await g.search("Quiet Valley", "la")
    assert http.get.await_count == 10                          # 2 attempts per search, one outcome per search
    with pytest.raises(gc.GeocoderUnavailable, match="circuit open"):
        await g.search("Quiet Valley", "la")
    details = shape_sources.GeoapifyDetails(http=http, api_key="test-key")
    with pytest.raises(gc.GeocoderUnavailable, match="circuit open"):
        await details.fetch("place-1")
    assert http.get.await_count == 10
    clock(61)
    http.get = AsyncMock(return_value=_geo_resp(200))
    assert await g.search("Quiet Valley", "la") == {"results": []}
    assert pc.open_class("geoapify") is None


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_details_404_is_an_answer(no_geo_writes, clock, alerts):
    http = MagicMock()
    http.get = AsyncMock(return_value=_geo_resp(404))
    details = shape_sources.GeoapifyDetails(http=http, api_key="test-key")
    for _ in range(6):
        assert await details.fetch("place-1") == {}
    assert await pc.admit("geoapify") is None and alerts == []


@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_rejected_geoapify_key_opens_at_once(no_geo_writes, clock, alerts):
    http = MagicMock()
    http.get = AsyncMock(return_value=_geo_resp(401))
    g = gc.GeoapifyGeocoder(http=http, api_key="test-key", sleep=_no_sleep, rate_per_s=1000)
    with pytest.raises(gc.GeocoderUnavailable, match="key rejected"):
        await g.search("Quiet Valley", "la")
    assert pc.open_class("geoapify") == "access_denied"
