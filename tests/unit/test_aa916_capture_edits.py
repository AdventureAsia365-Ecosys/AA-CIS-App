"""AA-916 — capture on the edit paths: WM-04 (tenant T9 edit), WM-05 (admin A1 field edit), WM-06 (reason codes on
the admin actions, fact_not_in_source -> Jev truth=false for a T2 sentence), WM-08 (every discarded T9 attempt and the
writer prompt version)."""
import hashlib
import json
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from structlog.testing import capture_logs

from api.core.aa544_tenant_pool import PIECE_EDIT_FLAG
from api.routers import admin_content_quality as cq
from api.routers import admin_pipeline as ap
from services.acp_content_writing import service
from services.writing_memory import signals as wm
from tests.unit.test_aa450_content_writing_service import (  # noqa: F401 — _no_cta_insert is an autouse fixture
    PIECE_ID as W_PIECE_ID, REQUEST_ID, _context, _failing_outcome, _finalized_row, _no_cta_insert,
    _passing_outcome,
)
from tests.unit.test_aa817_fail_closed_routes import PIECE_ID, TENANT_ID, _conn, _flag_only, _piece_row, _request

SECRET = "s3cret"
SID = uuid.UUID(int=916)
TEXT = "Edited by tenant"
TENANT_SAFE_KEYS = {"piece_id", "angle_gate_request_id", "channel", "ready_state", "content_text", "seo_title",
                    "meta_description", "slug", "route_hub_name", "route_segment_count", "created_at"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _signal_calls(conn):
    return [c for c in conn.fetchval.await_args_list if "INSERT INTO shared.writing_signal" in c.args[0]]


def _with_signal_insert(conn):
    """The fail-closed helpers' fetchval echoes the GUC (sql, value); add the signal INSERT … RETURNING."""
    echo = conn.fetchval.side_effect

    async def fetchval(sql, *args):
        if "INSERT INTO shared.writing_signal" in sql:
            return SID
        return echo(sql, *args)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    return conn


# ── WM-04: the tenant's hand edit ────────────────────────────────────────────────────────────────

async def _edit(request, text=TEXT):
    return await service.update_piece_content_text(TENANT_ID, PIECE_ID, text, request.app.state.pool,
                                                   request=request)


@pytest.mark.asyncio
async def test_tenant_edit_writes_one_signal_inside_the_tenant_transaction():
    order = []
    tenant = _with_signal_insert(_conn(fetchrow={**_piece_row(), "before_text": "Old body"}))
    inner = tenant.fetchval.side_effect

    async def fetchval(sql, *args):
        if "writing_signal" in sql:
            order.append("signal")
        return await inner(sql, *args)
    tenant.fetchval.side_effect = fetchval
    tenant.tx.__aexit__ = AsyncMock(side_effect=lambda *a: order.append("tx_exit") or False)
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = _flag_only(PIECE_EDIT_FLAG)
    request.app.state.pool.execute = AsyncMock(side_effect=lambda *a: order.append("audit") or "INSERT 0 1")
    out = await _edit(request)
    # savepoint (nested tx) closes, then the tenant transaction commits, then the audit on MAIN
    assert order == ["signal", "tx_exit", "tx_exit", "audit"]
    [call] = _signal_calls(tenant)
    args = call.args[1:]
    assert args[:5] == ("t9_blog", "blog", "tenant", str(TENANT_ID), "content_text")
    assert args[6:8] == ("Old body", TEXT) and args[12:15] == ("tenant_edit", "tenant", f"tenant:{TENANT_ID}")
    assert args[17:] == ("content_piece", str(PIECE_ID), None)
    audit_sql, *params = request.app.state.pool.execute.await_args.args
    assert "INSERT INTO acp_shared.audit_log" in audit_sql
    details = json.loads(params[5])
    assert details == {"content_length": len(TEXT), "signal_id": str(SID), "before_sha256": _sha("Old body"),
                       "after_sha256": _sha(TEXT)}                       # no text in the audit row (ADR 0010 §11)
    assert set(out) == TENANT_SAFE_KEYS and "before_text" not in out     # the tenant contract is unchanged


@pytest.mark.asyncio
async def test_tenant_edit_signal_failure_never_fails_the_edit():
    tenant = _conn(fetchrow={**_piece_row(), "before_text": "Old body"})
    tenant.fetchval = AsyncMock(side_effect=lambda sql, *a: (_ for _ in ()).throw(RuntimeError("no table"))
                                if "writing_signal" in sql else a[0])
    request = _request(flag=True, tenant_conn=tenant)
    with capture_logs() as logs:
        out = await _edit(request)
    assert out["content_text"] == "Body"
    assert [e for e in logs if e["event"] == "aa916_signal_failed"]
    details = json.loads(request.app.state.pool.execute.await_args.args[6])
    assert details == {"content_length": len(TEXT)}                      # no signal id when none was written


@pytest.mark.asyncio
async def test_unchanged_text_writes_no_signal_and_flag_off_writes_on_the_admin_conn():
    same = _with_signal_insert(_conn(fetchrow={**_piece_row(), "before_text": TEXT}))
    await _edit(_request(flag=True, tenant_conn=same))
    assert _signal_calls(same) == []
    main = _with_signal_insert(_conn(fetchrow={**_piece_row(), "before_text": "Old body"}))
    request = _request(flag=False, main_conn=main)
    await _edit(request)
    assert len(_signal_calls(main)) == 1
    [audit] = [c for c in main.execute.await_args_list if "acp_shared.audit_log" in c.args[0]]
    assert json.loads(audit.args[6])["signal_id"] == str(SID)


@pytest.mark.asyncio
async def test_master_tenant_edit_is_a_platform_signal_written_on_main_after_the_tenant_transaction():
    # review round 1: the AA master tenant's text is platform scope everywhere (scope_for_tenant); aa_app_user may
    # never insert a platform row, so the tenant pool skips it and the main pool writes it after the commit.
    master = uuid.UUID(wm.PLATFORM_TENANT_ID)
    tenant = _with_signal_insert(_conn(fetchrow={**_piece_row(), "tenant_id": master, "before_text": "Old body"}))
    main = _with_signal_insert(_conn())
    request = _request(flag=True, tenant_conn=tenant, main_conn=main)
    request.app.state.redis.get = _flag_only(PIECE_EDIT_FLAG)
    await service.update_piece_content_text(master, PIECE_ID, TEXT, request.app.state.pool, request=request)
    assert _signal_calls(tenant) == []
    [call] = _signal_calls(main)
    args = call.args[1:]
    assert args[:4] == ("t9_blog", "blog", "platform", None) and args[6:8] == ("Old body", TEXT)
    audit = request.app.state.pool.execute.await_args.args
    assert json.loads(audit[6])["signal_id"] == str(SID)
    # flag off (admin conn): written in place, platform scope as well
    off = _with_signal_insert(_conn(fetchrow={**_piece_row(), "tenant_id": master, "before_text": "Old body"}))
    req_off = _request(flag=False, main_conn=off)
    await service.update_piece_content_text(master, PIECE_ID, TEXT, req_off.app.state.pool, request=req_off)
    [call] = _signal_calls(off)
    assert call.args[3:5] == ("platform", None)


@pytest.mark.asyncio
async def test_master_tenant_main_pool_failure_never_fails_the_edit():
    master = uuid.UUID(wm.PLATFORM_TENANT_ID)
    tenant = _conn(fetchrow={**_piece_row(), "tenant_id": master, "before_text": "Old body"})
    request = _request(flag=True, tenant_conn=tenant)
    request.app.state.redis.get = _flag_only(PIECE_EDIT_FLAG)
    request.app.state.pool.acquire = MagicMock(side_effect=RuntimeError("pool closed"))
    with capture_logs() as logs:
        out = await service.update_piece_content_text(master, PIECE_ID, TEXT, request.app.state.pool,
                                                      request=request)
    assert out["content_text"] == "Body" and [e for e in logs if e["event"] == "aa916_signal_failed"]


def test_piece_edit_sql_reads_the_text_before_the_edit():
    sql = service._PIECE_EDIT_SQL
    assert sql.count("piece_id = $1 AND tenant_id = $2 AND status IN ('approved', 'held')") == 2
    assert "FOR UPDATE" in sql and "prev.before_text" in sql


# ── WM-08: every discarded attempt + the prompt version ─────────────────────────────────────────

def _items(text, ledger):
    return None if text.startswith("attempt 1") else [{"sentence": "x", "problem": "p", "gate": "F1_grounding",
                                                       "start": 0, "end": 1}]


@pytest.mark.asyncio
async def test_three_attempts_keep_attempts_one_and_two():
    with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
         patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
         patch.object(service.sentence_repair, "plan", side_effect=_items), \
         patch.object(service.sentence_repair, "neighbours", return_value=""), \
         patch.object(service, "_repair_source", return_value="src"), \
         patch.object(service, "repair_sentences", return_value=(["y"], 0.01)), \
         patch.object(service.sentence_repair, "splice_checked", return_value="attempt 3 text"), \
         patch.object(service, "run_quality_gates",
                      side_effect=[_failing_outcome(), _failing_outcome(), _passing_outcome()]), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as fin:
        await service.run_write_background(REQUEST_ID, W_PIECE_ID, _context(), pool=MagicMock())
    kw = fin.call_args.kwargs
    assert kw["content_text"] == "attempt 3 text" and kw["attempt_number"] == 3
    got = [(d["attempt_number"], d["content_text"]) for d in kw["discarded_attempts"]]
    assert got == [(1, "attempt 1 text"), (2, "attempt 2 text")]
    assert [d["ledger_index"] for d in kw["discarded_attempts"]] == [1, 2]
    assert re.fullmatch(r"[0-9a-f]{8}", kw["writer_input"]["writer_prompt_version"])


@pytest.mark.asyncio
async def test_revert_then_repair_lists_each_attempt_once_never_the_final():
    """Attempt 2 regresses and is reverted to attempt 1; attempt 3 repairs attempt 1's text. Discarded: the
    regressed attempt 2 and attempt 1 (replaced by the repair), each once."""
    a1 = {"passed": False, "first_failure": None, "flags": [],
          "gate_ledger": [{"gate": "F4_extreme_length", "passed": False, "violations": ["v"], "repairable": True,
                           "blocking": True}]}
    a1["first_failure"] = a1["gate_ledger"][0]
    a2 = {**a1, "gate_ledger": a1["gate_ledger"] + [{"gate": "F1_grounding", "passed": False, "violations": ["v"],
                                                     "repairable": True, "blocking": True}]}

    def items(text, ledger):
        return [{"sentence": "x", "problem": "p", "gate": "F4_extreme_length", "start": 0, "end": 1}] \
            if text == "attempt 1 text" and len(ledger) == 1 and ledger[0]["gate"] == "F4_extreme_length" \
            and items.calls_after_revert else None
    items.calls_after_revert = False
    real_keep = service._keep_discarded

    def keep(discarded, snap, current, idx):
        if snap and snap.get("attempt_number") == 2:
            items.calls_after_revert = True                          # the revert just happened
        return real_keep(discarded, snap, current, idx)
    with patch.object(service, "write_content", return_value=("attempt 1 text", 0.02, {}, None)), \
         patch.object(service, "rewrite_with_feedback", return_value=("attempt 2 text", 0.02, {}, None)), \
         patch.object(service.sentence_repair, "plan", side_effect=items), \
         patch.object(service, "_keep_discarded", side_effect=keep), \
         patch.object(service.sentence_repair, "neighbours", return_value=""), \
         patch.object(service, "_repair_source", return_value="src"), \
         patch.object(service, "repair_sentences", return_value=(["y"], 0.01)), \
         patch.object(service.sentence_repair, "splice_checked", return_value="attempt 3 text"), \
         patch.object(service, "run_quality_gates", side_effect=[a1, a2, _passing_outcome()]), \
         patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized_row())) as fin:
        await service.run_write_background(REQUEST_ID, W_PIECE_ID, _context(), pool=MagicMock())
    kw = fin.call_args.kwargs
    assert kw["content_text"] == "attempt 3 text"
    got = [(d["attempt_number"], d["content_text"]) for d in kw["discarded_attempts"]]
    assert got == [(1, "attempt 1 text"), (2, "attempt 2 text")]


def test_keep_discarded_dedupes_and_skips_the_current_text():
    out = []
    service._keep_discarded(out, {"attempt_number": 1, "content_text": "a", "gate_ledger": []}, "b", 1)
    service._keep_discarded(out, {"attempt_number": 1, "content_text": "a", "gate_ledger": []}, "c", 2)
    service._keep_discarded(out, {"attempt_number": 2, "content_text": "c", "gate_ledger": []}, "c", 2)
    service._keep_discarded(out, None, "c", 2)
    assert [(d["attempt_number"], d["ledger_index"]) for d in out] == [(1, 1)]
    final = service._final_discarded(out + [{"attempt_number": 3, "ledger_index": 3, "content_text": "[R:x] t",
                                             "gate_ledger": []}], 3)
    assert [d["attempt_number"] for d in final] == [1]


def test_writer_prompt_version_is_the_a1_formula():
    from services.acp_content_writing.prompts import SYSTEM_PROMPT
    assert service.WRITER_PROMPT_VERSION == hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    assert service.writer_input_snapshot({})["writer_prompt_version"] == service.WRITER_PROMPT_VERSION


def test_tenant_projection_still_hides_discarded_attempts_and_writer_input():
    row = {**_finalized_row(), "discarded_attempts": [{"attempt_number": 1}], "writer_input": {"x": 1}}
    out = service._tenant_safe_piece(row)
    assert "discarded_attempts" not in out and "writer_input" not in out


# ── WM-05: the admin's A1 field edit ─────────────────────────────────────────────────────────────

class _GcConn:
    def __init__(self, before):
        self.before, self.signals, self.updates = before, [], []
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        self.transaction = MagicMock(return_value=tx)

    async def fetchrow(self, sql, *args):
        assert "FOR UPDATE" in sql
        return self.before

    async def fetchval(self, sql, *args):
        if "INSERT INTO shared.writing_signal" in sql:
            self.signals.append(args)
            return uuid.UUID(int=len(self.signals))
        self.updates.append((sql, args))
        return args[-2]


def _gc_request(conn, body):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    req.json = AsyncMock(return_value=body)
    req.headers = {"x-admin-user-id": str(uuid.UUID(int=7)), "x-reviewer-id": "ana"}
    return req


@pytest.mark.asyncio
async def test_a1_field_edit_writes_one_platform_signal_per_changed_field():
    content_id, tour_id = str(uuid.uuid4()), str(uuid.uuid4())
    before = {"tenant_id": uuid.UUID(wm.PLATFORM_TENANT_ID), "aa_summary": "Old summary.", "aa_name": "Same",
              "aa_highlights": json.dumps(["b", "a"]), "seo_keywords_used": "[]"}
    body = {"aa_summary": "New summary.", "aa_name": "Same", "aa_highlights": ["c"], "seo_keywords_used": ["k"]}
    conn = _GcConn(before)
    out = await ap.update_generated_content_fields(tour_id, content_id, _gc_request(conn, body), x_admin_secret=SECRET)
    assert out == {"content_id": content_id, "updated": list(body), "human_edited": True,
                   "revalidate_required": True}                          # the response shape is unchanged
    got = {(a[4], a[6], a[7]) for a in conn.signals}                     # field, before, after
    assert got == {("aa_summary", "Old summary.", "New summary."), ("aa_highlights", '["b", "a"]', '["c"]')}
    for a in conn.signals:
        assert a[:4] == ("a1", "master", "platform", None) and a[12:14] == ("field_edit", "admin")
        assert a[14] == str(uuid.UUID(int=7)) and a[17:] == ("generated_content", content_id, None)
    [(sql, args)] = conn.updates
    assert "human_edited = true" in sql and "revalidate_passed = NULL" in sql
    edit_diff = json.loads(args[-3])
    assert edit_diff["fields"] == list(body) and edit_diff["reviewer_handle"] == "ana"
    assert sorted(edit_diff["signal_ids"]) == sorted(str(uuid.UUID(int=i)) for i in (1, 2))


@pytest.mark.asyncio
async def test_a1_field_edit_of_a_tenant_tour_is_tenant_scope_and_404_writes_nothing():
    tenant = uuid.uuid4()
    conn = _GcConn({"tenant_id": tenant, "aa_summary": "Old."})
    await ap.update_generated_content_fields("t", str(uuid.uuid4()), _gc_request(conn, {"aa_summary": "New."}),
                                             x_admin_secret=SECRET)
    assert conn.signals[0][2:4] == ("tenant", str(tenant))               # tenant text never platform scope
    missing = _GcConn(None)
    with pytest.raises(HTTPException) as e:
        await ap.update_generated_content_fields("t", "c", _gc_request(missing, {"aa_summary": "x"}),
                                                 x_admin_secret=SECRET)
    assert e.value.status_code == 404 and missing.signals == [] and missing.updates == []


# ── WM-06: reason codes ──────────────────────────────────────────────────────────────────────────

def test_an_unknown_reason_code_is_a_validation_error_on_every_route_body():
    base = {"start": 0, "end": 1, "text": "x", "mode": "cut"}
    for model, extra in ((cq.SentenceApplyBody, {}), (cq.TourSentenceApplyBody, {"field": "summary"}),
                         (ap.ReviewReasonBody, None), (ap.TourRunRequest, None)):
        data = ({**base, **extra} if extra is not None
                else {"tour_id": "t", "batch_id": "b", "tenant_id": "x"} if model is ap.TourRunRequest else {})
        with pytest.raises(ValidationError):
            model(**data, reason_code="bogus")
        assert model(**data, reason_code="voice_off_brand").reason_code == "voice_off_brand"
        assert model(**data).reason_code is None                         # omitting it keeps today's behaviour


def _review_pool(row):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetchval = AsyncMock(return_value=SID)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


ADMIN_ID = uuid.UUID(int=9)


@pytest.mark.asyncio
@pytest.mark.parametrize("ttv,writer,header,actor", [
    (None, "a1", str(ADMIN_ID).upper(), str(ADMIN_ID)),       # the BFF's JWT sub, stored canonical
    (uuid.uuid4(), "t2", "x" * 300, "unknown"),                # not an id: never stored as given (size CHECK, no names)
])
async def test_dismiss_with_a_code_writes_a_reason_only_signal(ttv, writer, header, actor):
    review = str(uuid.uuid4())
    pool, conn = _review_pool({"tenant_id": uuid.UUID(wm.PLATFORM_TENANT_ID), "tenant_tour_version_id": ttv})
    req = MagicMock()
    req.app.state.pool = pool
    out = await ap.admin_dismiss_review(review, req, ap.ReviewReasonBody(reason_code="voice_off_brand"),
                                        x_admin_secret=SECRET, x_admin_user_id=header)
    assert out == {"status": "dismissed", "review_id": review}
    [call] = _signal_calls(conn)
    a = call.args[1:]
    assert a[0] == writer and a[2:4] == ("platform", None) and a[6:8] == (None, None)    # no text
    assert a[11:15] == ("voice_off_brand", "dismiss", "admin", actor)
    assert a[17:] == ("review_queue", review, None)


@pytest.mark.asyncio
async def test_reject_without_a_body_writes_no_signal_and_with_one_writes_after_the_reject():
    review = str(uuid.uuid4())
    pool, conn = _review_pool({"tenant_id": uuid.UUID(wm.PLATFORM_TENANT_ID), "tenant_tour_version_id": None})
    req = MagicMock()
    req.app.state.pool = pool
    with patch("api.routers.v1_pipeline.reject_review", new=AsyncMock(return_value={"status": "rejected"})) as rj:
        assert await ap.admin_reject_review(review, req, None, x_admin_secret=SECRET) == {"status": "rejected"}
        assert _signal_calls(conn) == []
        await ap.admin_reject_review(review, req, ap.ReviewReasonBody(reason_code="fact_not_in_source"),
                                     x_admin_secret=SECRET, x_admin_user_id="u9")
    assert rj.await_count == 2
    [call] = _signal_calls(conn)
    assert call.args[1] == "a1" and call.args[12:14] == ("fact_not_in_source", "reject")
    assert call.args[16] == "fail"                                       # fact_status


@pytest.mark.asyncio
async def test_regenerate_keeps_the_job_payload_and_signals_only_a_new_job():
    review = str(uuid.uuid4())
    pool, conn = _review_pool({"tenant_id": uuid.UUID(wm.PLATFORM_TENANT_ID), "tenant_tour_version_id": None})
    req = MagicMock()
    req.app.state.pool = pool
    body = ap.TourRunRequest(tour_id="t", batch_id="b", tenant_id="x", review_id=review, reason_code="ai_phrasing")
    with patch("shared.jobs.registry.enqueue", new=AsyncMock(side_effect=[("j1", True), ("j1", False)])) as enq:
        await ap.run_tour_async(body, req, x_admin_secret=SECRET, x_admin_user_id="u9")
        await ap.run_tour_async(body, req, x_admin_secret=SECRET, x_admin_user_id="u9")   # a double click: dedup
    payload = enq.await_args_list[0].args[2]
    assert "review_id" not in payload and "reason_code" not in payload
    [call] = _signal_calls(conn)
    assert call.args[12:14] == ("ai_phrasing", "regenerate")


@pytest.mark.asyncio
async def test_t2_fact_not_in_source_labels_the_t3_jev_question_false():
    from services.acp_produce.tenant_pipeline import T3_JEV_Q
    from services.content_generation.grounding import source_text, subject_key
    tour = {"name": "Laos Loop", "aa_summary": "Walk the old town."}
    ref = wm.SignalRef(str(SID), None, None)
    with patch("shared.llm_client.decide.record_outcome", new=AsyncMock(return_value=1)) as ro:
        rows = await cq._record_fact_not_in_source("POOL", tour, " The trek climbs 1,200 steps. ", ref,
                                                   str(uuid.UUID(int=5)))
    assert rows == 1
    pool, q, key = ro.await_args.args
    assert (pool, q) == ("POOL", T3_JEV_Q) and key == subject_key(source_text(tour), "The trek climbs 1,200 steps.")
    assert ro.await_args.kwargs == {"truth": False, "source": "admin_reason_fact_not_in_source",
                                    "ref": str(SID), "by": str(uuid.UUID(int=5))}
    with patch("shared.llm_client.decide.record_outcome", new=AsyncMock()) as ro2:
        assert await cq._record_fact_not_in_source("POOL", {}, "x", ref, "not-a-uuid") == 0
    ro2.assert_not_awaited()                                             # no source text: nothing to label


# review round 1: the key the admin's fact_not_in_source labels must be the key T3 asked Jev with. T3 splits with
# tenant_pipeline._SENT_SPLIT_RE on the T2 output; the drawer flags spans from hygiene.sentence_spans on the stored
# version. Both read the same source dict (t2_rewrite_job.source_tour_dict).
_T3_TOUR = {"name": "Luang Prabang Loop", "subtitle": "", "summary": "Walk the old town with a local guide.",
            "description": "A gentle walk through temples and markets.", "highlights": "Temples; markets",
            "itineraries": "Day 1: old town walk.", "seo_title": "", "seo_meta": "", "country": "Laos",
            "duration": "3 days"}


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", [
    {"summary": "Walk the old town with a local guide. The trek climbs 4,200 metres to the pass. Evenings are free."},
    {"subtitle": "Temples and markets. A 1,200-step stairway leads to the summit!"},
    {"highlights": ["Temples at dawn.", "Markets by night.", "A 15 km bike ride? Yes, along the Mekong."]},
    {"itineraries": "Day 1: walk the old town. Day 2: the trek climbs 4,200 metres. \"It rains in May.\""},
    {"summary": "Walk the old town.\n\nThe trek climbs 4,200 metres to the pass.\nEvenings are free."},
])
async def test_fact_not_in_source_key_matches_the_key_t3_asked_jev(rc):
    from services.acp_produce.tenant_pipeline import _t3_grounding_check
    from services.content_generation.grounding import source_text, subject_key
    source_texts = [str(v) for v in _T3_TOUR.values() if v]
    violations = _t3_grounding_check(rc, source_texts, background=False)
    assert violations, "the fixture must give T3 a hit"
    t3_keys = {subject_key(source_text(_T3_TOUR), v["sentence"]) for v in violations}
    flagged = cq.t2_flag(cq.t2_sections(rc), _T3_TOUR, [])
    numeric = [f for f in flagged if any("number(s)" in p["problem"] for p in f["problems"])]
    assert numeric, "the drawer must flag the same sentence"
    keys = set()
    for f in numeric:
        text = cq._t2_field_text(rc, f["field"], f["index"])
        with patch("shared.llm_client.decide.record_outcome", new=AsyncMock(return_value=1)) as ro:
            await cq._record_fact_not_in_source("POOL", _T3_TOUR, text[f["start"]:f["end"]],
                                                wm.SignalRef(str(SID), None, None), None)
        keys.add(ro.await_args.args[2])
    assert keys <= t3_keys, (keys, t3_keys)                              # every flagged span labels a T3 question


@pytest.mark.asyncio
async def test_fact_not_in_source_that_labels_nothing_logs_a_warning():
    with patch("shared.llm_client.decide.record_outcome", new=AsyncMock(return_value=0)), capture_logs() as logs:
        rows = await cq._record_fact_not_in_source("POOL", _T3_TOUR, "The trek climbs 4,200 metres.",
                                                   wm.SignalRef(str(SID), None, None), None)
    assert rows == 0
    [warn] = [e for e in logs if e["event"] == "aa916_fact_not_in_source_unlabelled"]
    assert warn["log_level"] == "warning" and warn["signal_id"] == str(SID) and warn["subject_key"].startswith("claim:")


def test_flagged_sentences_carry_the_suggested_reason():
    text = "One. Two sentence here."
    hls = [{"key": "k", "sentence_start": 5, "sentence_end": 23, "sentence": "Two sentence here.",
            "gate": "F12_scope", "problem": "p", "blocking": False},
           {"key": "k", "sentence_start": 5, "sentence_end": 23, "sentence": "Two sentence here.",
            "gate": "F1_grounding", "problem": "q", "blocking": True}]
    [item] = cq.flagged_sentences(text, hls)
    assert item["gates"] == ["F12_scope", "F1_grounding"] and item["suggested_reason"] == "wrong_place_or_time"


def test_flagged_sentences_suggested_reason_follows_the_lexicon_source():
    """AA-924 — an F2_lexicon finding names its legacy source; the chip follows it, not the merged entry."""
    text = "One. Two sentence here."
    span = {"key": "k", "sentence_start": 5, "sentence_end": 23, "sentence": "Two sentence here."}
    banned = [{**span, "gate": "F2_lexicon", "source": "F2_banned_patterns", "problem": "p", "blocking": True}]
    forbidden = [{**span, "gate": "F2_lexicon", "source": "F2_brand_forbidden", "problem": "p", "blocking": True}]
    [item] = cq.flagged_sentences(text, banned)
    assert item["gates"] == ["F2_lexicon"] and item["suggested_reason"] == "ai_phrasing"
    [item] = cq.flagged_sentences(text, forbidden)
    assert item["suggested_reason"] == "voice_off_brand"
