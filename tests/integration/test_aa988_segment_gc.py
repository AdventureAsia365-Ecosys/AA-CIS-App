"""AA-988 — the `segment_gc` recompute stage against real Postgres: which orphan Segments it deletes, which references
keep one, and the lock it shares with the segment stage (a GC never runs while a segment run is between its candidate
read and its write). Every pass is scoped to this file's ids (`scope_ids`), so other tests' rows are never touched."""
import asyncio
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.acp_contract import segment_gc as gc  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
SEGS = ("zz988_dead", "zz988_empty", "zz988_live", "zz988_alias_old", "zz988_bf", "zz988_route", "zz988_agr")


def _seed(cur) -> dict:
    tour_id = str(uuid.uuid4())
    cur.execute("SET app.tenant_id = %s", (MASTER,))
    cur.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                     source_group_id, source_status) VALUES (%s, %s, %s, 'zz988', 'Bhutan', %s, 'active')""",
                (tour_id, MASTER, BATCH_ID, tour_id))
    cur.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name,
                     aa_summary, aa_highlights, aa_itineraries, master_status)
                   VALUES (%s, %s, %s, 'zz988', 'S', '[]'::jsonb, 'Day 1 — Paro', 'active')""",
                (tour_id, str(uuid.uuid4()), MASTER))
    cur.execute("RESET app.tenant_id")
    live, dead = f"zz988-live-{tour_id}", f"zz988-dead-{tour_id}"
    for atom_id, deleted in ((live, False), (dead, True)):
        cur.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place, action,
                         activity_type, deleted, is_empty_marker)
                       VALUES (%s, %s, 'Paro Dzong — visit', 1, 'Paro Dzong', 'visit', 'culture', %s, false)""",
                    (atom_id, tour_id, deleted))
    for sid in SEGS:
        cur.execute("""INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action, created_at)
                       VALUES (%s, 'Paro Dzong', 'visit', now())""", (sid,))
    cur.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES (%s, %s), (%s, %s)",
                ("zz988_dead", dead, "zz988_live", live))
    cur.execute("""INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at)
                   VALUES ('zz988_alias_old', 'zz988_live', now())""")
    cur.execute("""INSERT INTO acp_shared.debate_brand_fit_cache (tenant_id, segment_id, brand_version, judge_score,
                     brand_fit_score, cross_brand_distinct, mission_present)
                   VALUES (%s, 'zz988_bf', 1, 1, 1, 1, true)""",
                (MASTER,))
    route_id = f"zz988-route-{tour_id}"
    cur.execute("""INSERT INTO acp_contract.route (route_id, tour_id, hub_name, ordered_segment_ids, first_day,
                     last_day, created_at, version)
                   VALUES (%s, %s, 'Paro', '["zz988_route"]'::jsonb, 1, 2, now(), 1)""",
                (route_id, tour_id))
    cur.execute("""INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, route_segment_ids)
                   VALUES (%s, %s, 'blog', '["zz988_agr"]'::jsonb)""", (MASTER, live))
    return {"tour_id": tour_id, "live": live, "dead": dead, "route_id": route_id}


def _cleanup(cur, seeded):
    cur.execute("DELETE FROM acp_shared.angle_gate_request WHERE atom_id = %s", (seeded["live"],))
    cur.execute("DELETE FROM acp_shared.debate_brand_fit_cache WHERE segment_id = ANY(%s)", (list(SEGS),))
    cur.execute("DELETE FROM acp_contract.route WHERE route_id = %s", (seeded["route_id"],))
    cur.execute("DELETE FROM acp_contract.atom_segment_alias WHERE segment_id_old = ANY(%s)", (list(SEGS),))
    cur.execute("DELETE FROM acp_contract.atom_segment_member WHERE segment_id = ANY(%s)", (list(SEGS),))
    cur.execute("DELETE FROM acp_contract.atom_segment WHERE segment_id = ANY(%s)", (list(SEGS),))
    cur.execute("DELETE FROM acp_contract.tour_atoms WHERE tour_id = %s", (seeded["tour_id"],))


@pytest.fixture
def seeded(db_conn):
    cur = db_conn.cursor()
    data = _seed(cur)
    yield data
    _cleanup(cur, data)
    cur.close()


@pytest_asyncio.fixture
async def pool():
    # app.tenant_id pinned like the catalog reads: the active-atom view joins FORCE-RLS published_tours (a no-op for the
    # CI superuser, needed for a non-superuser test login)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4, server_settings={"app.tenant_id": MASTER})
    yield p
    await p.close()


async def _segments(pool) -> set:
    return {r["segment_id"] for r in await pool.fetch(
        "SELECT segment_id FROM acp_contract.atom_segment WHERE segment_id = ANY($1::text[])", list(SEGS))}


@pytest.mark.asyncio
async def test_report_counts_and_writes_nothing(seeded, pool):
    out = await gc.run_segment_gc(pool, mode="report", scope_ids=list(SEGS))
    assert out["status"] == "report"
    # orphans by the probe rule: dead, empty, alias_old, bf, agr (live has an active member, route is current)
    assert out["orphans"] == 5 and out["deletable"] == 2 and out["dead_members"] == 1
    assert sorted(out["sample_deletable"]) == ["zz988_dead", "zz988_empty"]
    assert out["kept_by"]["alias"] == 1 and out["kept_by"]["brand_fit"] == 1 and out["kept_by"]["angle_request"] == 1
    assert await _segments(pool) == set(SEGS)


@pytest.mark.asyncio
async def test_apply_deletes_zero_reference_orphans_and_their_dead_members_only(seeded, pool):
    out = await gc.run_segment_gc(pool, mode="apply", scope_ids=list(SEGS))
    assert out["status"] == "applied" and out["deleted_segments"] == 2 and out["deleted_members"] == 1
    assert out["kept_on_recheck"] == 0
    assert await _segments(pool) == set(SEGS) - {"zz988_dead", "zz988_empty"}
    live = await pool.fetch("SELECT atom_id FROM acp_contract.atom_segment_member WHERE segment_id = 'zz988_live'")
    assert [r["atom_id"] for r in live] == [seeded["live"]]
    # idempotent: a second pass finds nothing more to delete
    again = await gc.run_segment_gc(pool, mode="apply", scope_ids=list(SEGS))
    assert again["deletable"] == 0 and again["deleted_segments"] == 0


@pytest.mark.asyncio
async def test_gc_waits_for_a_segment_run_and_gives_up_after_the_lock_timeout(seeded, pool, monkeypatch):
    monkeypatch.setattr(gc, "LOCK_TIMEOUT", "300ms")
    async with gc.segment_writes_shared(pool):          # a segment run between its read and its write
        out = await gc.run_segment_gc(pool, mode="apply", scope_ids=list(SEGS))
    assert out["status"] == "skipped" and out["reason"] == "lock_busy"
    assert await _segments(pool) == set(SEGS)          # nothing deleted under the run


@pytest.mark.asyncio
async def test_gc_runs_once_the_segment_run_has_written(seeded, pool):
    release = asyncio.Event()

    async def segment_run():
        async with gc.segment_writes_shared(pool):
            await release.wait()
            # the run re-claims the empty Segment (a new member) before it lets go
            await pool.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) VALUES ($1, $2)",
                               "zz988_empty", seeded["live"])

    run = asyncio.create_task(segment_run())
    await asyncio.sleep(0.2)
    collect = asyncio.create_task(gc.run_segment_gc(pool, mode="apply", scope_ids=list(SEGS)))
    await asyncio.sleep(0.3)
    assert not collect.done()                           # blocked by the shared lock
    release.set()
    await run
    out = await collect
    # the GC read AFTER the run committed: zz988_empty has a live member again and is kept
    assert out["status"] == "applied" and out["deleted_segments"] == 1
    assert await _segments(pool) == set(SEGS) - {"zz988_dead"}


@pytest.mark.asyncio
async def test_two_segment_runs_do_not_block_each_other(pool):
    async with gc.segment_writes_shared(pool):
        await asyncio.wait_for(_nested(pool), timeout=2)


async def _nested(pool):
    async with gc.segment_writes_shared(pool):
        return True
