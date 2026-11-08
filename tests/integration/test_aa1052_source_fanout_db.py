"""AA-1052 W6 — the source-change fan-out on real Postgres (CIS 392 shared.source_change_mark,
services/quality/source_fanout.py).

One transaction, rolled back. Invented tours, names and places (no text from the DB, audits or prompts). Triggers are
off for the transaction (session_replication_role = replica: the FKs to tenants, batches and requests are not the
point); CHECK constraints still apply. ``gold_aa_internal.tenant_tour_versions`` is created by migration 001, which
the replay skips: a stand-in with the columns the fan-out reads is created when it is missing.

What it proves:
  * the S1 / T2 / T9 dependent SQL runs on the replayed schema and finds exactly the objects written from the source
    before the change (the live and the newest Master version; each tenant's newest version per Master; the stored
    pieces citing an atom of the tour by their angle request or a route moment), not the ones written after it, not
    a processing piece, not a T2 version of the new Master;
  * an S1 version whose stored FactSheet text equals the sheet rebuilt from the raw tour now stays unmarked;
  * a second fan-out of the same source keeps ONE open mark per object (hits 2); a clear lets a new one open;
  * the place card fan-out reaches the pieces through atom_place_card;
  * no model is called and no object row changes (status, text, updated columns).
"""
import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.quality import source_fanout as sf  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = uuid.UUID("00000000-0000-0000-0000-00000a105201")
AGENCY = uuid.UUID("00000000-0000-0000-0000-00000a105202")
MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
(MIGRATION,) = MIGRATIONS.glob("[0-9][0-9][0-9]_source_change_mark.sql")


@pytest_asyncio.fixture
async def conn(monkeypatch):
    from shared.llm_client import client
    monkeypatch.setattr(client.LLMClient, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")))
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("SET LOCAL session_replication_role = replica")
        await c.execute(MIGRATION.read_text().replace("BEGIN;", "").replace("COMMIT;", ""))   # re-runnable
        await _schema(c)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _schema(conn):
    if not await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NOT NULL AS ok"):
        await conn.execute(
            "CREATE TABLE gold_aa_internal.tenant_tour_versions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "published_tour_id uuid NOT NULL, version_number int NOT NULL, rewritten_content jsonb NOT NULL, "
            "status text NOT NULL, edit_source text NOT NULL, rewrite_language text NOT NULL, seo_mode text NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), qa_status text NOT NULL, qa_repair_count smallint NOT NULL, "
            "qa_auto_passed boolean NOT NULL, job_id uuid, generation_meta jsonb, judge_breakdown jsonb, fs_id uuid)")
    await conn.execute("ALTER TABLE gold_aa_internal.tenant_tour_versions ADD COLUMN IF NOT EXISTS fs_id uuid")
    await conn.execute("ALTER TABLE silver_aa_internal.generated_content ADD COLUMN IF NOT EXISTS fs_id uuid")
    for col in ("job_id uuid", "writer_input jsonb"):
        await conn.execute(f"ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS {col}")


RAW = {"src_name": "Invented Ridge Walk", "src_summary": "A quiet walk above an invented lake.",
       "src_itineraries": "Day 1: Walk the ridge to Lake Imaginaria.", "country": "Vietnam", "duration": "1 day",
       "inclusions": "Guide"}


async def _tour(conn):
    tour = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, src_summary, "
        "src_itineraries, country, duration, inclusions, source_status, source_group_id, source_version) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active', $10, 1)",
        tour, TENANT, uuid.uuid4(), RAW["src_name"], RAW["src_summary"], RAW["src_itineraries"], RAW["country"],
        RAW["duration"], RAW["inclusions"], uuid.uuid4())
    return tour


async def _sheet(conn, tour, render):
    return await conn.fetchval(
        "INSERT INTO acp_shared.fact_sheet (tenant_id, workflow, builder, schema_version, builder_version, fs_sha, "
        "tour_id, line_count, lines, render_text) VALUES ($1, 's1', 'tour', 'factsheet/1', '1', $2, $3, 0, "
        "'[]'::jsonb, $4) RETURNING fs_id", TENANT, uuid.uuid4().hex * 2, tour, render)


async def _version(conn, tour, num, *, at, fs=None):
    return await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, status, retry_count, "
        "metadata, fs_id, aa_name, created_at) VALUES ($1, $2, $3, 'approved'::content_status_enum, 0, '{}'::jsonb, "
        "$4, 'Invented Ridge Walk', $5) RETURNING id", tour, TENANT, num, fs, at)


async def _master(conn, tour, gc):
    return await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, aa_summary, "
        "aa_highlights, master_status) VALUES ($1, $2, $3, 'Invented Ridge Walk', 'S', '[]'::jsonb, 'active') "
        "RETURNING id", tour, gc, TENANT)


async def _ttv(conn, pt, num, *, at, status="approved", tenant=AGENCY):
    vid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (id, tenant_id, published_tour_id, version_number, "
        "rewritten_content, status, edit_source, rewrite_language, seo_mode, created_at, qa_status, qa_repair_count, "
        "qa_auto_passed) VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 'ai_generated', 'en', 'off', $6, 'passed', 0, true)",
        vid, tenant, pt, num, status, at)
    return vid


async def _atom(conn, tour, label):
    atom = f"aa1052-{label}-{uuid.uuid4().hex[:8]}"
    await conn.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence) "
                       "VALUES ($1, $2, 'platform', 'Walk the invented ridge', 'Lake Imaginaria', 'Day 1')",
                       atom, tour)
    return atom


async def _piece(conn, *, atom=None, moments=(), at, status="approved"):
    req, piece = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO acp_shared.angle_gate_request (request_id, tenant_id, atom_id, status, created_at, updated_at) "
        "VALUES ($1, $2, $3, 'approved', $4, $4)", req, AGENCY, atom or "aa1052-unrelated", at)
    await conn.execute(
        "INSERT INTO acp_shared.content_piece (piece_id, tenant_id, angle_gate_request_id, attempt_number, "
        "content_text, status, gate_ledger, repair_log, discarded_attempts, created_at, writer_input) "
        "VALUES ($1, $2, $3, 1, 'An invented blog about a ridge.', $4, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, $5, "
        "$6::jsonb)", piece, AGENCY, req, status, at,
        json.dumps({"route_moments": [{"atom_id": a, "seed": "x"} for a in moments]}))
    return piece


async def _marks(conn, **where):
    clauses = " AND ".join(f"{k} = ${i + 1}" for i, k in enumerate(where))
    return [dict(r) for r in await conn.fetch(
        f"SELECT object_kind, object_id::text AS object_id, source_kind, source_ref, reason, hits, "
        f"cleared_at IS NULL AS open FROM shared.source_change_mark WHERE {clauses} ORDER BY object_kind, object_id",
        *where.values())]


@pytest.mark.asyncio
async def test_a_raw_tour_change_marks_what_was_written_from_it_and_nothing_else(conn):
    now = await conn.fetchval("SELECT now() AS ok")
    before, after = now - timedelta(days=1), now + timedelta(hours=1)
    tour = await _tour(conn)
    raw_row = await conn.fetchrow(sf.RAW_ROW_SQL, str(tour))
    same = await _sheet(conn, tour, sf.current_tour_render(raw_row))       # the facts the writer read are current
    v1 = await _version(conn, tour, 1, at=before - timedelta(days=1), fs=same)          # the live Master
    v2 = await _version(conn, tour, 2, at=before)                                       # newest, no stored sheet
    v_old = await _version(conn, tour, 0, at=before - timedelta(days=3))                # neither live nor newest
    pt = await _master(conn, tour, v1)
    t2_old = await _ttv(conn, pt, 1, at=before - timedelta(hours=2))
    t2_new = await _ttv(conn, pt, 2, at=before)                         # the tenant's newest: the dependent
    t2_rejected_tenant = await _ttv(conn, pt, 1, at=before, status="rejected", tenant=uuid.uuid4())
    atom = await _atom(conn, tour, "a")
    p_req = await _piece(conn, atom=atom, at=before)
    p_moment = await _piece(conn, moments=[atom], at=before)
    p_processing = await _piece(conn, atom=atom, at=before, status="processing")
    p_later = await _piece(conn, atom=atom, at=after)
    snapshot = await conn.fetch("SELECT id, status::text, aa_name FROM silver_aa_internal.generated_content "
                                "WHERE tour_id = $1 ORDER BY id", tour)

    change = sf.Change(source_kind=sf.RAW_TOUR, reason="raw_tour_edited", changed_at=now, tour_id=str(tour),
                       fields=("src_summary",))
    out = await sf.fan_out(conn, change, job_id=str(uuid.uuid4()))
    assert out["llm_calls"] == 0
    assert out["unchanged"] == [{"object_kind": "s1", "object_id": str(v1)}]
    marked = {(m["object_kind"], m["object_id"]) for m in out["marked"]}
    assert marked == {("s1", str(v2)), ("t2", str(t2_new)), ("t9", str(p_req)), ("t9", str(p_moment))}
    for oid in (v_old, t2_old, t2_rejected_tenant, p_processing, p_later):
        assert str(oid) not in {m[1] for m in marked}
    rows = await _marks(conn, tour_id=tour)
    assert len(rows) == 4 and all(r["reason"] == "raw_tour_edited" and r["hits"] == 1 and r["open"] for r in rows)
    details = await conn.fetchval("SELECT details FROM shared.source_change_mark WHERE object_id = $1", p_req)
    assert json.loads(details) == {"atom_ids": [atom], "fields": ["src_summary"]}      # ids and field names only

    # the same source again: one open mark per object, hits 2
    again = await sf.fan_out(conn, change)
    assert again["marked"] == [] and len(again["bumped"]) == 4
    assert {r["hits"] for r in await _marks(conn, tour_id=tour)} == {2}
    # a cleared mark lets a new one open
    await conn.execute("UPDATE shared.source_change_mark SET cleared_at = now(), cleared_by = 'test' "
                       "WHERE object_id = $1", v2)
    third = await sf.fan_out(conn, change)
    assert ("s1", str(v2)) in {(m["object_kind"], m["object_id"]) for m in third["marked"]}

    # nothing a writer stored was touched
    assert snapshot == await conn.fetch("SELECT id, status::text, aa_name FROM silver_aa_internal.generated_content "
                                        "WHERE tour_id = $1 ORDER BY id", tour)
    assert await conn.fetchval("SELECT status FROM acp_shared.content_piece WHERE piece_id = $1", p_req) == "approved"

    view = await sf.read_tour(conn, str(tour))
    assert view["open"] == 4 and view["cleared_recent"] == 1
    assert view["by_object_kind"] == {"s1": 1, "t2": 1, "t9": 2}


@pytest.mark.asyncio
async def test_a_master_publish_re_checks_the_tenant_versions_written_before_it_and_not_s1(conn):
    now = await conn.fetchval("SELECT now() AS ok")
    before = now - timedelta(days=1)
    tour = await _tour(conn)
    v1 = await _version(conn, tour, 1, at=before)
    pt = await _master(conn, tour, v1)            # one published_tours row per tour: a re-publish updates it in place
    t2_before = await _ttv(conn, pt, 1, at=before)
    await _ttv(conn, pt, 1, at=now + timedelta(minutes=5), tenant=uuid.uuid4())   # written from the new Master
    change = sf.Change(source_kind=sf.MASTER, reason="master_published", changed_at=now, tour_id=str(tour),
                       source_ref=str(pt))
    out = await sf.fan_out(conn, change)
    assert {(m["object_kind"], m["object_id"]) for m in out["marked"]} == {("t2", str(t2_before))}
    rows = await _marks(conn, tour_id=tour)
    assert rows == [{"object_kind": "t2", "object_id": str(t2_before), "source_kind": "master",
                     "source_ref": str(pt), "reason": "master_published", "hits": 1, "open": True}]


@pytest.mark.asyncio
async def test_a_place_card_change_reaches_the_pieces_through_the_atom_links(conn):
    now = await conn.fetchval("SELECT now() AS ok")
    tour = await _tour(conn)
    linked, other = await _atom(conn, tour, "l"), await _atom(conn, tour, "o")
    card = uuid.uuid4()
    await conn.execute("INSERT INTO acp_shared.atom_place_card (atom_id, card_id, resolved_at) VALUES ($1, $2, now())",
                       linked, card)
    hit = await _piece(conn, atom=linked, at=now - timedelta(hours=3))
    await _piece(conn, atom=other, at=now - timedelta(hours=3))
    out = await sf.fan_out(conn, sf.Change(source_kind=sf.PLACE_CARD, reason="place_card_facts_edited",
                                           changed_at=now, source_ref=str(card)))
    assert [(m["object_kind"], m["object_id"]) for m in out["marked"]] == [("t9", str(hit))]
    rows = await _marks(conn, object_id=hit)
    assert rows[0]["source_kind"] == "place_card" and rows[0]["source_ref"] == str(card)
    # an atom edit reaches only the pieces citing that atom
    out = await sf.fan_out(conn, sf.Change(source_kind=sf.ATOM, reason="atom_edited", changed_at=now,
                                           tour_id=str(tour), source_ref=other))
    assert len(out["marked"]) == 1 and out["marked"][0]["object_id"] != str(hit)


@pytest.mark.asyncio
async def test_the_mark_table_refuses_an_unknown_reason_and_a_mark_newer_than_its_change(conn):
    base = ("t9", uuid.uuid4(), uuid.uuid4(), "atom", "a1")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shared.source_change_mark (object_kind, object_id, tour_id, source_kind, source_ref, "
                "reason, changed_at, object_created_at) VALUES ($1, $2, $3, $4, $5, 'made_up', now(), now())", *base)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shared.source_change_mark (object_kind, object_id, tour_id, source_kind, source_ref, "
                "reason, changed_at, object_created_at) VALUES ($1, $2, $3, $4, $5, 'atom_edited', now(), "
                "now() + interval '1 hour')", *base)


@pytest.mark.asyncio
async def test_cis_app_may_read_and_write_the_marks(conn):
    assert await conn.fetchval(
        "SELECT has_table_privilege('cis_app', 'shared.source_change_mark', 'SELECT, INSERT, UPDATE, DELETE') AS ok")
