"""AA-769 (S233) — /admin/content-quality: cross-tenant T9/T2 quality, highlights, place cards."""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_content_quality as cq
from services.acp_content_writing import service

SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


@pytest.fixture(autouse=True)
def _no_seed_reads(monkeypatch):
    # AA-863 — the seed read is its own query set (tests/unit/test_aa863_seed_audit.py); these tests
    # are about the piece query.
    monkeypatch.setattr(cq.seed_audit, "load_piece_seeds", AsyncMock(return_value={}))


def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


TEXT = ("Opening line here. The monastery sits above Luang Prabang today. "
        "At Arcadia Adventures, we build it. It is a hidden gem.")
LEDGER = [
    {"gate": "F9_cta_fact", "passed": False, "blocking": True, "violations": [
        'audit manual_check: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "The monastery sits above Luang '
        'Prabang today." (claims: Luang Prabang; source: not in seed)']},
    {"gate": "F11_brand_name", "passed": False, "blocking": True, "violations": [
        "names a brand that is not Trail Pulse ('Arcadia Adventures'): 'At Arcadia Adventures, we build it.'"]},
    {"gate": "F1_grounding", "passed": True, "blocking": True, "violations": []},
]
FLAGS = [{"gate": "F9_brand_style", "passed": False, "blocking": False,
          "violations": ['audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "hidden gem"']}]


def test_highlights_locate_every_flagged_span():
    hs = cq.highlights(TEXT, LEDGER, FLAGS)
    assert [h["gate"] for h in hs] == ["F9_cta_fact", "F11_brand_name", "F9_brand_style"]
    assert [TEXT[h["start"]:h["end"]] for h in hs] == [
        "The monastery sits above Luang Prabang today.", "At Arcadia Adventures, we build it.", "hidden gem"]
    assert hs[0]["blocking"] is True and hs[2]["blocking"] is False
    assert "Luang Prabang" in hs[0]["problem"]


def test_highlights_dedupe_gate_listed_in_ledger_and_flags():
    hs = cq.highlights(TEXT, LEDGER + FLAGS, FLAGS)
    assert [h["gate"] for h in hs].count("F9_brand_style") == 1


def test_highlights_skip_unfound_quotes():
    assert cq.highlights("Different text entirely.", LEDGER, []) == []


@pytest.mark.asyncio
async def test_admin_secret_required():
    with pytest.raises(HTTPException) as e:
        await cq.list_pieces(_request(AsyncMock()), x_admin_secret="wrong")
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_list_pieces_filters_by_gate_from_ledger_not_held_reason():
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    conn.fetch.return_value = [{"piece_id": uuid.uuid4(), "status": "held", "failed_gates": '["F9_cta_fact"]',
                                "flag_gates": "[]", "cost_usd": None,
                                "created_at": datetime(2026, 10, 13, tzinfo=timezone.utc)}]
    out = await cq.list_pieces(_request(conn), limit=50, offset=0, tenant_id=None, channel="blog", status=None,
                               gate="F9_cta_fact", q=None, sort="cost_usd", sort_dir="asc", x_admin_secret=SECRET)
    sql, *params = conn.fetch.await_args.args
    assert "jsonb_array_elements(cp.gate_ledger || COALESCE(cp.flags" in sql
    assert "held_reason ILIKE" not in sql
    assert "ORDER BY cost_usd ASC" in sql
    assert params == ["blog", "F9_cta_fact", 50, 0]
    assert out["total"] == 1 and out["data"][0]["failed_gates"] == ["F9_cta_fact"]


@pytest.mark.asyncio
async def test_unknown_sort_falls_back_to_created_at():
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.fetch.return_value = []
    await cq.list_pieces(_request(conn), limit=10, offset=0, tenant_id=None, channel=None, status=None, gate=None,
                         q=None, sort="1; DROP TABLE x", sort_dir="desc", x_admin_secret=SECRET)
    assert "ORDER BY cp.created_at DESC" in conn.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_place_card_verdict_validated_and_recorded():
    conn = AsyncMock()
    with pytest.raises(HTTPException) as e:
        await cq.set_place_card_status("c1", cq.PlaceCardVerdict(status="approved"), _request(conn),
                                       x_admin_secret=SECRET, x_admin_user_id="u1")
    assert e.value.status_code == 422
    conn.fetchrow.return_value = {"card_id": uuid.uuid4(), "status": "rejected", "verified_by": "u1",
                                  "verified_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
                                  "display_name": "Paro", "country": "Bhutan"}
    out = await cq.set_place_card_status("c1", cq.PlaceCardVerdict(status="rejected"), _request(conn),
                                         x_admin_secret=SECRET, x_admin_user_id="u1")
    assert out["status"] == "rejected" and conn.fetchrow.await_args.args[2:] == ("rejected", "u1")
    # S234: every verdict writes an audit row (platform data → tenant_id NULL)
    audit = conn.execute.await_args.args
    assert "acp_shared.audit_log" in audit[0]
    assert audit[1:6] == (None, "admin:u1", "place_card.status", "place_card", "c1")


@pytest.mark.asyncio
async def test_build_place_cards_enqueues_one_tour_job():
    with patch("shared.jobs.queue.enqueue", new=AsyncMock(return_value=("j1", True))) as enq:
        out = await cq.build_place_cards(cq.PlaceCardBuild(tour_id="t1"), _request(AsyncMock()),
                                         x_admin_secret=SECRET, x_admin_user_id="u1")
    assert out == {"job_id": "j1", "created": True}
    assert enq.await_args.args[1:] == ("place_cards_build", {"tour_id": "t1"})


def test_writer_input_snapshot_keeps_what_the_writer_saw():
    ctx = {"atom_text": "seed", "route_segments": [("a1", "m1")], "facts_text": "[Fact id=f]", "scope": "SCOPE: x",
           "brand_audience": {"brand_name": "Terra"}, "brand_rubric_text": "rubric",
           "chosen": {"name": "A", "why_it_works": "w", "best_final_style": "s", "option_id": "o"},
           "goal": {"key": "promotion"}, "channel": "blog", "keyword": "kw", "cta": "Ask", "trip_name": "T",
           "destination": "Laos"}
    snap = service.writer_input_snapshot(ctx)
    assert snap["seed"] == "seed" and snap["route_moments"] == [{"atom_id": "a1", "seed": "m1"}]
    assert snap["brand_pack"] == {"brand_name": "Terra"} and snap["angle"]["name"] == "A"
    assert "option_id" not in snap["angle"] and snap["scope"] == "SCOPE: x"
