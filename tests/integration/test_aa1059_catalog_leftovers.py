"""AA-1059 — on real Postgres: the `segment_catchup` stage (which active Masters it picks, that it writes their
Segments) and every query of the read-only diagnostic scripts/aa1059/catalog_leftovers_diag.py (columns, types, JSON
operators). Rows are invented; every pass of the stage is scoped to this file's tours (`scope_tour_ids`)."""
import importlib.util
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import BATCH_ID, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.export import handler  # noqa: E402
from services.recompute import segment_catchup as sc  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
_DIAG = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1059", "catalog_leftovers_diag.py")


def _tour(cur, *, master: bool, atoms=(), country="Bhutan") -> str:
    tour_id = str(uuid.uuid4())
    cur.execute("SET app.tenant_id = %s", (MASTER,))
    cur.execute("""INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country,
                     source_group_id, source_status) VALUES (%s, %s, %s, 'zz1059', %s, %s, 'active')""",
                (tour_id, MASTER, BATCH_ID, country, tour_id))
    if master:
        cur.execute("""INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name,
                         aa_summary, aa_highlights, aa_itineraries, master_status)
                       VALUES (%s, %s, %s, 'zz1059', 'S', '[]'::jsonb, 'Day 1 — Zzlo', 'active')""",
                    (tour_id, str(uuid.uuid4()), MASTER))
    cur.execute("RESET app.tenant_id")
    for i, (day, place, action, typ) in enumerate(atoms):
        cur.execute("""INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, text, itinerary_day, place, action,
                         activity_type, deleted, is_empty_marker)
                       VALUES (%s, %s, 'zz1059 atom', %s, %s, %s, %s, false, false)""",
                    (f"zz1059-{tour_id}-{i}", tour_id, day, place, action, typ))
    return tour_id


@pytest.fixture
def seeded(db_conn):
    cur = db_conn.cursor()
    tours = {
        # forming atoms, no Segment: the catch-up target (invented places, one country)
        "gap": _tour(cur, master=True, atoms=[(1, "Zzlo Fortress Ridge", "visit", "culture"),
                                              (2, "Qarvi Lake Shore", "walk along", "trek")]),
        # every atom transit by rule: no Segment by design
        "transit": _tour(cur, master=True, atoms=[(1, "Zzlo", "drive to Qarvi", "transit")]),
        # live source, no Master, one hitl version with a pending S1 item
        "no_master": _tour(cur, master=False),
    }
    cur.execute("SET app.tenant_id = %s", (MASTER,))
    cur.execute("""INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name,
                     aa_itineraries, status, metadata)
                   VALUES (%s, %s, 1, 'Zzlo Ridge', 'Day 1 — Zzlo', 'hitl', '{}'::jsonb) RETURNING id""",
                (tours["no_master"], MASTER))
    gc_id = cur.fetchone()[0]
    cur.execute("""INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status,
                     score_overall) VALUES (%s, %s, %s, 'pending'::review_status_enum, 7.5)""",
                (tours["no_master"], gc_id, MASTER))
    cur.execute("RESET app.tenant_id")
    yield tours
    ids = list(tours.values())
    cur.execute("""SELECT DISTINCT m.segment_id FROM acp_contract.atom_segment_member m
                     JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id WHERE t.tour_id = ANY(%s::uuid[])""",
                (ids,))
    segs = [r[0] for r in cur.fetchall()]
    cur.execute("""DELETE FROM acp_contract.atom_segment_member m USING acp_contract.tour_atoms t
                    WHERE t.atom_id = m.atom_id AND t.tour_id = ANY(%s::uuid[])""", (ids,))
    cur.execute("DELETE FROM acp_contract.atom_segment_alias WHERE segment_id_old = ANY(%s) "
                "OR segment_id_canonical = ANY(%s)", (segs, segs))
    cur.execute("""DELETE FROM acp_contract.atom_segment s WHERE s.segment_id = ANY(%s)
                    AND NOT EXISTS (SELECT 1 FROM acp_contract.atom_segment_member m
                                     WHERE m.segment_id = s.segment_id)""",
                (segs,))
    cur.execute("DELETE FROM acp_contract.tour_atoms WHERE tour_id = ANY(%s::uuid[])", (ids,))
    cur.execute("DELETE FROM silver_aa_internal.review_queue WHERE tour_id = ANY(%s::uuid[])", (ids,))
    cur.execute("DELETE FROM silver_aa_internal.generated_content WHERE tour_id = ANY(%s::uuid[])", (ids,))
    cur.execute("DELETE FROM gold_aa_internal.published_tours WHERE tour_id = ANY(%s::uuid[])", (ids,))
    cur.close()


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=5, server_settings={"app.tenant_id": MASTER})
    yield p
    await p.close()


@pytest.fixture
def diag():
    spec = importlib.util.spec_from_file_location("aa1059_catalog_leftovers_diag", _DIAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_candidates_are_the_active_masters_with_atoms_and_no_segment(seeded, pool):
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(sc.CANDIDATES_SQL)]
    mine = [r for r in rows if r["tour_id"] in seeded.values()]
    assert {r["tour_id"] for r in mine} == {seeded["gap"], seeded["transit"]}   # no Master: not an active atom
    planned = sc.plan(mine)
    assert planned == {"tours": [seeded["gap"]], "all_excluded": [seeded["transit"]]}


@pytest.mark.asyncio
async def test_catchup_writes_the_segments_and_the_tour_drops_out(seeded, pool, monkeypatch):
    monkeypatch.setattr(handler, "_connect_tour_lock", lambda: asyncpg.connect(DSN))

    async def no_jev(atoms, new_ids, p):   # the same-place question is Jev (paid): never asked in a test
        return frozenset(), 0

    monkeypatch.setattr("services.acp_contract.segment_matching._apart_pairs", no_jev)
    scope = list(seeded.values())
    out = await sc.run_segment_catchup(pool, max_tours=10, scope_tour_ids=scope)
    assert out["candidates"] == 1 and out["all_excluded"] == 1
    assert out["run"] == 1 and out["segmented"] == 1 and out["failed"] == 0 and out["busy"] == 0
    assert out["segment_ids"]
    async with pool.acquire() as conn:
        members = await conn.fetchval("""SELECT count(*) FROM acp_contract.atom_segment_member m
                                            JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id
                                           WHERE t.tour_id = $1::uuid""", seeded["gap"])
    assert members == 2
    again = await sc.run_segment_catchup(pool, max_tours=10, scope_tour_ids=scope)
    assert again["candidates"] == 0 and again["run"] == 0 and again["all_excluded"] == 1


@pytest.mark.asyncio
async def test_catchup_skips_a_tour_an_atomize_holds(seeded, pool, monkeypatch):
    monkeypatch.setattr(handler, "_connect_tour_lock", lambda: asyncpg.connect(DSN))
    holder = await asyncpg.connect(DSN)
    try:
        assert await holder.fetchval(handler.A3_TOUR_LOCK_SQL, handler.A3_TOUR_LOCK_CLASS, seeded["gap"])
        out = await sc.run_segment_catchup(pool, max_tours=10, scope_tour_ids=list(seeded.values()))
        assert out["busy"] == 1 and out["run"] == 0
    finally:
        await holder.close()


@pytest.mark.asyncio
async def test_every_diagnostic_query_runs(seeded, diag, capsys):
    conn = await asyncpg.connect(DSN, server_settings={"default_transaction_read_only": "on"})
    try:
        await diag.section_seg(conn, None)
        await diag.section_s1(conn, None)
        await diag.section_route(conn, ("BT",))
        await diag.section_gc(conn)
    finally:
        await conn.close()
    text = capsys.readouterr().out
    errors = [line for line in text.splitlines() if line.startswith("ERR ")]
    assert errors == [], errors
    lines = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines() if "=" in line}
    assert lines[f"AA1059 SEG.tour.{seeded['gap']}"].endswith("verdict=no_atomize_job")
    assert "forming=2" in lines[f"AA1059 SEG.tour.{seeded['gap']}"]
    assert lines[f"AA1059 SEG.tour.{seeded['transit']}"].endswith("verdict=all_atoms_excluded")
    s1 = lines[f"AA1059 S1.tour.{seeded['no_master']}"]
    assert "in_no_master=True" in s1 and "pending_items=1" in s1 and "latest_status=hitl" in s1
    assert "next=staff_" in s1 and "gate=" in s1 and "gate=-" not in s1
    assert "AA1059 GC.today.orphans" in lines and "AA1059 GC.rule" in lines
    assert any(k.startswith("AA1059 ROUTE.country.") for k in lines)
    # nothing invented above (place, action, name) reaches the output
    for word in ("Zzlo", "Qarvi", "visit", "drive to"):
        assert word not in text
