"""AA-847 (S247) — place backfill: an explicit tour-id list (the coverage gate's missing tours) and a dry-run
estimate priced at each model stage's live route (shared.llm_role_config + shared.llm_model_catalog), plus
migration 261 (a3_place_locate back on Sonnet). No DB, no model, no geocoder."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from services.places import backfill as bf

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "261_place_locate_sonnet.sql"
T1, T2, T3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")
S1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _row(stage, model, in_mtok, out_mtok, shadow=None, pct=0, s_in=None, s_out=None):
    return {"stage": stage, "model_id": model, "in_mtok": in_mtok, "out_mtok": out_mtok, "shadow_model_id": shadow,
            "shadow_sample_pct": pct, "shadow_in_mtok": s_in, "shadow_out_mtok": s_out}


# ── prices ─────────────────────────────────────────────────────────────────────────────────────

def test_prices_follow_the_live_route_and_catalog():
    p = bf.route_prices([_row("a3_place_days", "haiku", 1.0, 5.0), _row("a3_place_locate", "sonnet", 3.0, 15.0)])
    assert p["days"] == pytest.approx((3200 * 1 + 1200 * 5) / 1e6)       # $0.0092, not the old $0.028
    assert p["locate"] == pytest.approx((1000 * 3 + 800 * 15) / 1e6)     # $0.015
    assert p["days_shadow"] == 0.0 and p["embed"] == bf.EMBED_PER_TOUR_USD
    assert p["models"]["a3_place_days"]["model"] == "haiku" and "shadow" not in p["models"]["a3_place_days"]
    assert p["models"]["a3_place_locate"]["model"] == "sonnet"


def test_shadow_is_priced_at_its_own_model_times_the_sample():
    p = bf.route_prices([_row("a3_place_days", "sonnet", 3.0, 15.0, "gpt-5.6-luna", 20, 0.2, 1.2),
                         _row("a3_place_locate", "haiku", 1.0, 5.0)])
    assert p["days_shadow"] == pytest.approx(0.2 * (3200 * 0.2 + 1200 * 1.2) / 1e6)
    assert p["models"]["a3_place_days"]["shadow"] == {"model": "gpt-5.6-luna", "sample_pct": 20}


def test_no_row_uses_safe_defaults_and_cost_table():
    from shared.llm_client.role_config import SAFE_DEFAULTS
    p = bf.route_prices([])
    assert p["models"]["a3_place_days"]["model"] == SAFE_DEFAULTS["a3_place_days"].model_id == "haiku"
    assert p["models"]["a3_place_locate"]["model"] == SAFE_DEFAULTS["a3_place_locate"].model_id == "sonnet"
    assert p["days"] == pytest.approx((3200 * 1 + 1200 * 5) / 1e6)       # COST_TABLE Haiku $1 / $5
    assert p["locate"] == pytest.approx((1000 * 3 + 800 * 15) / 1e6)     # COST_TABLE Sonnet $3 / $15


def test_a_model_with_no_known_price_is_priced_at_sonnet_rates():
    p = bf.route_prices([_row("a3_place_days", "mystery-model", None, None),
                         _row("a3_place_locate", "sonnet-5-5", 2.0, 10.0)])
    assert p["days"] == pytest.approx((3200 * 3 + 1200 * 15) / 1e6)
    assert p["locate"] == pytest.approx((1000 * 2 + 800 * 10) / 1e6)


@pytest.mark.asyncio
async def test_load_route_prices_reads_both_stages_and_survives_a_db_error():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_row("a3_place_days", "haiku", 1.0, 5.0),
                                         _row("a3_place_locate", "sonnet", 3.0, 15.0)])
    p = await bf.load_route_prices(conn)
    assert conn.fetch.await_args.args[1] == ["a3_place_days", "a3_place_locate"]
    assert "llm_model_catalog" in conn.fetch.await_args.args[0] and p["locate"] == pytest.approx(0.015)
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    assert (await bf.load_route_prices(conn))["locate"] == pytest.approx(0.015)


@pytest.mark.asyncio
async def test_estimate_uses_the_route_prices(monkeypatch):
    from services.places import components as comp_mod
    from services.places import days as days_mod
    from services.places import store
    block = SimpleNamespace(day_from=1, day_to=1)
    monkeypatch.setattr(days_mod, "parse_days", lambda text: [block])
    monkeypatch.setattr(days_mod, "needs_model", lambda blocks, rules: True)
    monkeypatch.setattr(days_mod, "rule_day", lambda b: (None, None, None))
    monkeypatch.setattr(days_mod, "anchor_place", lambda o, e: None)
    monkeypatch.setattr(store, "tour_atoms", AsyncMock(return_value=[]))
    comp = SimpleNamespace(locality_norm="", name_norm="erdene zuu", country="Mongolia")
    monkeypatch.setattr(comp_mod, "build_components", lambda *a, **k: ([comp], {}))
    monkeypatch.setattr(store, "resolved_names", AsyncMock(return_value={}))
    monkeypatch.setattr(store, "known_destinations", AsyncMock(return_value={}))
    monkeypatch.setattr(bf, "load_route_prices", AsyncMock(return_value={
        "days": 0.01, "days_shadow": 0.0, "locate": 0.02, "embed": 0.001, "models": {"x": 1}}))
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])          # no pending names
    est = await bf.estimate(conn, [{"tour_id": T1, "source_id": S1, "country": "Mongolia", "aa_itineraries": "x",
                                    "build": None}], 0.0, False)
    assert (est["day_calls"], est["locate_calls"], est["embed_calls"]) == (1, 1, 1)
    assert est["llm_cost_usd"] == pytest.approx(0.031) and est["prices"] == {"x": 1}


# ── tour ids ───────────────────────────────────────────────────────────────────────────────────

def _tour(tid, sid, text="d"):
    return {"tour_id": tid, "source_id": sid, "aa_itineraries": text, "country": "Mongolia"}


@pytest.mark.asyncio
async def test_plan_ids_keeps_request_order_one_per_source_and_reports_skips(monkeypatch):
    from services.places import days as days_mod
    from services.places import store
    live = [_tour(T1, S1), _tour(T2, T2)]
    monkeypatch.setattr(store, "live_tours_by_ids", AsyncMock(return_value=live))
    fresh = {"source_id": T2, "tour_id": T2, "status": "ok", "rule_version": days_mod.RULE_VERSION,
             "days_hash": days_mod.days_hash("d")}
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[fresh])
    picked, skipped = await bf.plan_ids(conn, [T3, S1, T1, T2], only_stale=True)
    assert [t["tour_id"] for t in picked] == [T1]            # S1 (source id) and T1 are the same source: once
    assert skipped == {"not_live": [T3], "fresh": [T2]}
    picked, skipped = await bf.plan_ids(conn, [T2, T1], only_stale=False)
    assert [t["tour_id"] for t in picked] == [T2, T1] and skipped == {"not_live": [], "fresh": []}
    assert picked[0]["build"] == fresh and picked[1]["build"] is None


def test_request_validates_tour_ids():
    from api.routers.admin_places import BackfillRequest
    body = BackfillRequest(tour_ids=[T1.upper(), T1, f" {T2} "])
    assert body.tour_ids == [T1, T2]
    with pytest.raises(ValidationError):
        BackfillRequest(tour_ids=["not-a-uuid"])
    with pytest.raises(ValidationError):
        BackfillRequest(tour_ids=[T1], country="Laos")
    with pytest.raises(ValidationError):
        BackfillRequest(tour_ids=[])
    with pytest.raises(ValidationError):
        BackfillRequest(tour_ids=[f"00000000-0000-4000-8000-{i:012d}" for i in range(101)])
    assert len(BackfillRequest(tour_ids=[f"00000000-0000-4000-8000-{i:012d}" for i in range(100)]).tour_ids) == 100


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.mark.asyncio
async def test_route_runs_only_the_listed_tours(monkeypatch):
    from api.routers import admin as admin_module
    from api.routers.admin_places import BackfillRequest, backfill
    from services.places import enqueue as eq
    monkeypatch.setattr(admin_module, "ADMIN_SECRET", "s")
    monkeypatch.setattr(bf, "wave_busy", AsyncMock(return_value={}))
    plan = AsyncMock()
    monkeypatch.setattr(bf, "plan", plan)
    monkeypatch.setattr(bf, "plan_ids", AsyncMock(return_value=(
        [{"tour_id": T1, "source_id": S1, "build": None}], {"not_live": [T3], "fresh": []})))
    monkeypatch.setattr(bf, "estimate", AsyncMock(return_value={"expected_cost_usd": 0.02, "geocoder_cost_usd": 0.0}))
    enq = AsyncMock(return_value=("j1", True))
    monkeypatch.setattr(eq, "enqueue_place_extract", enq)
    request = MagicMock()
    request.app.state.pool = _Pool(object())
    out = await backfill(BackfillRequest(tour_ids=[T1, T3], dry_run=False, only_stale=False, force=True), request,
                         x_admin_secret="s", x_admin_user_id="u")
    plan.assert_not_awaited()
    assert bf.plan_ids.await_args.kwargs == {"only_stale": False}
    assert out["requested"] == 2 and out["skipped"] == {"not_live": [T3], "fresh": []}
    assert out["enqueued"] == 1 and enq.await_args.kwargs["tour_id"] == T1 and enq.await_args.kwargs["force"] is True


# ── migration 261 ──────────────────────────────────────────────────────────────────────────────

def test_migration_261_moves_only_locate_to_sonnet_and_safe_defaults_mirror_it():
    from shared.llm_client.role_config import SAFE_DEFAULTS
    sql = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    updates = re.findall(r"UPDATE shared\.llm_role_config.*?;", body, re.S)
    assert len(updates) == 1 and updates[0].rstrip().endswith("WHERE stage = 'a3_place_locate';")
    assert "model_id = 'sonnet'" in body and "ARRAY['haiku']::text[]" in body
    assert "shadow_model_id = NULL" in body and "shadow_sample_pct = 0" in body
    assert "VALUES ('261'" in body and body.strip().startswith("BEGIN;") and body.strip().endswith("COMMIT;")
    loc, days = SAFE_DEFAULTS["a3_place_locate"], SAFE_DEFAULTS["a3_place_days"]
    assert (loc.model_id, loc.fallback_model_ids, loc.shadow_model_id) == ("sonnet", ("haiku",), None)
    assert (days.model_id, days.fallback_model_ids) == ("haiku", ("sonnet",))
