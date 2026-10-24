"""AA-802 — silent correction: an admin fixes a sentence of a held / low-score T2 tour version and the
tenant's unpublished copy changes in place (no new version, no flag), with the version history in
the audit row; and the "tenant has an older copy" indicator for a published / exported T9 piece.
"""
import datetime as dt
import hashlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import _tenant_projection as tp
from api.routers import admin_content_quality as cq
from api.routers import v1_tours
from services.acp_content_writing import generate
from services.acp_shared.audit_log import AdminAuditAction, TenantAuditAction

SECRET = "s3cret"
VERSION_ID = str(uuid.uuid4())
TENANT_ID = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


SUMMARY = ("Walk the old town of Luang Prabang with a local guide. The trek climbs 1,200 steps to the "
           "monastery. Evenings are free by the river.")
BAD = "The trek climbs 1,200 steps to the monastery."
RC = {"name": "Laos Loop", "subtitle": "Temples and rivers of northern Laos",
      "summary": SUMMARY,
      "highlights": ["Sunrise alms giving in Luang Prabang", "A stunning boat ride on the Mekong",
                     "Kuang Si waterfalls swim"],
      "itineraries": "Day 1 — Luang Prabang\nArrive and meet your guide. Walk to the night market.",
      "seo_title": "Laos Loop tour", "seo_meta": "Ten days through northern Laos with a local guide and boat.",
      "trip_type": "culture", "status": "done"}


def _version_row(status="needs_review", rc=RC, version_number=1, latest=1, exported_at=None, tenant_id=TENANT_ID):
    return {"id": uuid.UUID(VERSION_ID), "tenant_id": tenant_id, "published_tour_id": uuid.uuid4(),
            "tenant_name": "Trail Pulse", "tour_name": "Laos Loop", "version_number": version_number,
            "status": status, "quality_score": 6.1, "qa_status": "escalated", "qa_auto_passed": True,
            "qa_repair_count": 2, "qa_checked_at": NOW, "rewritten_content": json.dumps(rc),
            "edit_source": "ai_generated", "created_at": NOW, "job_id": None,
            "latest_version_number": latest, "exported_at": exported_at,
            "aa_name": "Laos Loop", "aa_subtitle": "Northern Laos", "aa_description": "Ten days, groups of 2-12.",
            "aa_summary": "Walk the old town of Luang Prabang with a guide to the monastery.",
            "aa_highlights": "[]", "aa_itineraries": "Day 1 Luang Prabang. Day 2 the Mekong.",
            "seo_title": "", "seo_meta": "", "country": "Laos", "duration": "10 days"}


PACK_ROW = {"brand_name": "Trail Pulse", "forbidden_words": json.dumps(["cosy"]), "customer_segment": "families"}


class _Conn:
    """Routes fetchrow by SQL text; records execute calls. `update_result` is what the UPDATE returns."""

    def __init__(self, row, update_result="UPDATE 1", edits=()):
        self.row, self.update_result, self.edits = row, update_result, list(edits)
        self.fetchrows, self.executes, self.signals = [], [], []
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        self.transaction = MagicMock(return_value=tx)

    async def fetchrow(self, sql, *args):
        self.fetchrows.append((sql, args))
        if "shared.tenant_brand_rules" in sql:
            return PACK_ROW
        return self.row

    async def fetch(self, sql, *args):
        if "acp_shared.audit_log" in sql:
            return self.edits
        return []

    async def fetchval(self, sql, *args):
        """AA-916: the writing signal's INSERT … RETURNING signal_id."""
        self.signals.append((sql, args))
        return uuid.UUID(int=len(self.signals))

    async def execute(self, sql, *args):
        self.executes.append((sql, args))
        if "UPDATE gold_aa_internal.tenant_tour_versions" in sql:
            return self.update_result
        return "INSERT 0 1"

    def updates(self):
        return [c for c in self.executes if "UPDATE gold_aa_internal.tenant_tour_versions" in c[0]]

    def audits(self):
        return [c for c in self.executes if "INSERT INTO acp_shared.audit_log" in c[0]]


def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    req.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
    return req


async def _apply(conn, role="admin", **body):
    start = SUMMARY.index(BAD)
    body = {"field": "summary", "start": start, "end": start + len(BAD), "text": BAD, "mode": "cut", **body}
    return await cq.apply_tour_sentence(VERSION_ID, cq.TourSentenceApplyBody(**body), _request(conn),
                                        x_admin_secret=SECRET, x_admin_user_id="u1", x_admin_role=role)


# ── tenant copy indicator (T9 + T2 share one function) ──────────────────────────────────────────

def test_tenant_copy_state():
    d = lambda day: dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc)  # noqa: E731
    assert cq.tenant_copy_state(None, None, [d(13)])["older_copy"] is False  # never delivered
    out = cq.tenant_copy_state(d(12), None, [d(13), d(11)])
    assert out["older_copy"] is True and out["edits_after_delivery"] == 1
    assert out["delivered_at"].startswith("2026-10-12")
    assert cq.tenant_copy_state(d(12), d(14), [d(13)])["older_copy"] is False  # re-exported after the edit
    assert cq.tenant_copy_state(None, d(10), [])["older_copy"] is False


# ── T2: what may be corrected, what is flagged ──────────────────────────────────────────────────

@pytest.mark.parametrize("status,version,latest,blocked", [
    ("needs_review", 1, 1, False), ("ai_generated", 2, 2, False),
    ("approved", 1, 1, True), ("rejected", 1, 1, True), ("pending", 1, 1, True), ("failed", 1, 1, True),
    ("needs_review", 1, 2, True),  # the tenant edited it since: v2 is their copy
])
def test_t2_edit_block(status, version, latest, blocked):
    assert (cq.t2_edit_block(status, version, latest) is not None) is blocked


def test_t2_edit_block_never_touches_a_tenant_written_version():
    # v1_tours.update_version(action=edit) inserts a new 'pending' version with edit_source 'tenant_edit'
    assert "tenant wrote" in cq.t2_edit_block("pending", 2, 2, "tenant_edit")
    assert cq.t2_edit_block("needs_review", 2, 2, "tenant_edit") is not None
    assert cq.t2_edit_block("needs_review", 2, 2, "ai_generated") is None


def test_t2_sections_and_flags():
    rc = {**RC, "itineraries": [{"day": 1, "title": "x"}]}  # a day-object itinerary is not editable here
    secs = cq.t2_sections(rc)
    assert [(s["field"], s["index"]) for s in secs] == [("subtitle", None), ("summary", None),
                                                        ("highlights", 0), ("highlights", 1), ("highlights", 2)]
    summary = secs[1]
    assert [SUMMARY[s["start"]:s["end"]] for s in summary["sentences"]][1] == BAD
    from services.jobs.t2_rewrite_job import source_tour_dict
    flagged = cq.t2_flag(secs, source_tour_dict(_version_row()), ["cosy"])
    by = {(f["field"], f["index"]): f["gates"] for f in flagged}
    # AA-800: one stock superlative ("A stunning boat ride") is allowed in a tenant tour; a second one
    # makes both FORBIDDEN_WORD, as T3 validates.
    assert by == {("summary", None): ["T3_grounding"]}
    secs2 = cq.t2_sections({**rc, "subtitle": "A magical week on the Mekong."})
    flagged2 = cq.t2_flag(secs2, source_tour_dict(_version_row()), ["cosy"])
    by2 = {(f["field"], f["index"]): f["gates"] for f in flagged2}
    assert by2[("highlights", 1)] == ["FORBIDDEN_WORD"] and by2[("subtitle", None)] == ["FORBIDDEN_WORD"]


# ── T2 apply: the tenant's copy changes in place ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_cut_updates_the_tenants_row_in_place_and_audits():
    conn = _Conn(_version_row())
    out = await _apply(conn, gate="T3_grounding", problem="number not in source")
    sql, _ = conn.fetchrows[0]
    assert "FOR UPDATE OF ttv" in sql
    [(upd_sql, args)] = conn.updates()
    # the write names the tenant's row explicitly: id AND tenant_id AND a still-editable status
    assert "WHERE id = $1::uuid AND tenant_id = $2::uuid AND status = ANY($4::text[])" in upd_sql
    # silent: only the content changes — no status / edit_source / edited_at / version_number write
    assert "SET rewritten_content = $3::jsonb\n" in upd_sql and "edited_at" not in upd_sql
    assert args[0] == VERSION_ID and args[1] == str(TENANT_ID) and args[3] == ["ai_generated", "needs_review"]
    new_rc = json.loads(args[2])
    assert new_rc["summary"] == ("Walk the old town of Luang Prabang with a local guide. "
                                 "Evenings are free by the river.")
    assert {k: v for k, v in new_rc.items() if k != "summary"} == {k: v for k, v in RC.items() if k != "summary"}
    [(_, audit)] = conn.audits()
    tenant, actor, action, rtype, rid, details = audit
    assert (tenant, actor, action, rtype, rid) == (str(TENANT_ID), "admin:u1", AdminAuditAction.TOUR_SENTENCE_CUT,
                                                   "tenant_tour_version", VERSION_ID)
    d = json.loads(details)
    assert d["field"] == "summary" and d["version_number"] == 1 and d["status"] == "needs_review"
    # AA-916 (ADR 0010 decision 11): the sentence text is in the writing signal (tenant scope, writer t2); the
    # version history keeps the hashes of the section and of the whole version before / after, no text
    [(sig_sql, sig)] = conn.signals
    assert "INSERT INTO shared.writing_signal" in sig_sql
    assert sig[:5] == ("t2", "tour", "tenant", str(TENANT_ID), "summary")
    assert sig[6:8] == (BAD, "") and sig[12] == "sentence_cut" and sig[17:] == ("tenant_tour_version", VERSION_ID)
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()  # noqa: E731
    assert d["signal_id"] == str(uuid.UUID(int=1)) and d["before_sha256"] == sha(BAD)
    assert d["section_before_sha256"] == sha(SUMMARY) and d["section_after_sha256"] == sha(new_rc["summary"])
    assert d["content_before_sha256"] == sha(json.dumps(RC, sort_keys=True))
    assert d["content_after_sha256"] == sha(json.dumps(new_rc, sort_keys=True))
    assert not {"before", "after", "section_before", "section_after", "content_before", "content_after"} & set(d)
    assert out["warnings"] == [] and out["editable"] is True


@pytest.mark.asyncio
async def test_apply_audits_the_canonical_version_id():
    # the drawer reads edits by ttv.id::text, so an upper-case path id must not orphan the row
    conn = _Conn(_version_row())
    start = SUMMARY.index(BAD)
    await cq.apply_tour_sentence(VERSION_ID.upper(), cq.TourSentenceApplyBody(
        field="summary", start=start, end=start + len(BAD), text=BAD, mode="cut"), _request(conn),
        x_admin_secret=SECRET, x_admin_user_id="u1", x_admin_role="admin")
    [(_, audit)] = conn.audits()
    assert audit[4] == VERSION_ID


def test_latest_version_ignores_failed_rewrites():
    # a failed newer rewrite has no text: the older version is still the tenant's copy
    assert "v2.status <> 'failed'" in cq._VERSION_DETAIL_SQL


@pytest.mark.asyncio
async def test_apply_targets_the_rows_own_tenant_not_another():
    conn = _Conn(_version_row(tenant_id=OTHER_TENANT))
    await _apply(conn)
    [(_, args)] = conn.updates()
    assert args[1] == str(OTHER_TENANT)
    [(_, audit)] = conn.audits()
    assert audit[0] == str(OTHER_TENANT)


@pytest.mark.asyncio
async def test_apply_zero_rows_updated_is_a_409_not_a_silent_success():
    conn = _Conn(_version_row(), update_result="UPDATE 0")  # e.g. a FORCE RLS policy hides the row (D2)
    with pytest.raises(HTTPException) as e:
        await _apply(conn)
    assert e.value.status_code == 409 and not conn.audits()


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [_version_row(status="approved"), _version_row(status="pending"),
                                 _version_row(version_number=1, latest=2)])
async def test_apply_refuses_a_version_the_tenant_owns_or_superseded(row):
    conn = _Conn(row)
    with pytest.raises(HTTPException) as e:
        await _apply(conn)
    assert e.value.status_code == 409 and not conn.updates() and not conn.audits()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement,gate", [
    ("The trek climbs 900 steps to the monastery.", "F1_grounding"),
    ("The trek climbs to a stunning monastery.", "FORBIDDEN_WORD"),
    ("The trek climbs to a cosy monastery.", "F2_lexicon"),  # AA-924 (the brand's forbidden word)
    ("Adventure Asia leads the trek to the monastery.", "F11_brand_name"),
])
async def test_apply_rewrite_blocked_by_checks(replacement, gate):
    conn = _Conn(_version_row())
    with pytest.raises(HTTPException) as e:
        await _apply(conn, mode="manual", replacement=replacement)
    assert e.value.status_code == 422 and gate in e.value.detail
    assert not conn.updates() and not conn.audits()


@pytest.mark.asyncio
async def test_apply_manual_rewrite_saves():
    conn = _Conn(_version_row())
    await _apply(conn, mode="manual", replacement="The trek climbs to the monastery above town.")
    [(_, args)] = conn.updates()
    assert "The trek climbs to the monastery above town. Evenings" in json.loads(args[2])["summary"]
    [(_, audit)] = conn.audits()
    assert audit[2] == AdminAuditAction.TOUR_SENTENCE_EDITED


@pytest.mark.asyncio
async def test_apply_cut_of_a_highlight_drops_the_item():
    conn = _Conn(_version_row())
    item = RC["highlights"][1]
    await _apply(conn, field="highlights", index=1, start=0, end=len(item), text=item)
    [(_, args)] = conn.updates()
    assert json.loads(args[2])["highlights"] == [RC["highlights"][0], RC["highlights"][2]]


@pytest.mark.asyncio
async def test_apply_refuses_to_empty_a_text_section_and_stale_spans():
    conn = _Conn(_version_row(rc={**RC, "subtitle": "Temples and rivers."}))
    with pytest.raises(HTTPException) as e:
        await _apply(conn, field="subtitle", start=0, end=19, text="Temples and rivers.")
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await _apply(_Conn(_version_row()), text="A sentence that is not there.")
    assert e.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["content", "reviewer", None])
async def test_t2_write_endpoints_require_admin_role(role):
    conn = _Conn(_version_row())
    with pytest.raises(HTTPException) as e:
        await _apply(conn, role=role)
    assert e.value.status_code == 403
    body = cq.TourSentenceSuggestBody(field="summary", start=0, end=1, text="x")
    with pytest.raises(HTTPException) as e:
        await cq.suggest_tour_sentence(VERSION_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role=role)
    assert e.value.status_code == 403
    assert not conn.fetchrows


def _llm(content):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="haiku-4-5", cost_usd=0.0012,
                                           input_tokens=900, output_tokens=40, stop_reason="end_turn",
                                           satellite_account="acc3", fallback_used=False)
    return fake


@pytest.mark.asyncio
async def test_suggest_tour_sentence_uses_cheap_stage_and_source_tour():
    conn = _Conn(_version_row())
    start = SUMMARY.index(BAD)
    body = cq.TourSentenceSuggestBody(field="summary", start=start, end=start + len(BAD), text=BAD,
                                      problem="number not in source")
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["The trek climbs to the '
                                                               'monastery."]}')) as m, \
            patch.object(generate, "record_call_sync") as rec:
        out = await cq.suggest_tour_sentence(VERSION_ID, body, _request(conn), x_admin_secret=SECRET,
                                             x_admin_role="admin")
    req = m.return_value.generate.call_args.args[0]
    assert req.stage == "t9_repair" and req.max_tokens <= 400
    assert "BRAND: Trail Pulse" in req.user_prompt and "Ten days, groups of 2-12." in req.user_prompt
    kw = rec.call_args.kwargs
    assert kw["tenant_id"] == str(TENANT_ID) and kw["quality_signal"]["mode"] == "admin_suggest_tour"
    assert out["replacement"] == "The trek climbs to the monastery." and out["checks"] == []
    assert not conn.executes  # nothing written


# ── detail ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_version_detail_lists_sections_flags_history_and_older_copy():
    edit = {"action": AdminAuditAction.TOUR_SENTENCE_CUT, "actor": "admin:u1", "created_at": NOW,
            "details": json.dumps({"field": "summary", "before": BAD, "after": ""})}
    conn = _Conn(_version_row(exported_at=NOW - dt.timedelta(days=1)), edits=[edit])
    out = await cq.get_tour_version(VERSION_ID, _request(conn), x_admin_secret=SECRET)
    assert out["editable"] is True and out["edit_block_reason"] is None
    assert out["tenant_copy"]["older_copy"] is True  # downloaded before the admin edit
    assert out["admin_edits"][0]["details"]["before"] == BAD
    # AA-800: the one stock superlative in the highlights is allowed for a tenant (not flagged)
    assert {f["field"] for f in out["flagged_sentences"]} == {"summary"}
    assert "aa_summary" not in out and out["rewritten_content"]["summary"] == SUMMARY


# ── tenant side: the corrected copy carries no admin / gate trace (AA-796 key-walk) ──────────────

@pytest.mark.asyncio
async def test_tenant_version_read_after_a_correction_is_clean():
    conn = _Conn(_version_row())
    await _apply(conn, mode="manual", replacement="The trek climbs to the monastery above town.")
    [(_, args)] = conn.updates()
    written = args[2]

    class _TenantConn:
        async def fetchrow(self, sql, *a):
            return {"id": uuid.UUID(VERSION_ID), "version_number": 1, "status": "needs_review",
                    "edit_source": "ai_generated", "rewrite_language": "en-US", "created_at": NOW,
                    "rewritten_content": written, "published_tour_id": uuid.uuid4(), "quality_score": 6.1,
                    "qa_status": "escalated", "qa_auto_passed": True, "qa_repair_count": 2, "qa_checked_at": NOW,
                    "job_status": "succeeded"}

        async def fetch(self, sql, *a):
            return [{"id": uuid.UUID(VERSION_ID), "version_number": 1, "status": "needs_review",
                     "edit_source": "ai_generated", "created_at": NOW}]

    resp = await v1_tours.get_version(VERSION_ID, _request(_TenantConn()), {"sub": str(TENANT_ID)})
    assert tp.forbidden_keys(resp) == []
    assert resp["status"] == "ai_generated" and len(resp["version_history"]) == 1  # no new version shown
    flat = json.dumps(resp, default=str)
    assert "admin" not in flat and "T3_grounding" not in flat and "FORBIDDEN_WORD" not in flat


# ── tenant DOCX export is recorded (the T2 "older copy" signal) ─────────────────────────────────

def _docx_row():
    return {"version_number": 2, "rewritten_content": json.dumps(RC), "rewrite_language": "en-US",
            "tour_id": uuid.uuid4(), "aa_name": "Laos Loop", "aa_subtitle": "", "aa_summary": "", "aa_highlights": "[]",
            "aa_itineraries": "", "country": "Laos", "duration": "10 days", "inclusions": None, "exclusions": None}


@pytest.mark.asyncio
async def test_docx_export_writes_an_admin_only_audit_row():
    conn = AsyncMock()
    conn.fetchrow.return_value = _docx_row()
    req = _request(conn)
    with patch.object(v1_tours, "write_audit_log", new=AsyncMock()) as audit:
        resp = await v1_tours.export_version_docx(VERSION_ID.upper(), req, {"sub": str(TENANT_ID)})
    assert resp.status_code == 200
    kw = audit.await_args.kwargs  # resource_id canonical (lower-case) even for an upper-case path id
    assert kw["action"] == TenantAuditAction.TOUR_VERSION_EXPORTED == "tenant_tour_version.exported"
    assert (kw["resource_type"], kw["resource_id"], kw["tenant_id"]) == ("tenant_tour_version", VERSION_ID,
                                                                         str(TENANT_ID))


@pytest.mark.asyncio
async def test_docx_export_survives_an_audit_failure():
    conn = AsyncMock()
    conn.fetchrow.return_value = _docx_row()
    with patch.object(v1_tours, "write_audit_log", new=AsyncMock(side_effect=RuntimeError("db"))):
        resp = await v1_tours.export_version_docx(VERSION_ID, _request(conn), {"sub": str(TENANT_ID)})
    assert resp.status_code == 200


def test_version_detail_sql_matches_the_export_action():
    assert f"'{TenantAuditAction.TOUR_VERSION_EXPORTED}'" in cq._VERSION_DETAIL_SQL
    assert f"'{TenantAuditAction.CONTENT_EXPORTED}'" in cq._DELIVERED_SQL
