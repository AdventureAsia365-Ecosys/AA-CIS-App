"""AA-986 L1 — one "tour is served" rule: live Master AND live source.

Unit level (no DB):
  * shared/tour_liveness.py fragments are the predicates CIS 271 writes into acp_contract.v_active_tour_atoms, and
    the two former hand copies (subject_seed._MEMBERS_SQL, route_guard._TRIP_MEMBER_SQL) use them;
  * migration 271 hygiene (one BEGIN/COMMIT, self-registers, explicit column list, the Infra copy when present);
  * process_export refuses a trashed / superseded source before any write;
  * approve_review returns 409 source_not_live for a pending review whose source is not live, and keeps the old
    answers (re-validation gate, already processed) otherwise;
  * _enqueue_review files a dead-source review as 'dismissed'; the admin review list hides pending dead-source rows.
The behaviour on a real database is in tests/integration/test_aa986_migration_271.py.
"""
import asyncio
import os
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared import tour_liveness as tl

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
MIGRATION = os.path.join(ROOT, "api", "migrations", "271_v_active_tour_atoms_live_source.sql")
INFRA_COPY = os.path.join(ROOT, "..", "..", "infra", "AA-CIS-Infra", "db", "migrations", "cis",
                          "271_v_active_tour_atoms_live_source.sql")
TOUR = "33333333-3333-3333-3333-333333333333"
GCID = "22222222-2222-2222-2222-222222222222"
REVIEW_ID = "44444444-4444-4444-4444-444444444444"


def _sql():
    with open(MIGRATION) as f:
        return f.read()


def _view_body():
    body = re.search(r"CREATE OR REPLACE VIEW acp_contract\.v_active_tour_atoms AS(.*?);", _sql(), re.S).group(1)
    return " ".join(body.split())


# ── the rule, in one place ──────────────────────────────────────────────────────────────────────────────────────────

def test_view_uses_the_shared_predicates():
    body = _view_body()
    assert tl.live_master_sql("pt") in body
    assert tl.live_source_sql("rt") in body
    assert "JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id" in body
    assert "LEFT JOIN" not in body                       # inner join: a missing source row is not served
    assert "WHERE NOT ta.deleted AND NOT ta.is_empty_marker" in body


def test_view_keeps_its_24_columns_in_order():
    cols = re.search(r"SELECT (.*?) FROM acp_contract\.tour_atoms ta", _view_body()).group(1)
    names = [c.strip().removeprefix("ta.") for c in cols.split(",")]
    assert names == ["atom_id", "tour_id", "owner_scope", "text", "activity_type", "emotional_hook",
                     "visual_potential", "persona_fit", "season_note", "media", "starred", "deleted", "usage_log",
                     "cooldown_until", "human_seam_notes", "weight", "created_at", "updated_at", "source_hash",
                     "is_empty_marker", "itinerary_day", "place", "action", "evidence"]
    # guard 1 checks the same list against the live view
    assert all(n in _sql().split("expected  text :=")[1].split(";")[0] for n in names)


def test_former_hand_copies_use_the_shared_joins():
    from services.acp_shared import route_guard, subject_seed
    joins = tl.served_tour_joins_sql("ta.tour_id")
    for sql in (subject_seed._MEMBERS_SQL, route_guard._TRIP_MEMBER_SQL):
        assert joins in sql
        assert "LEFT JOIN silver_aa_internal.raw_tours" not in sql


def test_served_joins_are_the_catalog_rule():
    joins = " ".join(tl.served_tour_joins_sql("x.tour_id").split())
    assert joins == ("JOIN gold_aa_internal.published_tours pt ON pt.tour_id = x.tour_id AND "
                     "pt.master_status = 'active' AND pt.deleted_at IS NULL "
                     "JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id AND "
                     "rt.source_status = 'active' AND rt.deleted_at IS NULL")
    assert tl.live_source_exists_sql("rq.tour_id") == (
        "EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours rt_live WHERE rt_live.tour_id = rq.tour_id "
        "AND rt_live.source_status = 'active' AND rt_live.deleted_at IS NULL)")


# ── migration hygiene ───────────────────────────────────────────────────────────────────────────────────────────────

def test_migration_is_one_transaction_and_self_registers():
    sql = _sql()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert code.count("BEGIN;") == 1 and code.rstrip().endswith("COMMIT;")
    assert "VALUES ('271'" in code and "ON CONFLICT (version) DO NOTHING" in code
    assert "SET ROLE" not in code.upper().replace("SET LOCAL LOCK_TIMEOUT", "")
    assert "DROP " not in code.upper()                   # CREATE OR REPLACE only: owner and grants stay
    assert "GRANT " not in code.upper() and "REVOKE " not in code.upper()


def test_infra_copy_is_byte_identical_when_checked_out_side_by_side():
    if not os.path.exists(INFRA_COPY):
        pytest.skip("AA-CIS-Infra copy not next to this checkout (CI checks out one repo)")
    with open(INFRA_COPY) as f:
        assert f.read() == _sql()


# ── source state + message ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state, live, reason", [
    (tl.SourceState(TOUR, True, "active", None), True, "live"),
    (tl.SourceState(TOUR, True, "trashed", datetime(2026, 10, 23, tzinfo=timezone.utc)), False, "trashed"),
    (tl.SourceState(TOUR, True, "superseded", None), False, "superseded"),
    (tl.SourceState(TOUR, True, "active", datetime(2026, 10, 23, tzinfo=timezone.utc)), False, "deleted"),
    (tl.SourceState(TOUR, False), False, "missing"),
])
def test_source_state(state, live, reason):
    assert state.live is live and state.reason == reason


def test_message_starts_with_the_code_and_says_what_to_do():
    msg = tl.source_not_live_message(tl.SourceState(TOUR, True, "superseded", None), "approve this review")
    assert msg.startswith("source_not_live: cannot approve this review")
    assert "superseded" in msg and "newer version" in msg
    msg = tl.source_not_live_message(tl.SourceState(TOUR, True, "trashed", None), "approve this review")
    assert "trashed" in msg and "Restore" in msg
    err = tl.SourceNotLiveError(tl.SourceState(TOUR, True, "trashed", None))
    assert isinstance(err, ValueError) and err.code == "source_not_live" and str(err).startswith("source_not_live")


def test_source_state_reads_the_row():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"source_status": "trashed", "deleted_at": datetime(2026, 10, 1)})
    st = asyncio.run(tl.source_state(conn, TOUR))
    assert st.exists and not st.live and st.reason == "trashed"
    conn.fetchrow = AsyncMock(return_value=None)
    assert asyncio.run(tl.source_state(conn, TOUR)).reason == "missing"


# ── process_export ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _db_url(monkeypatch):
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.delenv("TENANT_SLUG", raising=False)


def _gc_row(source_status, deleted_at=None):
    return {"id": GCID, "tour_id": TOUR, "tenant_id": GCID, "batch_id": None, "aa_name": "T",
            "aa_highlights": "[]", "seo_keywords_used": "[]", "og_tags": "{}", "quality_score_id": None,
            "quality_score": 9.0, "country": "Vietnam", "duration": "5 days",
            "src_source_status": source_status, "src_deleted_at": deleted_at}


@pytest.mark.parametrize("status, deleted_at", [("trashed", datetime(2026, 10, 23)), ("superseded", None),
                                                ("active", datetime(2026, 10, 23))])
def test_process_export_refuses_a_dead_source_before_any_write(_db_url, status, deleted_at):
    from services.export import handler
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_gc_row(status, deleted_at)])
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()) as atomize:
        with pytest.raises(tl.SourceNotLiveError):
            asyncio.run(handler.process_export(GCID))
    assert conn.fetchrow.await_count == 1                # no INSERT INTO published_tours
    conn.execute.assert_not_called()                     # no pipeline_status / review_queue write
    atomize.assert_not_called()
    conn.close.assert_awaited_once()
    sql = conn.fetchrow.await_args_list[0].args[0]
    assert "rt.source_status::text AS src_source_status" in sql and "rt.deleted_at AS src_deleted_at" in sql


def test_process_export_publishes_a_live_source(_db_url):
    from services.export import handler
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_gc_row("active"), {"id": GCID}])
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()):
        out = asyncio.run(handler.process_export(GCID))
    assert out["status"] == "exported"
    insert = conn.fetchrow.await_args_list[1].args
    assert "INSERT INTO gold_aa_internal.published_tours" in insert[0]


# ── approve_review ──────────────────────────────────────────────────────────────────────────────────────────────────

def _request(conn):
    pool = MagicMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acq
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _approve(conn):
    from api.routers import v1_pipeline
    with patch("services.export.handler.process_export", new=AsyncMock()) as export:
        try:
            return asyncio.run(v1_pipeline.approve_review(REVIEW_ID, _request(conn), None)), export
        except HTTPException as e:
            return e, export


@pytest.mark.parametrize("status", ["trashed", "superseded"])
def test_approve_on_a_dead_source_is_409_source_not_live(status):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        None,                                            # claim: the live-source condition fails
        {"tour_id": TOUR, "blocked": False},             # still pending
        {"source_status": status, "deleted_at": None},   # source_state
    ])
    out, export = _approve(conn)
    assert isinstance(out, HTTPException) and out.status_code == 409
    assert out.detail.startswith("source_not_live: cannot approve this review")
    assert out.headers == {"X-Error-Code": "source_not_live"}
    export.assert_not_called()
    conn.execute.assert_not_called()                     # generated_content not flipped to approved
    claim = conn.fetchrow.await_args_list[0].args[0]
    assert tl.live_source_exists_sql("rq.tour_id") in claim


def test_approve_keeps_the_revalidation_answer_on_a_live_source():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        None, {"tour_id": TOUR, "blocked": True}, {"source_status": "active", "deleted_at": None}])
    out, _ = _approve(conn)
    assert out.status_code == 409 and "re-validation" in out.detail


def test_approve_already_processed_is_unchanged():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    out, _ = _approve(conn)
    assert out.status_code == 409 and out.detail == "Review already processed or not found"


def test_approve_on_a_live_source_exports():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[{"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    out, export = _approve(conn)
    assert out["status"] == "approved" and out["exported"] is True
    export.assert_awaited_once_with(GCID)


def test_approve_survives_a_trash_between_claim_and_export():
    from api.routers import v1_pipeline
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[{"generated_content_id": GCID, "step_fn_task_token": None}])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    err = tl.SourceNotLiveError(tl.SourceState(TOUR, True, "trashed", None))
    with patch("services.export.handler.process_export", new=AsyncMock(side_effect=err)):
        out = asyncio.run(v1_pipeline.approve_review(REVIEW_ID, _request(conn), None))
    assert out["status"] == "approved" and out["exported"] is False


# ── review queue in and out ─────────────────────────────────────────────────────────────────────────────────────────

def test_enqueue_review_dismisses_a_dead_source():
    from api.routers import admin_pipeline
    conn = AsyncMock()
    asyncio.run(admin_pipeline._enqueue_review(conn, TOUR, GCID, {"quality_score": 4.0}))
    insert = conn.execute.await_args_list[0].args[0]
    assert "WHEN pub.live OR NOT pub.source_live THEN 'dismissed'" in insert
    assert tl.live_source_exists_sql("$1::uuid") + " AS source_live" in insert


def test_admin_review_list_hides_pending_dead_sources_and_flags_closed_rows():
    import inspect
    from api.routers import admin_pipeline, v1_pipeline
    src = inspect.getsource(admin_pipeline.admin_review_queue)
    assert "(rq.review_status <> 'pending' OR ({live_source_sql('rt')}))" in src
    assert '"source_live":' in src
    v1 = inspect.getsource(v1_pipeline.get_review_queue)
    assert '{live_source_sql("rt")}' in v1 and '{live_source_exists_sql("rq.tour_id")}' in v1
