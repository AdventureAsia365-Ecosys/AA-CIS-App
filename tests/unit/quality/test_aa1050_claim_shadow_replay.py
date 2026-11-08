"""AA-1050 W4 — the claim shadow on the replay corpus: DRY RUN plans without a call, a live run is cache first and
stops at the spend guard, the CLI refuses a live run without --confirm-spend. No DB, no model. Invented text only."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.quality import claim_shadow as cs
from services.quality import claim_shadow_replay as rp

PIECES = ["00000000-0000-4000-8000-0000000a1060", "00000000-0000-4000-8000-0000000a1061"]
TENANT = "00000000-0000-4000-8000-0000000a1062"
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included."
TEXTS = ["Arrive in Velmora and dine at the Harbour Lodge on the first night.",
         "Arrive in Velmora where a sunrise balloon ride waits over the bay."]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (cs.SWITCH_ENV, cs.INSTRUMENT_ENV, cs.CAP_ENV, cs.WINDOW_ENV):
        monkeypatch.delenv(k, raising=False)


def _objs():
    return [cs.ShadowObject(surface="t9_blog", fields=(("content_text", t),), source_text=SOURCE, baseline="pass",
                            tenant_id=TENANT, piece_id=p) for p, t in zip(PIECES, TEXTS)]


class _Conn:
    def __init__(self, cached_keys=(), spend=0.0):
        self.cached_keys, self.spend = set(cached_keys), spend
        self.inserted = []

    async def fetchrow(self, sql, *args):
        if args[0] in self.cached_keys:
            return {"verdict": "supported", "reason": "supported:clear", "shadow_action": "none", "details": "{}",
                    "pinned": True, "answered_model": "gpt-6-luna-openai", "max_rung": 0}
        return None

    async def fetchval(self, sql, *args):
        if "llm_call_log" in sql:
            return self.spend
        self.inserted.append(args)
        return f"00000000-0000-4000-8000-00000000f{len(self.inserted):03d}"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _reply(cost=0.001):
    return SimpleNamespace(content=json.dumps({"claims": []}), model_used="gpt-6-luna-openai",
                           connection_key="openai-direct", cost_usd=cost, attempt_no=1, stop_reason="stop",
                           route_position=0, provider="openai", input_tokens=500, output_tokens=100,
                           satellite_account=None, fallback_used=False)


def test_dry_run_plans_without_a_call_or_a_row():
    inst = cs.instrument()
    key0 = rp.plan_object(_objs()[0], inst)["key"]
    conn = _Conn(cached_keys={key0})

    def caller(*_a):
        raise AssertionError("a DRY RUN never calls the model")

    out = asyncio.run(rp.replay(_Pool(conn), _objs(), run_id="replay-zz", cap=0.1, live=False, caller=caller))
    assert out["dry_run"] is True and out["cache_hits"] == 1 and out["planned_calls"] == cs.VOTES
    assert 0 < out["expected_usd"] <= out["worst_case_usd"] and conn.inserted == []


def test_live_run_is_cache_first_and_writes_one_row_per_object(monkeypatch):
    inst = cs.instrument()
    key0 = rp.plan_object(_objs()[0], inst)["key"]
    conn = _Conn(cached_keys={key0})
    calls = []

    def caller(*a):
        calls.append(a)
        return _reply()

    async def no_log(*a, **k):
        return None

    monkeypatch.setattr(cs, "_log", no_log)
    out = asyncio.run(rp.replay(_Pool(conn), _objs(), run_id="replay-zz", cap=0.1, live=True, caller=caller,
                                attempts=1))
    assert len(calls) == cs.VOTES and len(conn.inserted) == 2
    assert [r["cached"] for r in out["results"]] == [True, False]
    assert out["agreement"]["agree"] == 2 and "stopped" not in out


def test_live_run_stops_at_the_guard(monkeypatch):
    conn = _Conn(spend=0.2999)

    def caller(*_a):
        raise AssertionError("no call may start over the cap")

    out = asyncio.run(rp.replay(_Pool(conn), _objs(), run_id="replay-zz", cap=0.1, live=True, caller=caller,
                                attempts=1))
    assert out["stopped"].startswith("spend guard") and len(out["results"]) == 1
    assert out["results"][0]["verdict"] == "unverified" and out["results"][0]["reason"] == "unverified:budget"


def test_the_run_cap_cannot_pass_the_window_cap():
    with pytest.raises(ValueError):
        asyncio.run(rp.replay(_Pool(_Conn()), _objs(), run_id="replay-zz", cap=0.5, live=False))


def test_cli_refuses_live_without_confirm(capsys):
    assert asyncio.run(rp._main(["--live"])) == 2
    assert "--confirm-spend" in capsys.readouterr().err
    assert asyncio.run(rp._main(["--limit", "0"])) == 2
