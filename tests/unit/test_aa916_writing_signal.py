"""AA-916 WM-03 / WM-06 — services/writing_memory: record_writer_feedback (one INSERT, hashes, refusals), the
best-effort wrapper, the audit details (ids and hashes, never text), the edit-history reader and the reason codes."""
import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from services.quality import registry as qr
from services.writing_memory import reason_codes as rc
from services.writing_memory import signals as wm

TENANT = str(uuid.uuid4())
SID = uuid.uuid4()


def _conn(fetchval=SID):
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.tx = tx
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(return_value=fetchval) if not isinstance(fetchval, Exception) else \
        AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _kw(**over):
    kw = dict(writer="t9_blog", scope="tenant", tenant_id=TENANT, action="tenant_edit", actor_type="tenant",
              source_table="content_piece", source_id="p1", before_text="Old text.", after_text="New text.")
    kw.update(over)
    return kw


# ── record_writer_feedback ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_insert_with_hashes_and_the_returned_id():
    conn = _conn()
    ref = await wm.record_writer_feedback(conn, **_kw(locator={"start": 1, "end": 4}, gate=qr.F1_GROUNDING))
    sql, *args = conn.fetchval.await_args.args
    assert sql.strip().startswith("INSERT INTO shared.writing_signal") and "RETURNING signal_id" in sql
    assert conn.fetchval.await_count == 1                                   # one statement, no model call
    sha = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()  # noqa: E731
    assert ref == wm.SignalRef(str(SID), sha("Old text."), sha("New text."))
    assert args[:4] == ["t9_blog", None, "tenant", TENANT] and json.loads(args[5]) == {"start": 1, "end": 4}
    assert args[6:10] == ["Old text.", "New text.", sha("Old text."), sha("New text.")]
    assert args[15] == "pending" and args[17:] == ["content_piece", "p1", None, None]   # AA-917: created_at


def test_sha256_is_lower_hex_of_utf8():
    assert wm.text_sha256("Hà Nội") == hashlib.sha256("Hà Nội".encode("utf-8")).hexdigest()
    assert wm.text_sha256(None) is None and len(wm.text_sha256("")) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("over,msg", [
    ({"scope": "platform"}, "platform rows never name a tenant"),           # platform + tenant_id, refused in code
    ({"tenant_id": None}, "tenant rows always do"),
    ({"writer": "s1"}, "writer"), ({"action": "approve"}, "action"), ({"actor_type": "bot"}, "actor_type"),
    ({"reason_code": "bogus"}, "reason_code"), ({"fact_status": "maybe"}, "fact_status"),
    ({"locator": [1, 2]}, "locator"), ({"before_text": "x" * (wm.MAX_TEXT + 1)}, "longer than"),
])
async def test_refuses_what_the_table_would_refuse(over, msg):
    conn = _conn()
    with pytest.raises(ValueError, match=msg):
        await wm.record_writer_feedback(conn, **_kw(**over))
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_not_in_source_sets_fact_status_fail():
    conn = _conn()
    await wm.record_writer_feedback(conn, **_kw(reason_code="fact_not_in_source", actor_type="admin",
                                                action="sentence_cut"))
    assert conn.fetchval.await_args.args[16] == "fail"
    await wm.record_writer_feedback(conn, **_kw(reason_code="style_only", actor_type="admin", action="sentence_cut"))
    assert conn.fetchval.await_args.args[16] == "pending"


@pytest.mark.asyncio
async def test_safe_wrapper_runs_in_a_savepoint_and_never_raises():
    conn = _conn(fetchval=RuntimeError("relation does not exist"))
    with capture_logs() as logs:
        ref = await wm.record_writer_feedback_safe(conn, **_kw())
    assert ref is None and conn.transaction.call_count == 1                # nested transaction = SAVEPOINT
    assert conn.tx.__aexit__.await_args.args[0] is RuntimeError             # the savepoint rolled back
    [line] = [e for e in logs if e["event"] == "aa916_signal_failed"]
    assert line["log_level"] == "warning" and line["source_id"] == "p1"
    ok = await wm.record_writer_feedback_safe(_conn(), **_kw())
    assert ok is not None and ok.signal_id == str(SID)


def test_audit_details_hold_ids_and_hashes_only():
    ref = wm.SignalRef("sid", "a" * 64, "b" * 64)
    assert wm.audit_details(ref, "voice_off_brand") == {"signal_id": "sid", "before_sha256": "a" * 64,
                                                        "after_sha256": "b" * 64, "reason_code": "voice_off_brand"}
    assert wm.audit_details(None) == {}


def test_scope_and_writer_helpers():
    assert wm.scope_for_tenant(wm.PLATFORM_TENANT_ID) == ("platform", None)
    assert wm.scope_for_tenant(None) == ("platform", None)
    assert wm.scope_for_tenant(uuid.UUID(TENANT)) == ("tenant", TENANT)
    assert wm.writer_for_channel("blog") == "t9_blog" and wm.writer_for_channel(None) == "t9_blog"
    assert wm.writer_for_channel("linkedin") == wm.writer_for_channel("facebook") == "t9_social"


# ── the edit-history reader ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_problem_is_stored_on_the_signal_and_the_caps_refuse_in_code():
    conn = _conn()
    await wm.record_writer_feedback(conn, writer="t2", scope="tenant", tenant_id=TENANT, action="sentence_cut",
                                    actor_type="admin", source_table="tenant_tour_version", source_id="v",
                                    problem="number(s) ['4,200'] not in the source tour")
    assert conn.fetchval.await_args.args[-2] == "number(s) ['4,200'] not in the source tour"
    assert "problem," in conn.fetchval.await_args.args[0] and "$20" in conn.fetchval.await_args.args[0]
    for kw in ({"problem": "p" * (wm.MAX_PROBLEM + 1)}, {"actor_id": "a" * 129}):
        with pytest.raises(ValueError, match="longer than"):
            await wm.record_writer_feedback(conn, writer="a1", scope="platform", tenant_id=None, action="reject",
                                            actor_type="admin", source_table="review_queue", source_id="r", **kw)


@pytest.mark.parametrize("raw,expected", [
    (None, "unknown"), ("", "unknown"), ("u1", "unknown"), ("x" * 300, "unknown"), ("ops@example.com", "unknown"),
    (str(uuid.UUID(int=3)).upper(), str(uuid.UUID(int=3))),
])
def test_admin_actor_id_keeps_only_a_canonical_uuid(raw, expected):
    assert wm.admin_actor_id(raw) == expected


@pytest.mark.asyncio
async def test_history_reads_new_rows_from_the_signal_and_keeps_legacy_rows():
    sid = str(uuid.uuid4())
    conn = _conn()
    conn.fetch = AsyncMock(return_value=[{"signal_id": sid, "before_text": "Old.", "after_text": None,
                                          "problem": "'Wat Xieng Thong 1560' not present",
                                          "reason_code": "ai_phrasing", "exemplar_pref": None,
                                          "fact_status": "pending"}])
    edits = [{"action": "x", "details": {"signal_id": sid, "mode": "cut", "problem_sha256": "h"}},
             {"action": "y", "details": {"before": "legacy", "after": "row", "problem": "old"}}]   # D16
    out = await wm.with_signal_texts(conn, edits, TENANT)
    assert out[0]["details"] == {"signal_id": sid, "mode": "cut", "problem_sha256": "h", "before": "Old.",
                                 "after": "", "problem": "'Wat Xieng Thong 1560' not present",
                                 "reason_code": "ai_phrasing", "exemplar_pref": None}
    assert out[1]["details"] == {"before": "legacy", "after": "row", "problem": "old"}
    sql, ids, owner = conn.fetch.await_args.args
    assert "tenant_id IS NOT DISTINCT FROM $2::uuid" in sql and ids == [sid] and owner == TENANT  # explicit filter


@pytest.mark.asyncio
async def test_history_reader_skips_the_query_without_signals_and_platform_rows_filter_on_null():
    conn = _conn()
    assert await wm.load_texts(conn, ["not-a-uuid", None], TENANT) == {}
    conn.fetch.assert_not_awaited()
    await wm.load_texts(conn, [str(uuid.uuid4())], wm.PLATFORM_TENANT_ID)
    assert conn.fetch.await_args.args[2] is None


# ── reason codes ─────────────────────────────────────────────────────────────────────────────────

def test_the_eight_codes_match_the_migration_check():
    assert rc.REASON_CODES == ("fact_not_in_source", "optional_as_included", "wrong_place_or_time",
                               "voice_off_brand", "ai_phrasing", "format_channel", "repair_residue", "style_only")
    sql = (Path(__file__).resolve().parents[2] / "api" / "migrations" / "277_writing_signal.sql").read_text()
    check = sql.split("CONSTRAINT writing_signal_reason_code_check")[1].split("CONSTRAINT")[0]
    assert all(f"'{c}'" in check for c in rc.REASON_CODES)
    for name, values in (("writer", wm.WRITERS), ("action", wm.ACTIONS)):
        block = sql.split(f"CONSTRAINT writing_signal_{name}_check")[1].split("CONSTRAINT")[0]
        assert all(f"'{v}'" in block for v in values), name


@pytest.mark.parametrize("gate,code", [
    (qr.F1_GROUNDING, "fact_not_in_source"), (qr.F9_CTA_FACT, "fact_not_in_source"),
    (qr.F17_TRIP_FACT, "fact_not_in_source"), (qr.T3_GROUNDING, "fact_not_in_source"),
    (qr.F12_SCOPE, "wrong_place_or_time"), (qr.F9_BRAND_STYLE, "voice_off_brand"),
    (qr.F15_VOICE, "voice_off_brand"), (qr.T2_FORBIDDEN_WORD, "voice_off_brand"),
    (qr.F13_HYGIENE, "repair_residue"), (qr.F13_REPEAT, "repair_residue"),
    (qr.F14_SOCIAL_SHAPE, "format_channel"), (qr.F4_EXTREME_LENGTH, "format_channel"),
    (qr.F2_BANNED_PATTERNS, "ai_phrasing"), (None, "style_only"), ("not_a_gate", "style_only"),
])
def test_suggested_reason(gate, code):
    assert rc.suggested_reason(gate) == code


def test_every_registered_gate_gets_a_known_code():
    for gate in qr.GATE_CONSTANTS.values():
        assert rc.suggested_reason(gate) in rc.REASON_CODES, gate
