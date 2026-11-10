"""S275 (AI gateway hardening, lane jev-decide-burst) — Jev (TypeSafe) under a recompute burst.

On 09/11 one platform recompute sent > 240 Jev asks a minute; 20 failed with ConnectTimeout, the shared circuit
opened and refused ~5,000 asks, each counted as a provider error by the readers. Here:

- F2: ``_call_jev`` reuses ONE keep-alive httpx client per event loop (one connection setup, not one per ask); the
  client is closed when its loop's ``asyncio.run`` shuts down, a closed loop's entry is dropped, the connection row's
  timeout is passed per request, a retry stays on the same client.
- F4: a failed call whose text says nothing is classified by its HTTP status (401 / 403 access_denied, 402
  quota_exhausted, 429 throttled; a content 4xx, a 5xx and a timeout stay ``other``), so a refused key moves Jev's
  access_state and its row is ``refused``.
- F5: a circuit-open refusal row carries ``quality_signal.refusal = circuit_open`` with the class that opened the
  circuit, and the AI gateway's Jev health counts gateway refusals apart from provider errors.

Mock-only: httpx.MockTransport, no provider call, no database.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from api.services import ai_gateway as gw
from shared import provider_circuit as pc
from shared.llm_client import decide as d
from tests.unit.test_aa642_l2_provider_switch import _ask, _conn, jev_row  # noqa: F401  (fixture)

URL = "https://api.typesafe.ai/v1/systemone"
_REPLY = {"model": "jev-probe", "usage": {"input_tokens": 10}, "answers": {}}


@pytest.fixture
def transport(monkeypatch):
    """Every client ``_call_jev`` makes runs on a MockTransport; ``state`` counts clients and requests."""
    state = {"clients": [], "requests": [], "replies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if state["replies"]:
            return state["replies"].pop(0)
        return httpx.Response(200, json=_REPLY)

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=d._TIMEOUT_S)
        state["clients"].append(client)
        return client
    monkeypatch.setattr(d, "_new_jev_client", factory)
    monkeypatch.setattr(d, "get_typesafe_api_key", lambda: "probe-key")
    monkeypatch.setattr(d, "_provider", {})
    d._jev_clients.clear()
    yield state
    d._jev_clients.clear()


# ── F2: one keep-alive client per event loop ────────────────────────────────────────────────────────────────────────

def test_one_loop_uses_one_client_for_every_call_and_closes_it_on_shutdown(transport):
    async def burst():
        await asyncio.gather(*(d._call_jev("a quiet valley", {"q": {}}) for _ in range(6)))
        await d._call_jev("a quiet valley", {"q": {}})
    asyncio.run(burst())
    assert len(transport["clients"]) == 1 and len(transport["requests"]) == 7
    assert transport["clients"][0].is_closed, "asyncio.run's shutdown closes the loop's client"
    assert d._jev_clients == {}


def test_each_loop_gets_its_own_client(transport):
    for _ in range(2):                    # a job body or decide_sync in its own asyncio.run
        asyncio.run(d._call_jev("a quiet valley", {"q": {}}))
    assert len(transport["clients"]) == 2 and all(c.is_closed for c in transport["clients"])


def test_the_connection_row_timeout_is_sent_per_request(transport, monkeypatch):
    monkeypatch.setattr(d, "_provider", {"timeout_s": 7, "max_retries": 0})
    asyncio.run(d._call_jev("a quiet valley", {"q": {}}))
    assert transport["requests"][0].extensions["timeout"] == {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}
    assert str(transport["requests"][0].url) == URL


def test_a_retry_stays_on_the_same_client(transport):
    transport["replies"] = [httpx.Response(503)]
    out = asyncio.run(d._call_jev("a quiet valley", {"q": {}}))
    assert out == _REPLY and len(transport["requests"]) == 2 and len(transport["clients"]) == 1


def test_close_jev_client_closes_now_and_the_next_call_makes_a_new_one(transport):
    async def run():
        await d._call_jev("a quiet valley", {"q": {}})
        await d.close_jev_client()
        assert transport["clients"][0].is_closed and d._jev_clients == {}
        await d._call_jev("a quiet valley", {"q": {}})
    asyncio.run(run())
    assert len(transport["clients"]) == 2


def test_a_loop_closed_without_shutdown_is_dropped(transport):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(d._call_jev("a quiet valley", {"q": {}}))
    loop.close()                          # no shutdown_asyncgens: the entry stays until the next new client
    assert len(d._jev_clients) == 1
    asyncio.run(d._call_jev("a quiet valley", {"q": {}}))
    assert len(transport["clients"]) == 2 and d._jev_clients == {}


# ── F4: the HTTP status decides when the text says nothing ─────────────────────────────────────────────────────────

def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", URL)
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"Client error '{code} {resp.reason_phrase}' for url '{URL}'", request=req,
                                 response=resp)


@pytest.mark.parametrize("exc,cls", [
    (_status_error(401), "access_denied"), (_status_error(403), "access_denied"),
    (_status_error(402), "quota_exhausted"), (_status_error(429), "throttled"),
    (_status_error(422), "other"), (_status_error(400), "other"), (_status_error(404), "other"),
    (_status_error(503), "other"), (httpx.ConnectTimeout("timed out"), "other"), (RuntimeError("boom"), "other"),
])
def test_jev_failure_class(exc, cls):
    assert d.jev_failure_class(exc) == cls


@pytest.mark.asyncio
async def test_a_401_is_a_refused_access_denied_row_and_moves_the_access_state(jev_row):
    jev_row(enabled=True)
    dec, m_call, m_log, writes = await _ask(_conn(), call=AsyncMock(side_effect=_status_error(401)))
    m_call.assert_awaited_once()
    assert dec.verdicts["kw_belongs"].zone == "error"                     # fail-open as before
    row = m_log.await_args.kwargs
    assert (row["status"], row["error_class"]) == ("refused", "access_denied")
    assert writes == ["access_denied"]


@pytest.mark.asyncio
async def test_a_timeout_stays_an_other_error(jev_row):
    jev_row(enabled=True)
    _, _, m_log, writes = await _ask(_conn(), call=AsyncMock(side_effect=httpx.ConnectTimeout("")))
    row = m_log.await_args.kwargs
    assert (row["status"], row["error_class"]) == ("error", "other")
    assert "refusal" not in row["quality_signal"] and writes == []


# ── F5: a circuit-open refusal is marked, and counted as a refusal ─────────────────────────────────────────────────

@pytest.mark.real_provider_circuit
@pytest.mark.asyncio
async def test_a_circuit_open_refusal_row_is_marked_with_the_opening_class(jev_row, monkeypatch):
    jev_row(base_url=None, timeout_s=None, max_retries=None, enabled=True)
    monkeypatch.setattr(pc, "on_open", AsyncMock())
    for _ in range(5):                                                  # five timeouts open the circuit (other)
        await _ask(_conn(), call=AsyncMock(side_effect=httpx.ConnectTimeout("")))
    dec, m_call, m_log, _ = await _ask(_conn())
    m_call.assert_not_awaited()
    row = m_log.await_args.kwargs
    assert (row["status"], row["error_class"]) == ("refused", "other")
    assert row["quality_signal"]["refusal"] == pc.REFUSAL_CIRCUIT_OPEN == "circuit_open"
    assert row["quality_signal"]["source"] == "decide" and row["cost_usd"] == 0.0
    v = dec.verdicts["kw_belongs"]
    assert gw.jev_verdict_status({"zone": v.zone, "error": v.error}) == "refused"


def test_jev_health_counts_refusals_apart_from_errors():
    assert gw.JEV_STATUS_SQL in gw.DECISIONS_SQL and "AS refused" in gw.DECISIONS_SQL
    row = gw.jev_row({"estimated_left_usd": 20.0}, 3.0, {}, {"calls": 100, "errors": 2, "refused": 90, "cached": 0},
                     None, {})
    assert row["health"]["errors"] == 2 and row["health"]["refused"] == 90 and row["health"]["error_pct"] == 2.0
    assert row["status"] == "ok", "refusals alone never mark Jev degraded (an open circuit shows as its own status)"
