"""AA-645 step 0 (C2, d06) on top of AA-986 L2, on real Postgres: the admin promote of an older version publishes
through the tour lifecycle service (`services.tour_lifecycle.publish`, the only writer of published_tours inserts).

* promote of an older approved version of a live tour: the Master carries that version's text, the first
  published_at is kept and updated_at moves (d06), the a3_atomize job is forced and not deduped, and running it
  brings that version's atoms back in acp_contract.v_active_tour_atoms (the replaced version's atoms leave the view,
  a day the old itinerary lacks keeps no atom). Without `force` the same run would have kept the newer version's
  atoms (shown in the test).
* promote on a dead source: 409 source_not_live, nothing changes (version stays approved, Master untouched, no job).
* a retire that commits between promote's check and the publish: the service's locked re-check refuses, process_export
  turns it into the L1 refusal (version → hitl) and promote answers 409 source_not_live; no Master change, no job.

process_export opens its own connection: it gets the test connection (never closed), so everything runs in one
transaction that is rolled back. The LLM / Jev / gateway config of the atomize are stubbed (no network, $0).
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _ttv_live_standin import TTV_LIVE_STANDIN_DDL  # noqa: E402

from services import tour_lifecycle as tl  # noqa: E402
import services.jobs.a3_atomize_job  # noqa: E402,F401  (registers the a3_atomize kind process_export enqueues)
import services.jobs.t2_rewrite_job  # noqa: E402,F401  (kinds whose on_terminal hooks the retire cascade may run)
import services.jobs.t8_angles_job  # noqa: E402,F401
import services.jobs.t9_write_job  # noqa: E402,F401

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
FIRST_PUBLISHED = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)

V1_DAYS = "Day 1 — Old Quarter\nA slow morning walk round Hoan Kiem Lake."
V2_DAYS = ("Day 1 — Culture\nA guided visit to the Temple of Literature.\n\n"
           "Day 2 — Bay\nA day cruise on Halong Bay.")
# day-body token → the atom the fake model extracts for that day
_ATOMS = {
    "Hoan Kiem": {"place": "Hoan Kiem Lake", "action": "walk round the lake", "evidence": "walk round Hoan Kiem Lake"},
    "Temple of Literature": {"place": "Temple of Literature", "action": "guided visit",
                             "evidence": "visit to the Temple of Literature"},
    "Halong": {"place": "Halong Bay", "action": "day cruise", "evidence": "cruise on Halong Bay"},
}


class _NoClose:
    """process_export opens and closes its own connection: hand it the test connection, keep it open."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def close(self):
        return None


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_OneConnPool(conn))))


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(TTV_LIVE_STANDIN_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _no_recompute(monkeypatch):
    rec = AsyncMock()
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", rec)
    return rec


# ── seeds and helpers ──────────────────────────────────────────────────────────────────────────────────────────────

async def _live_tour(conn) -> str:
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "source_status, source_group_id, source_version) "
        "VALUES ($1, $2, $3, 'aa645 promote', 'Vietnam', '2 days', 'active', $4, 1)",
        tour_id, MASTER, BATCH_ID, uuid.uuid4())
    return str(tour_id)


async def _version(conn, tour_id, num, itinerary) -> str:
    return str(await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_itineraries, status) VALUES ($1, $2, $3, $4, 'A short Hanoi tour.', $5, 'approved') RETURNING id",
        uuid.UUID(tour_id), MASTER, num, f"aa645 v{num}", itinerary))


async def _export(conn, version_id, **kw) -> dict:
    from services.export import handler
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=_NoClose(conn))):
        return await handler.process_export(version_id, **kw)


async def _promote(conn, tour_id, num):
    from api.routers import admin_pipeline as ap
    with patch.object(ap, "verify_admin_secret", lambda _s: None), \
         patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=_NoClose(conn))):
        return await ap.promote_tour_version(tour_id, num, _req(conn), x_admin_secret="s")


def _fake_generate(req):
    day = req.user_prompt.split("(extract atoms only from this section):", 1)[1]
    atoms = [dict(a, activity_type="sightseeing") for token, a in _ATOMS.items() if token in day]
    return SimpleNamespace(content=json.dumps({"atoms": atoms}), model_used="test-model", input_tokens=1,
                           output_tokens=1, cost_usd=0.0, stop_reason="end_turn", satellite_account=None,
                           fallback_used=False, provider="test")


async def _run_atomize_job(conn, job_id, *, force=None) -> dict:
    """Run the job's payload the way the a3_atomize job does (run_a3_atomize), with the model stubbed. `force`
    overrides the payload's flag (to show what an un-forced run would have done)."""
    from services.acp_contract.a3_atomize import run_a3_atomize
    payload = json.loads(await conn.fetchval("SELECT payload::text FROM shared.job WHERE id = $1::uuid", job_id))
    use_force = bool(payload.get("force")) if force is None else force
    with patch("services.acp_contract.a3_atomize.LLMClient", lambda: SimpleNamespace(generate=_fake_generate)), \
         patch("services.acp_contract.a3_atomize.get_stage_config",
               AsyncMock(return_value=SimpleNamespace(model_id="test-model", role="writer"))), \
         patch("services.acp_contract.a3_atomize.ground_day_atoms",
               AsyncMock(side_effect=lambda atoms, *a, **k: atoms)), \
         patch("services.acp_contract.a3_atomize.record_call_with_pool", AsyncMock()):
        return await run_a3_atomize(payload["tour_id"], payload["rewritten"], _OneConnPool(conn),
                                    country=payload.get("country") or "", version_id=payload["version_id"],
                                    force=use_force)


async def _served_places(conn, tour_id) -> set:
    rows = await conn.fetch("SELECT place FROM acp_contract.v_active_tour_atoms WHERE tour_id = $1::uuid",
                            uuid.UUID(tour_id))
    return {r["place"] for r in rows}


async def _master(conn, tour_id):
    return await conn.fetchrow(
        "SELECT generated_content_id::text AS gc, aa_name, aa_itineraries, master_status::text AS status, "
        "published_at, updated_at FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid",
        uuid.UUID(tour_id))


async def _jobs_for(conn, version_id) -> int:
    return await conn.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'a3_atomize' "
                               "AND payload->>'version_id' = $1", version_id)


async def _published_v1_then_v2(conn):
    """A live tour first published with v1 long ago (its atoms read), then rewritten to v2 (atoms re-read)."""
    tour = await _live_tour(conn)
    v1 = await _version(conn, tour, 1, V1_DAYS)
    v2 = await _version(conn, tour, 2, V2_DAYS)
    first = await _export(conn, v1)
    # the first publish happened long ago: both dates at that time (what 273's backfill / defaults give)
    await conn.execute("UPDATE gold_aa_internal.published_tours SET published_at = $2, updated_at = $2 "
                       "WHERE tour_id = $1::uuid", uuid.UUID(tour), FIRST_PUBLISHED)
    assert (await _run_atomize_job(conn, first["atomize_job_id"]))["status"] == "success"
    assert await _served_places(conn, tour) == {"Hoan Kiem Lake"}
    second = await _export(conn, v2)
    assert (await _run_atomize_job(conn, second["atomize_job_id"]))["status"] == "success"
    assert await _served_places(conn, tour) == {"Temple of Literature", "Halong Bay"}
    m = await _master(conn, tour)
    # d06 through the service's publish: the rewrite kept the first published_at and moved updated_at
    assert m["gc"] == v2 and m["published_at"] == FIRST_PUBLISHED and m["updated_at"] > FIRST_PUBLISHED
    return tour, v1, v2


# ── tests ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_an_older_version_serves_its_text_and_atoms_and_keeps_published_at(conn):
    tour, v1, v2 = await _published_v1_then_v2(conn)
    before = await _master(conn, tour)
    events_before = await conn.fetchval("SELECT count(*) FROM gold_aa_internal.tour_lifecycle_event "
                                        "WHERE tour_id = $1::uuid", uuid.UUID(tour))

    out = await _promote(conn, tour, 1)

    assert out["promoted"] is True and out["version_num"] == 1 and out["version_id"] == v1
    m = await _master(conn, tour)
    assert out["published_tour_id"] == str(await conn.fetchval(
        "SELECT id FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid", uuid.UUID(tour)))
    # the whole version is served (not the old aa_name / aa_subtitle copy), on the same active Master row
    assert (m["gc"], m["aa_name"], m["aa_itineraries"], m["status"]) == (v1, "aa645 v1", V1_DAYS, "active")
    # d06: the first published_at stays; updated_at records the promote
    assert m["published_at"] == FIRST_PUBLISHED and m["updated_at"] >= before["updated_at"]
    # served before and after: the service writes no lifecycle event, and still exactly one active Master
    assert await conn.fetchval("SELECT count(*) FROM gold_aa_internal.tour_lifecycle_event WHERE tour_id = $1::uuid",
                               uuid.UUID(tour)) == events_before
    # the atomize job: a new one (v1's publish key is spent), forced, not keyed, by the promote
    job = await conn.fetchrow("SELECT payload, idempotency_key, created_by FROM shared.job WHERE id = $1::uuid",
                              out["atomize_job_id"])
    payload = json.loads(job["payload"])
    assert job["idempotency_key"] is None and job["created_by"] == "admin:promote_version"
    assert payload["force"] is True and payload["version_id"] == v1
    assert payload["rewritten"]["itineraries"] == V1_DAYS
    assert await _jobs_for(conn, v1) == 2

    # Without force, v1's day fingerprints still match: day 1 is skipped and v2's day-1 atom would stay served
    # (only v2's day 2, absent from v1, is retired).
    unforced = await _run_atomize_job(conn, out["atomize_job_id"], force=False)
    assert unforced["status"] == "skipped" and unforced["orphan_atoms_deleted"] == 1
    assert await _served_places(conn, tour) == {"Temple of Literature"}

    # The forced job re-reads v1: its atoms come back (content-hash ids), v2's leave the view.
    forced = await _run_atomize_job(conn, out["atomize_job_id"])
    assert forced["status"] == "success" and forced["days_read"] == 1
    assert await _served_places(conn, tour) == {"Hoan Kiem Lake"}
    assert await conn.fetchval("SELECT count(*) FROM acp_contract.tour_atoms WHERE tour_id = $1::uuid "
                               "AND deleted AND place IN ('Temple of Literature', 'Halong Bay')", uuid.UUID(tour)) == 2


@pytest.mark.asyncio
async def test_promote_on_a_dead_source_is_refused_and_changes_nothing(conn):
    tour, v1, v2 = await _published_v1_then_v2(conn)
    await tl.retire_source(conn, tour, actor="test")
    before = await _master(conn, tour)
    assert before["status"] == "inactive"          # the retire cascade switched the Master off

    with pytest.raises(HTTPException) as exc:
        await _promote(conn, tour, 1)

    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "source_not_live"
    assert exc.value.detail.startswith("source_not_live: cannot promote this version")
    assert await _master(conn, tour) == before     # same version, still off, same dates
    assert await conn.fetchval("SELECT status::text FROM silver_aa_internal.generated_content WHERE id = $1::uuid",
                               v1) == "approved"   # refused before the export: v1 is not flipped to hitl
    assert await _jobs_for(conn, v1) == 1          # only its first publish's job


@pytest.mark.asyncio
async def test_promote_refused_by_the_services_locked_recheck_when_a_retire_lands_first(conn):
    """The retire commits after promote's own check but before the publish: the lifecycle service re-checks the
    source under FOR SHARE and refuses; process_export runs the L1 refusal; promote answers 409."""
    tour, v1, v2 = await _published_v1_then_v2(conn)
    real_publish = tl.publish

    async def retire_then_publish(c, data, **kw):
        await tl.retire_source(c, tour, actor="concurrent")
        return await real_publish(c, data, **kw)

    with patch("services.tour_lifecycle.publish", retire_then_publish):
        with pytest.raises(HTTPException) as exc:
            await _promote(conn, tour, 1)

    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "source_not_live"
    m = await _master(conn, tour)
    assert m["gc"] == v2 and m["status"] == "inactive" and m["published_at"] == FIRST_PUBLISHED
    # L1 refusal: the version leaves 'approved' (no approved version without a Master); no atomize job for it
    assert await conn.fetchval("SELECT status::text FROM silver_aa_internal.generated_content WHERE id = $1::uuid",
                               v1) == "hitl"
    assert await _jobs_for(conn, v1) == 1
