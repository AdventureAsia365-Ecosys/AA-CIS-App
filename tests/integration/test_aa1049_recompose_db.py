"""AA-1049 path A-new on real Postgres (conftest replays every migration; each test runs in one transaction that is
rolled back): scripts/quality/recompose_masters.py with services/content_generation/s1_recompose_store.py.

* DRY RUN then APPLY on an open wave: a new approved version (only the itinerary's fact lines changed, the narrative
  byte for byte, ``metadata.recompose_of`` / ``writer``, the score row copied, the final contract recorded under exit
  ``recompose``) published through ``process_export``: the Master serves it, an ``a3_atomize`` job of the new version
  is queued by ``system:recompose``; a re-run after the job is a no-op;
* APPLY refusals: no wave, a paused wave, a tour outside the wave, a pin that moved: nothing written;
* skips: a pending review, a newer pending version;
* resume: a version written whose publish failed is published by the next run, never written twice.

process_export opens its own connection: it gets the test connection (never closed). No model call ($0).
Invented tours and places only (the country is a real one: raw_tours.country must be a listed country name).
"""
import importlib.util
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

import services.jobs.a3_atomize_job  # noqa: E402,F401  (registers the a3_atomize kind process_export enqueues)

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality" / "recompose_masters.py"

SRC_ITIN = ("Day 1: Arrive in Velmora and walk along the canal to the guesthouse.\nOvernight: Velmora\nMeals: D\n"
            "Day 2: Walk up Corran Ridge to the chapel and back to the station.\nMeals: B")
DAY1 = "Arrive in Velmora and walk along the canal to the guesthouse."
DAY2 = "Walk up Corran Ridge to the chapel, then return to the station."
PAGE_ITIN = (f"Day 1 — Arrive in Velmora\n{DAY1}\nOvernight: Velmora. Meals: dinner.\n\n"
             f"Day 2 — Corran Ridge walk\n{DAY2}\nMeals: breakfast.")
META = ("A two-day walking break in Velmora, Vietnam, with a canal walk to the guesthouse and a climb up "
        "Corran Ridge to the chapel before the station.")


class _NoClose:
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


def _script():
    spec = importlib.util.spec_from_file_location("recompose_masters", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _no_recompute(monkeypatch):
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", AsyncMock())


async def _master(conn) -> tuple[str, str]:
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries, source_status, source_group_id, source_version) "
        "VALUES ($1, $2, $3, 'Velmora Ridge 2D', 'Vietnam', '2 days', $4, 'active', $5, 1)",
        tour_id, MASTER, BATCH_ID, SRC_ITIN, uuid.uuid4())
    meta = {"writer_version": "v2", "grounding": {"found": 0, "violations": []},
            "final_contract": {"verdict": "publish"}, "s1_job_id": str(uuid.uuid4())}
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_subtitle, "
        "aa_summary, aa_highlights, aa_itineraries, seo_title, seo_meta, status, metadata, model_editorial) "
        "VALUES ($1, $2, 1, 'Velmora Canal and Corran Ridge Walk', 'Two easy walking days from Velmora.', "
        "'A two-day walking break in Velmora with a climb up Corran Ridge to the chapel.', "
        "'[\"Canal walk in Velmora\", \"Corran Ridge chapel\", \"Guesthouse night by the canal\"]'::jsonb, $3, "
        "'Velmora Canal and Corran Ridge Walk, 2 Days', $4, 'approved', $5::jsonb, 'invented-model') RETURNING id",
        tour_id, MASTER, PAGE_ITIN, META, json.dumps(meta))
    await conn.execute(
        "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
        "failure_codes, brand_audit_status, brand_audit_codes, brand_audit_issues, passed_count, failed_count) "
        "VALUES ($1, $2, $3, 9.5, '[]', 'pass', '[]', '[]', 4, 0)", gc, tour_id, MASTER)
    await conn.execute(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_itineraries, master_status) VALUES ($1, $2, $3, 'Velmora Canal and Corran Ridge Walk', $4, 'active')",
        tour_id, gc, MASTER, PAGE_ITIN)
    return str(tour_id), str(gc)


async def _wave(conn, ids: list[str], *, paused: bool = False) -> str:
    return str(await conn.fetchval(
        "INSERT INTO shared.rerun_wave (name, expected_tour_ids, status, created_by, paused_at, paused_by) "
        "VALUES ('aa1049 recompose test', $1::uuid[], 'open', 'test', "
        "CASE WHEN $2 THEN now() END, CASE WHEN $2 THEN 'test' END) RETURNING id", ids, paused))


async def _run(conn, env: dict) -> tuple[int, list[str]]:
    from services.export import handler
    out: list[str] = []
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=_NoClose(conn))):
        code = await _script().run(_OneConnPool(conn), env, out=out.append)
    return code, out


def _pin(out: list[str]) -> str:
    return next(ln for ln in out if ln.startswith("PIN=")).removeprefix("PIN=")


@pytest.mark.asyncio
async def test_dry_run_then_apply_publishes_a_recomposed_version_and_a_re_run_is_a_no_op(conn):
    tour, old = await _master(conn)
    wave = await _wave(conn, [tour])
    code, out = await _run(conn, {"WAVE_ID": wave})
    assert code == 0 and "recompose=1" in out[1], out
    assert await conn.fetchval("SELECT count(*) FROM silver_aa_internal.generated_content WHERE tour_id = $1",
                               uuid.UUID(tour)) == 1          # DRY RUN wrote nothing

    code, out = await _run(conn, {"WAVE_ID": wave, "APPLY": "1", "PIN": _pin(out)})
    assert code == 0, out
    assert out[-1] == "applied: published=1"
    new = await conn.fetchrow(
        "SELECT g.*, pt.generated_content_id AS served FROM silver_aa_internal.generated_content g "
        "JOIN gold_aa_internal.published_tours pt ON pt.tour_id = g.tour_id "
        "WHERE g.tour_id = $1 AND g.version_num = 2", uuid.UUID(tour))
    assert new is not None and str(new["served"]) == str(new["id"]) and new["status"] == "approved"
    assert new["aa_itineraries"] == (f"Day 1 — Arrive in Velmora\n{DAY1}\nOvernight: Velmora\nMeals: D\n\n"
                                     f"Day 2 — Corran Ridge walk\n{DAY2}\nMeals: B")
    assert new["aa_name"] == "Velmora Canal and Corran Ridge Walk" and new["seo_meta"] == META
    md = json.loads(new["metadata"])
    assert md["recompose_of"] == old and md["writer"] == "recompose-v1" and md["writer_version"] == "v2"
    assert "s1_job_id" not in md and md["recompose"]["fields"] == ["fact_lines"]
    assert [e["exit"] for e in md["final_contract_log"]] == ["recompose", "recompose"]
    assert md["final_contract"]["verdict"] == "publish"
    served_text = await conn.fetchval("SELECT aa_itineraries FROM gold_aa_internal.published_tours WHERE tour_id = $1",
                                      uuid.UUID(tour))
    assert served_text == new["aa_itineraries"]
    qs = await conn.fetchrow("SELECT validator_fn_version, score_overall::float AS score FROM "
                             "silver_aa_internal.quality_scores WHERE generated_content_id = $1", new["id"])
    assert qs["validator_fn_version"] == "recompose-v1" and qs["score"] == 9.5
    job = await conn.fetchrow("SELECT id, created_by, payload->>'version_id' AS version_id FROM shared.job "
                              "WHERE kind = 'a3_atomize' AND payload->>'tour_id' = $1", tour)
    assert job["created_by"] == "system:recompose" and job["version_id"] == str(new["id"])

    # the A3 job ran: the next run finds nothing to change
    await conn.execute("UPDATE shared.job SET status = 'succeeded' WHERE id = $1", job["id"])
    code, out = await _run(conn, {"WAVE_ID": wave})
    assert code == 0 and "no_op=1" in out[1] and _pin(out).startswith("0:"), out


@pytest.mark.asyncio
async def test_apply_is_refused_without_an_open_wave_or_with_a_moved_pin(conn):
    tour, _old = await _master(conn)
    other, _ = await _master(conn)
    wave = await _wave(conn, [tour])
    paused = await _wave(conn, [tour], paused=True)
    _, out = await _run(conn, {"WAVE_ID": wave})
    pin = _pin(out)
    for env, code_want, why in (
        ({"APPLY": "1", "PIN": pin, "TOUR_IDS": tour}, 2, "needs WAVE_ID"),
        ({"APPLY": "1", "PIN": pin, "WAVE_ID": paused}, 2, "paused"),
        ({"APPLY": "1", "PIN": pin, "WAVE_ID": wave, "TOUR_IDS": other}, 2, "outside the wave"),
        ({"APPLY": "1", "PIN": "1:0000", "WAVE_ID": wave}, 3, "PIN mismatch"),
    ):
        code, out = await _run(conn, env)
        assert code == code_want and why in out[-1], (env, out)
    assert await conn.fetchval("SELECT count(*) FROM silver_aa_internal.generated_content WHERE tour_id = ANY($1)",
                               [uuid.UUID(tour), uuid.UUID(other)]) == 2
    await conn.execute("UPDATE shared.rerun_wave SET status = 'closed', closed_at = now() WHERE id = $1",
                       uuid.UUID(wave))
    code, out = await _run(conn, {"APPLY": "1", "PIN": pin, "WAVE_ID": wave})
    assert code == 2 and "not open" in out[-1]


@pytest.mark.asyncio
async def test_a_pending_review_and_a_newer_pending_version_are_skipped(conn):
    reviewed, served = await _master(conn)
    drafted, _ = await _master(conn)
    await conn.execute(
        "INSERT INTO silver_aa_internal.review_queue (tour_id, generated_content_id, tenant_id, review_status) "
        "VALUES ($1, $2, $3, 'pending')", uuid.UUID(reviewed), uuid.UUID(served), MASTER)
    await conn.execute(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, status) "
        "VALUES ($1, $2, 2, 'A newer draft', 'pending')", uuid.UUID(drafted), MASTER)
    code, out = await _run(conn, {"TOUR_IDS": f"{reviewed},{drafted}"})
    assert code == 0 and "newer_version=1" in out[1] and "pending_review=1" in out[1], out


@pytest.mark.asyncio
async def test_a_version_written_whose_publish_failed_is_resumed_not_written_twice(conn):
    from services.content_generation import s1_recompose_store as st
    tour, old = await _master(conn)
    wave = await _wave(conn, [tour])
    _, out = await _run(conn, {"WAVE_ID": wave})
    failing = AsyncMock(side_effect=RuntimeError("publish down"))
    code = await _script().run(_OneConnPool(conn), {"WAVE_ID": wave, "APPLY": "1", "PIN": _pin(out)},
                               publish=failing, out=out.append)
    assert code == 1 and "publish_failed=1" in out[-1]
    written = await conn.fetchval("SELECT id FROM silver_aa_internal.generated_content "
                                  "WHERE tour_id = $1 AND version_num = 2", uuid.UUID(tour))
    served = await conn.fetchval("SELECT generated_content_id FROM gold_aa_internal.published_tours "
                                 "WHERE tour_id = $1", uuid.UUID(tour))
    assert written is not None and str(served) == old

    code, out = await _run(conn, {"WAVE_ID": wave})
    assert "resume=1" in out[1], out
    code, out = await _run(conn, {"WAVE_ID": wave, "APPLY": "1", "PIN": _pin(out)})
    assert code == 0 and out[-1] == "applied: published=1", out
    assert await conn.fetchval("SELECT count(*) FROM silver_aa_internal.generated_content WHERE tour_id = $1",
                               uuid.UUID(tour)) == 2
    assert str(await conn.fetchval("SELECT generated_content_id FROM gold_aa_internal.published_tours "
                                   "WHERE tour_id = $1", uuid.UUID(tour))) == str(written)
    assert st.RESUME == "resume"


@pytest.mark.asyncio
async def test_every_query_prepares_on_the_live_schema(conn):
    from services.content_generation import s1_recompose_store as st
    await conn.prepare(st.candidates_sql())
    await conn.prepare(st.WAVE_SQL)
    await conn.prepare(st.SERVED_SQL)
    await conn.prepare(st.INSERT_VERSION_SQL)
    await conn.prepare(st.COPY_SCORE_SQL)
