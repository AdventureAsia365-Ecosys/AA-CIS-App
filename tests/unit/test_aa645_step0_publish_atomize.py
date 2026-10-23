"""AA-645 step 0 — before any S1 rerun wave: the publish / promote / atomize fixes of the platform-impact memo.

* d06 (T2): a new version of a published tour keeps its first published_at; the change goes to updated_at (CIS 273).
* C2: promote publishes the whole version through process_export and re-atomizes it (dedupe off, force on).
* C6: atoms on a day the new itinerary no longer has are retired; L1: the legacy path retires replaced atoms.
* T13: an atomize that changed atoms drops the T7 brand-fit cache rows of the tour's Segments / Routes.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.acp_contract import a3_atomize
from services.acp_shared.atom_extraction import day_fingerprint
from services.export import handler
from shared.llm_client.role_config import SAFE_DEFAULTS
from shared.repository.published_catalog_repository import PublishedCatalogRepository

TOUR_ID = "44444444-4444-4444-4444-444444444444"
VERSION_ID = "77777777-7777-7777-7777-777777777777"
FP_MODEL = SAFE_DEFAULTS["a3_atomize"].model_id
TWO_DAYS = ("Day 1 — Arrival in Hanoi\nWalk through the Old Quarter.\n\n"
            "Day 2 — Halong Bay Cruise\nKayak through limestone caves.")


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class _Resp:
    def __init__(self, content):
        self.content = content
        self.model_used, self.input_tokens, self.output_tokens, self.cost_usd = "m", 10, 5, 0.001
        self.stop_reason, self.satellite_account, self.fallback_used, self.provider = "end_turn", "acc3", False, "x"


def _gateway(*contents):
    client = MagicMock()
    client.generate = MagicMock(side_effect=[_Resp(c) for c in contents])
    return patch("services.acp_contract.a3_atomize.LLMClient", return_value=client), client.generate


def _atoms(place, action):
    return json.dumps({"atoms": [{"place": place, "action": action, "activity_type": "culture"}]})


def _sql_calls(conn, needle):
    return [c for c in conn.execute.call_args_list if needle in c.args[0]]


# ── d06: published_at is kept on a re-publish ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_keeps_published_at_and_sets_updated_at():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": "pt-1"}
    await PublishedCatalogRepository(conn).insert({"tour_id": TOUR_ID, "generated_content_id": VERSION_ID,
                                                   "aa_name": "Tour"})
    sql = conn.fetchrow.call_args.args[0]
    update = sql.split("DO UPDATE SET", 1)[1]
    assert "published_at" not in update.split("--")[0] and "published_at         =" not in update
    assert "updated_at           = NOW()" in update
    # the INSERT branch does not name either column: both come from their now() defaults (a first publish)
    assert "published_at" not in sql.split("VALUES", 1)[0] and "updated_at" not in sql.split("VALUES", 1)[0]


# ── C2: promote ───────────────────────────────────────────────────────────────────────────────────────────────

def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_pool(conn))))


async def _promote(conn, **patches):
    from api.routers import admin_pipeline as ap
    with patch.object(ap, "verify_admin_secret", MagicMock()), \
         patch.object(ap, "source_state", AsyncMock(return_value=patches.get(
             "src", SimpleNamespace(live=True, reason=None)))), \
         patch("services.export.handler.process_export",
               patches.get("export", AsyncMock(return_value={"catalog_id": "pt-1", "atomize_job_id": "job-1"}))) as ex:
        out = await ap.promote_tour_version(TOUR_ID, 2, _request(conn), x_admin_secret="s")
    return out, ex


@pytest.mark.asyncio
async def test_promote_publishes_the_whole_version_and_forces_a_reatomize():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": VERSION_ID, "status": "approved"}
    out, ex = await _promote(conn)
    ex.assert_awaited_once()
    assert ex.await_args.args == (VERSION_ID,)
    assert ex.await_args.kwargs == {"atomize_dedupe": False, "atomize_force": True,
                                    "atomize_created_by": "admin:promote_version"}
    assert out == {"promoted": True, "published_tour_id": "pt-1", "version_num": 2, "version_id": VERSION_ID,
                   "atomize_job_id": "job-1"}
    # no partial copy onto published_tours any more (the old UPDATE … SET aa_name, published_at = NOW())
    assert not any("published_tours" in c.args[0] for c in conn.execute.call_args_list)


@pytest.mark.asyncio
async def test_promote_unknown_version_is_404():
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException) as e:
        await _promote(conn)
    assert e.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["hitl", "rejected", "draft", "passed"])
async def test_promote_refuses_a_version_that_never_passed_the_gate(status):
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": VERSION_ID, "status": status}
    with pytest.raises(HTTPException) as e:
        await _promote(conn)
    assert e.value.status_code == 409 and "Review Queue" in e.value.detail


@pytest.mark.asyncio
async def test_promote_refuses_a_dead_source_before_touching_the_version():
    from shared.tour_liveness import SourceState
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": VERSION_ID, "status": "approved"}
    dead = SourceState(tour_id=TOUR_ID, exists=True, source_status="trashed", deleted_at=None)
    export = AsyncMock()
    with pytest.raises(HTTPException) as e:
        await _promote(conn, src=dead, export=export)
    assert e.value.status_code == 409 and e.value.headers["X-Error-Code"] == "source_not_live"
    export.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_maps_the_locked_publish_refusal_and_other_lifecycle_refusals():
    """AA-986 L2: the publish runs in the lifecycle service; a retire that lands between promote's check and the
    locked publish comes back from process_export as SourceNotLiveError (409 source_not_live), and any other
    LifecycleError keeps its status, code and detail."""
    from services.tour_lifecycle import LifecycleError
    from shared.tour_liveness import SourceNotLiveError, SourceState
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": VERSION_ID, "status": "approved"}
    dead = SourceState(tour_id=TOUR_ID, exists=True, source_status="trashed", deleted_at=None)
    with pytest.raises(HTTPException) as e:
        await _promote(conn, export=AsyncMock(side_effect=SourceNotLiveError(dead)))
    assert e.value.status_code == 409 and e.value.headers["X-Error-Code"] == "source_not_live"
    with pytest.raises(HTTPException) as e:
        await _promote(conn, export=AsyncMock(side_effect=LifecycleError(409, "master_trashed", "it is trashed")))
    assert e.value.status_code == 409 and e.value.headers["X-Error-Code"] == "master_trashed"
    assert e.value.detail == "master_trashed: it is trashed"


@pytest.mark.asyncio
async def test_process_export_passes_the_atomize_options_and_returns_the_job(monkeypatch, plain_publish):
    # AA-986 L2: process_export publishes through services.tour_lifecycle.publish; plain_publish
    # (tests/unit/conftest.py) stands in with the repository insert only (the real publish: integration suite).
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    row = {"id": VERSION_ID, "tour_id": TOUR_ID, "tenant_id": TOUR_ID, "batch_id": None, "aa_name": "Tour",
           "aa_subtitle": "s", "aa_summary": "sum", "aa_description": "d", "aa_highlights": "[]",
           "aa_itineraries": TWO_DAYS, "mobile_card_text": None, "seo_title": "t", "seo_meta": "m",
           "seo_keywords_used": "[]", "og_tags": "{}", "quality_score_id": None, "quality_score": 9.0,
           "country": "Vietnam", "duration": "2 days", "src_source_status": "active", "src_deleted_at": None}
    conn = AsyncMock()
    conn.fetchrow.side_effect = [row, {"id": "pt-1"}]
    enq = AsyncMock(return_value="job-7")
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", enq):
        out = await handler.process_export(VERSION_ID, atomize_dedupe=False, atomize_force=True,
                                           atomize_created_by="admin:promote_version")
    assert out["atomize_job_id"] == "job-7" and out["tour_id"] == TOUR_ID
    kw = enq.await_args.kwargs
    assert kw["dedupe"] is False and kw["force"] is True and kw["created_by"] == "admin:promote_version"


@pytest.mark.asyncio
async def test_enqueue_force_goes_into_the_payload_and_dedupe_off_drops_the_key():
    from services.jobs import a3_atomize_job
    with patch.object(a3_atomize_job, "enqueue", AsyncMock(return_value=("job-1", True))) as enq:
        await a3_atomize_job.enqueue_a3_atomize(MagicMock(), tour_id=TOUR_ID, version_id=VERSION_ID, country="VN",
                                                rewritten={}, created_by="x", dedupe=False, force=True)
        await a3_atomize_job.enqueue_a3_atomize(MagicMock(), tour_id=TOUR_ID, version_id=VERSION_ID, country="VN",
                                                rewritten={}, created_by="x")
    (_, _, forced), forced_kw = enq.await_args_list[0].args, enq.await_args_list[0].kwargs
    (_, _, plain), plain_kw = enq.await_args_list[1].args, enq.await_args_list[1].kwargs
    assert forced["force"] is True and forced_kw["idempotency_key"] is None
    assert "force" not in plain and plain_kw["idempotency_key"] == f"a3_atomize:{VERSION_ID}"


# ── atomize: force, C6 orphan days, L1 legacy ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_force_rereads_every_day_even_with_matching_fingerprints():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"day_number": 1, "fingerprint_hash": day_fingerprint("Arrival in Hanoi", "Walk through the Old Quarter.",
                                                              FP_MODEL)},
        {"day_number": 2, "fingerprint_hash": day_fingerprint("Halong Bay Cruise", "Kayak through limestone caves.",
                                                              FP_MODEL)}]
    cm, gen = _gateway(_atoms("Old Quarter", "walk"), _atoms("Halong Bay", "kayak"))
    with cm, patch.object(a3_atomize, "ground_day_atoms", AsyncMock(side_effect=lambda atoms, *a, **k: atoms)):
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": TWO_DAYS}, _pool(conn),
                                              version_id=VERSION_ID, force=True)
    assert gen.call_count == 2 and out["days_read"] == 2 and out["status"] == "success"


@pytest.mark.asyncio
async def test_without_force_matching_fingerprints_still_skip():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"day_number": 1, "fingerprint_hash": day_fingerprint("Arrival in Hanoi", "Walk through the Old Quarter.",
                                                              FP_MODEL)},
        {"day_number": 2, "fingerprint_hash": day_fingerprint("Halong Bay Cruise", "Kayak through limestone caves.",
                                                              FP_MODEL)}]
    cm, gen = _gateway()
    with cm:
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": TWO_DAYS}, _pool(conn),
                                              version_id=VERSION_ID)
    gen.assert_not_called()
    assert out["status"] == "skipped"


@pytest.mark.asyncio
async def test_atoms_on_days_the_new_itinerary_lacks_are_retired():
    """s1-001: live 4 days, v2 1 day — days 2-4 must not keep live atoms (memo C6). Also on an all-skipped run."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.execute.return_value = "UPDATE 6"
    one_day = "Day 1 — Kathmandu\nA walk in Thamel."
    cm, _ = _gateway(_atoms("Thamel", "walk"))
    with cm, patch.object(a3_atomize, "ground_day_atoms", AsyncMock(side_effect=lambda atoms, *a, **k: atoms)):
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": one_day}, _pool(conn),
                                              version_id=VERSION_ID)
    calls = _sql_calls(conn, "NOT (itinerary_day = ANY($3::smallint[]))")
    assert len(calls) == 1
    sql, tour, scope, days = calls[0].args
    assert "itinerary_day IS NULL" in sql and "deleted = true" in sql
    assert tour == TOUR_ID and scope == "platform" and days == [1]
    assert out["orphan_atoms_deleted"] == 6


@pytest.mark.asyncio
async def test_legacy_path_retires_the_atoms_it_replaces():
    """L1: no "Day N —" headers → legacy path with random ids; after a good read the older live atoms go."""
    conn = AsyncMock()
    conn.fetchval.return_value = None
    conn.execute.return_value = "UPDATE 3"
    cm, _ = _gateway(_atoms("Old Quarter", "walk"))
    with cm, patch.object(a3_atomize, "record_call_with_pool", AsyncMock()):
        out = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": "free text, no day headers"},
                                              _pool(conn), version_id=VERSION_ID)
    inserted = [c.args[1] for c in _sql_calls(conn, "INSERT INTO acp_contract.tour_atoms")]
    retire = _sql_calls(conn, "NOT (atom_id = ANY($3::text[]))")
    assert len(retire) == 1 and retire[0].args[3] == inserted
    assert out["replaced_atoms_deleted"] == 3


@pytest.mark.asyncio
async def test_legacy_force_ignores_the_whole_tour_hash_skip():
    row = {"id": TOUR_ID, "name": "T", "aa_summary": "", "aa_highlights": [], "itinerary_source": "free text"}
    conn = AsyncMock()
    conn.fetchval.return_value = a3_atomize._source_hash(row)
    cm, gen = _gateway(_atoms("Old Quarter", "walk"))
    with cm, patch.object(a3_atomize, "record_call_with_pool", AsyncMock()):
        skipped = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": "free text"}, _pool(conn))
        forced = await a3_atomize.run_a3_atomize(TOUR_ID, {"name": "T", "itineraries": "free text"}, _pool(conn),
                                                 force=True)
    assert skipped["status"] == "skipped" and forced["status"] == "success" and gen.call_count == 1


# ── T13: brand-fit cache ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_brand_fit_cache_by_segment_and_route():
    db = AsyncMock()
    db.execute.return_value = "DELETE 4"
    assert await handler.clear_brand_fit_cache(db, ["seg_a", "seg_b"]) == 4
    sql, ids = db.execute.await_args.args
    assert "DELETE FROM acp_shared.debate_brand_fit_cache" in sql
    assert "segment_id = ANY($1::text[])" in sql and "ordered_segment_ids ?| $1::text[]" in sql
    assert ids == ["seg_a", "seg_b"]


@pytest.mark.asyncio
async def test_clear_brand_fit_cache_without_segments_sends_nothing():
    db = AsyncMock()
    assert await handler.clear_brand_fit_cache(db, []) == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_skipped_atomize_keeps_the_cache():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"segment_id": "seg_a"}])
    with patch("services.export.handler.open_job_pool", AsyncMock(return_value=conn)), \
         patch("services.acp_contract.a3_atomize.run_a3_atomize",
               AsyncMock(return_value={"status": "skipped", "atom_count": 0, "orphan_atoms_deleted": 0})), \
         patch("services.recompute.run_stages", AsyncMock(return_value={})), \
         patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock(return_value=("job-9", True))), \
         patch("services.places.enqueue.after_atomize", AsyncMock(return_value={})):
        out = await handler._run_a3_atomize_background(tour_id=TOUR_ID, rewritten={}, country="", version_id="v")
    assert "brand_fit_cache_cleared" not in out
    assert not [c for c in conn.execute.await_args_list if "debate_brand_fit_cache" in c.args[0]]
