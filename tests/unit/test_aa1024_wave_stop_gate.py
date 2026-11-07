"""AA-1024 G17 — the wave start refuses a new wave while the previous wave has a stop flag that is true or pending.

Root lessons log (wave family): a wave went on past an approved stop threshold because the report only printed its
flags, and a flag read while jobs were still queued was once taken for a final answer. `POST /admin/waves` now
recomputes the previous wave's flags (services/waves/quality.py, the wave report's own numbers) and answers 409 unless
`stop_override` quotes the tech lead's written approval. Invented tours and names only; no DB (fake connection).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_waves
from services.waves import quality as wq

T0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
PREV = {"id": "9b9b9b9b-0000-4000-8000-000000000001", "name": "Invented wave 7", "status": "open",
        "created_at": T0, "closed_at": None, "expected_tour_ids": ["t-1", "t-2"]}


def _req(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire = MagicMock(return_value=ctx)
    return req


def _create_conn():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tour_id": "t-1"}])
    conn.fetchrow = AsyncMock(return_value={
        "id": "w2", "name": "Next", "country": None, "expected_tour_ids": ["t-1"], "status": "open", "notes": None,
        "created_by": "admin", "created_at": T0, "closed_at": None, "last_checked_at": None, "last_check": None})
    return conn


def _gate(exit_code, state, active=()):
    return {"wave": {"id": PREV["id"], "name": PREV["name"], "status": "open"}, "applies": True, "tours": 2,
            "blocks": exit_code in (wq.EXIT_STOP, wq.EXIT_PENDING), "stop_state": state,
            "active_jobs": list(active), "exit_code": exit_code}


async def _create(gate, **body):
    conn = _create_conn()
    side = gate if isinstance(gate, Exception) else None
    with patch.object(admin_waves, "verify_admin_secret"), \
            patch.object(wq, "previous_wave_gate", AsyncMock(return_value=None if side else gate, side_effect=side)):
        out = await admin_waves.create_wave(admin_waves.WaveCreate(name="Next", tour_ids=["t-1"], **body),
                                            _req(conn), "s", None)
    return out, conn


@pytest.mark.asyncio
async def test_a_true_flag_refuses_the_next_wave():
    gate = _gate(wq.EXIT_STOP, {"stayed_v1_over_10pct": "true", "not_clearly_better": "false"})
    with pytest.raises(HTTPException) as e:
        await _create(gate)
    assert e.value.status_code == 409
    assert "is true" in e.value.detail["error"] and e.value.detail["previous_wave"]["name"] == "Invented wave 7"
    assert "stop_override" in e.value.detail["next"]


@pytest.mark.asyncio
async def test_pending_flags_refuse_too():
    gate = _gate(wq.EXIT_PENDING, {"stayed_v1_over_10pct": "pending"}, [{"kind": "s1_rewrite", "status": "queued",
                                                                         "n": 3}])
    with pytest.raises(HTTPException) as e:
        await _create(gate)
    assert e.value.status_code == 409 and "pending" in e.value.detail["error"]
    assert e.value.detail["active_jobs"][0]["n"] == 3


@pytest.mark.asyncio
async def test_a_gate_error_refuses_unless_overridden():
    with pytest.raises(HTTPException) as e:
        await _create(RuntimeError("relation does not exist"))
    assert e.value.status_code == 409 and "could not be computed" in e.value.detail["error"]
    assert "relation" not in str(e.value.detail)        # DB text stays in the server log (AA-912)
    out, conn = await _create(RuntimeError("boom"), stop_override="Tech lead approved in the invented thread on 01/11")
    assert out["id"] == "w2"
    assert "not computed" in conn.fetchrow.call_args[0][4]


@pytest.mark.asyncio
async def test_override_quotes_the_approval_in_the_notes():
    gate = _gate(wq.EXIT_STOP, {"stayed_v1_over_10pct": "true", "published_not_atomized": "true",
                                "not_clearly_better": "false"})
    out, conn = await _create(gate, notes="second batch",
                              stop_override="Approved by the tech lead: go on with batch two (invented quote)")
    notes = conn.fetchrow.call_args[0][4]
    assert notes.startswith("[stop-flag override: previous wave 'Invented wave 7'")
    assert "published_not_atomized, stayed_v1_over_10pct]" in notes and "invented quote" in notes
    assert notes.endswith("second batch")


@pytest.mark.asyncio
async def test_no_flag_or_no_previous_wave_passes():
    for gate in (None, _gate(0, {"stayed_v1_over_10pct": "false"})):
        out, conn = await _create(gate, notes="n")
        assert out["id"] == "w2" and conn.fetchrow.call_args[0][4] == "n"


def test_a_short_override_is_rejected():
    with pytest.raises(Exception):
        admin_waves.WaveCreate(name="x", tour_ids=["a"], stop_override="ok go")


# ── services/waves/quality.previous_wave_gate on a fake connection ────────────────────────────────────────────────


def _gate_conn(prev, versions=(), active=()):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=prev)

    async def fetch(sql, *args):
        if sql is wq.VERSIONS_SQL:
            return list(versions)
        if sql is wq.ACTIVE_JOBS_SQL:
            return list(active)
        return []
    conn.fetch = AsyncMock(side_effect=fetch)
    return conn


@pytest.mark.asyncio
async def test_gate_without_a_wave_is_none():
    assert await wq.previous_wave_gate(_gate_conn(None)) is None


@pytest.mark.asyncio
async def test_gate_reads_the_waves_window_and_expected_set():
    conn = _gate_conn(PREV)
    now = T0 + dt.timedelta(days=2)
    out = await wq.previous_wave_gate(conn, now=now)
    sql, since, until, ids, country = conn.fetch.call_args_list[0][0]
    assert sql is wq.VERSIONS_SQL and since == T0 and until == now and ids == ["t-1", "t-2"] and country is None
    # nothing written in the window and no job active: no quality read, no block
    assert out["applies"] is False and out["blocks"] is False


@pytest.mark.asyncio
async def test_gate_is_pending_while_jobs_run_before_any_version():
    conn = _gate_conn(PREV, active=[{"kind": "s1_rewrite", "status": "running", "n": 1}])
    out = await wq.previous_wave_gate(conn, now=T0)
    assert out["blocks"] is True and out["exit_code"] == wq.EXIT_PENDING
    assert set(out["stop_state"].values()) == {"pending"}


@pytest.mark.asyncio
async def test_gate_flags_come_from_the_wave_report_numbers():
    tours = [{"tour_id": "t-1", "country": "Invented", "outcome": "stayed_v1", "before": None, "after": None,
              "atoms": None}]
    conn = _gate_conn(PREV, versions=[{"tour_id": "t-1"}])
    with patch.object(wq, "_compute_tours", return_value=tours):
        out = await wq.previous_wave_gate(conn, now=T0)
    assert out["applies"] is True and out["tours"] == 1
    assert out["stop_state"]["stayed_v1_over_10pct"] == "true" and out["exit_code"] == wq.EXIT_STOP
    assert out["blocks"] is True
