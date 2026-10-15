"""AA-791 — every T9 attempt keeps its gate ledger (repair_log), and the admin Content Quality
"Label" routes (acp_shared.content_piece_label, migration 231), which must work before the
migration is applied: 503 on the label routes, no label column / filter on the list."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_content_quality as cq
from services.acp_content_writing import service
from services.acp_content_writing.gate_eval import MAX_VIOLATION_CHARS, MAX_VIOLATIONS_PER_GATE, ledger_snapshot

SECRET = "s3cret"
PIECE = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


# ── ledger snapshot ────────────────────────────────────────────────────────────────────────────

def test_snapshot_keeps_every_gate_trimmed():
    ledger = [
        {"gate": "F1_grounding", "passed": True, "blocking": True, "repairable": True, "violations": []},
        {"gate": "F17_trip_fact", "passed": False, "blocking": True, "repairable": True,
         "violations": ["x" * 900] + [f"v{i}" for i in range(9)]},
        {"gate": "F12_scope", "passed": False, "blocking": False, "violations": ["Gobi"]},
        {"gate": "F6_cta_present", "passed": True, "violations": ["stale text on a pass"]},
        "not a gate", {"no": "gate key"},
    ]
    snap = ledger_snapshot(ledger)
    assert [g["gate"] for g in snap] == ["F1_grounding", "F17_trip_fact", "F12_scope", "F6_cta_present"]
    assert set(snap[0]) == {"gate", "passed", "blocking", "violations"}  # no repairable / extra keys
    assert len(snap[1]["violations"]) == MAX_VIOLATIONS_PER_GATE
    assert len(snap[1]["violations"][0]) == MAX_VIOLATION_CHARS
    assert snap[2] == {"gate": "F12_scope", "passed": False, "blocking": False, "violations": ["Gobi"]}
    assert snap[3]["violations"] == [] and snap[3]["blocking"] is True  # a pass carries no violation
    assert ledger_snapshot(None) == []


# ── T9 loop: each failed attempt's ledger in its repair_log entry ──────────────────────────────

GOAL = {"key": "promotion", "name": "Promotion", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
ANGLE = {"idx": 0, "name": "A", "why_it_works": "wa", "formula_fit": "AIDA", "best_final_style": "warm",
         "recommended": True, "chosen": True}


@pytest.fixture
def _loop_isolation():
    """Same isolation as test_aa450_content_writing_service.py: the CTA restore and the final
    integrity pass are covered elsewhere and would edit these placeholder texts."""
    with patch("services.acp_content_writing.service.hygiene.ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch("services.acp_content_writing.service.text_integrity.final_pass",
                  side_effect=lambda text, hyg, **kw: (text, [], [])):
        yield


def _context():
    return {"atom_text": "atom text", "goal": GOAL, "channel_style": {"key": "facebook", "tone": "casual"},
            "brand_audience": {}, "chosen": ANGLE, "cta": "Book a consultation", "destination": None,
            "trip_name": None, "brand_rubric_text": "rubric", "channel": "facebook", "atom_id": "atom_abc123"}


def _outcome(*gates):
    ledger = [{"gate": g, "passed": p, "violations": [] if p else [f"{g} violation"], "repairable": True,
               "blocking": True} for g, p in gates]
    first = next((g for g in ledger if not g["passed"]), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


def _finalized(**over):
    row = {"piece_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "angle_gate_request_id": uuid.uuid4(),
           "attempt_number": 1, "content_text": "x", "status": "approved", "held_reason": None,
           "gate_ledger": [], "repair_log": [], "created_at": datetime.now(timezone.utc)}
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_each_failed_attempt_keeps_its_ledger(_loop_isolation):
    a1 = _outcome(("F1_grounding", True), ("F2_banned_patterns", False), ("F0_complete", True))
    a2 = _outcome(("F1_grounding", True), ("F2_banned_patterns", True), ("F0_complete", True))
    with patch.object(service, "write_content", return_value=("draft 1", 0.02, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=("draft 2", 0.02, {}, None)), \
            patch.object(service, "run_quality_gates", side_effect=[a1, a2]), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized())) as fin:
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), _context(), pool=MagicMock())

    kw = fin.call_args.kwargs
    assert kw["status"] == "approved"
    assert len(kw["repair_log"]) == 1
    entry = kw["repair_log"][0]
    assert entry["gate_targeted"] == "F2_banned_patterns"  # existing keys untouched
    assert [(g["gate"], g["passed"]) for g in entry["gate_ledger"]] == [
        ("F1_grounding", True), ("F2_banned_patterns", False), ("F0_complete", True)]
    assert entry["gate_ledger"][1]["violations"] == ["F2_banned_patterns violation"]
    # the passing attempt's ledger is the piece's own column → every attempt is covered
    assert [g["gate"] for g in kw["gate_ledger"]] == ["F1_grounding", "F2_banned_patterns", "F0_complete"]


@pytest.mark.asyncio
async def test_regressed_attempt_ledger_survives_the_revert(_loop_isolation):
    """AA-528 revert: attempt 2 is discarded and attempt 1's ledger becomes the column — attempt 2's
    own ledger (the new F1 failure) is still in its repair_log entry."""
    a1 = _outcome(("F4_extreme_length", False), ("F1_grounding", True))
    a2 = _outcome(("F4_extreme_length", False), ("F1_grounding", False))
    with patch.object(service, "write_content", return_value=("attempt 1", 0.02, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=("attempt 2", 0.02, {}, None)), \
            patch.object(service, "run_quality_gates", side_effect=[a1, a2]), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value=_finalized(status="held"))) as fin:
        await service.run_write_background(uuid.uuid4(), uuid.uuid4(), _context(), pool=MagicMock())

    log = fin.call_args.kwargs["repair_log"]
    assert [e["gate_targeted"] for e in log] == ["F4_extreme_length", "F4_extreme_length", "gate_regression_guard"]
    assert {g["gate"]: g["passed"] for g in log[1]["gate_ledger"]} == {"F4_extreme_length": False,
                                                                       "F1_grounding": False}
    assert "gate_ledger" not in log[2]  # the revert note is not an attempt


# ── label routes ───────────────────────────────────────────────────────────────────────────────

def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


def _label_row(by="admin:u1", label="light"):
    return {"labelled_by": by, "label": label, "defects": json.dumps({"fact": "light"}), "minutes_to_fix": 5,
            "reason": "one time not in the seed", "labelled_at": datetime(2026, 10, 14, tzinfo=timezone.utc),
            "text_changed_since_label": False}


def _hdr(**over):
    base = {"x_admin_secret": SECRET, "x_admin_user_id": "u1", "x_admin_role": "admin"}
    base.update(over)
    return base


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["get", "post"])
async def test_label_routes_503_before_migration_231(call):
    conn = AsyncMock()
    conn.fetchval.return_value = False  # to_regclass(...) IS NOT NULL → false
    with pytest.raises(HTTPException) as e:
        if call == "get":
            await cq.get_piece_label(PIECE, _request(conn), **_hdr())
        else:
            await cq.set_piece_label(PIECE, cq.PieceLabelBody(label="light"), _request(conn), **_hdr())
    assert e.value.status_code == 503 and "migration 231" in e.value.detail
    conn.execute.assert_not_awaited()
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "reviewer", "content"])
async def test_label_routes_are_admin_only(role):
    for call in (cq.get_piece_label(PIECE, _request(AsyncMock()), **_hdr(x_admin_role=role)),
                 cq.set_piece_label(PIECE, cq.PieceLabelBody(label="light"), _request(AsyncMock()),
                                    **_hdr(x_admin_role=role))):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("body, status", [
    ({"label": "great"}, 422),
    ({"label": "light", "defects": {"tone": "light"}}, 422),
    ({"label": "light", "defects": {"fact": "terrible"}}, 422),
])
async def test_label_body_validated(body, status):
    conn = AsyncMock()
    with pytest.raises(HTTPException) as e:
        await cq.set_piece_label(PIECE, cq.PieceLabelBody(**body), _request(conn), **_hdr())
    assert e.value.status_code == status
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_needs_an_admin_identity():
    with pytest.raises(HTTPException) as e:
        await cq.set_piece_label(PIECE, cq.PieceLabelBody(label="as_is"), _request(AsyncMock()),
                                 **_hdr(x_admin_user_id=None))
    assert e.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code", [(None, 404), ("processing", 409), ("failed", 409)])
async def test_label_refuses_missing_or_unfinished_piece(status, code):
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, status]
    with pytest.raises(HTTPException) as e:
        await cq.set_piece_label(PIECE, cq.PieceLabelBody(label="heavy"), _request(conn), **_hdr())
    assert e.value.status_code == code
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_upserts_one_row_per_labeller_with_text_hash():
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, "held"]
    conn.fetch.return_value = [_label_row("admin:u2", "heavy"), _label_row("admin:u1", "light")]
    body = cq.PieceLabelBody(label="light", defects={"fact": "light", "ai_phrasing": "none"}, minutes_to_fix=5,
                             reason="  one time not in the seed  ")
    out = await cq.set_piece_label(PIECE, body, _request(conn), **_hdr())
    sql, *params = conn.execute.await_args.args
    assert "ON CONFLICT (piece_id, labelled_by) DO UPDATE" in sql
    assert "sha256(convert_to(COALESCE(cp.content_text" in sql
    assert params == [PIECE, "admin:u1", "light", json.dumps({"fact": "light", "ai_phrasing": "none"}), 5,
                      "one time not in the seed"]
    assert out["mine"]["label"] == "light" and out["mine"]["defects"] == {"fact": "light"}
    assert [x["labelled_by"] for x in out["labels"]] == ["admin:u2", "admin:u1"]


@pytest.mark.asyncio
async def test_get_label_returns_all_labels_and_mine():
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, 1]
    conn.fetch.return_value = [_label_row("admin:u2", "reject")]
    out = await cq.get_piece_label(PIECE, _request(conn), **_hdr())
    assert out["mine"] is None and out["labels"][0]["label"] == "reject"
    assert out["labels"][0]["labelled_at"].startswith("2026-10-14")
    assert out["choices"]["label"] == ["as_is", "light", "heavy", "reject"]
    assert out["choices"]["defect_classes"] == ["fact", "voice", "format", "ai_phrasing"]


@pytest.mark.asyncio
async def test_get_label_404_for_unknown_piece():
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, None]
    with pytest.raises(HTTPException) as e:
        await cq.get_piece_label(PIECE, _request(conn), **_hdr())
    assert e.value.status_code == 404


# ── list: label column + filter, tolerant of a missing table ───────────────────────────────────

async def _list(conn, label=None):
    return await cq.list_pieces(_request(conn), limit=50, offset=0, tenant_id=None, channel=None, status=None,
                                gate=None, q=None, label=label, sort="created_at", sort_dir="desc",
                                x_admin_secret=SECRET)


@pytest.mark.asyncio
async def test_list_without_migration_has_no_label_and_ignores_the_filter():
    conn = AsyncMock()
    conn.fetchval.side_effect = [False, 0]  # to_regclass → false, then count(*)
    conn.fetch.return_value = []
    out = await _list(conn, label="heavy")
    sql, *params = conn.fetch.await_args.args
    assert "content_piece_label" not in sql and params == [50, 0]
    assert out["labels_enabled"] is False


@pytest.mark.asyncio
async def test_list_with_label_filter_on_latest_label():
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, 1]
    conn.fetch.return_value = []
    out = await _list(conn, label="heavy")
    sql, *params = conn.fetch.await_args.args
    assert "AS label," in sql and "AS label_count" in sql
    assert "ORDER BY l.labelled_at DESC LIMIT 1) = $1" in sql
    assert params == ["heavy", 50, 0] and out["labels_enabled"] is True
    count_sql = conn.fetchval.await_args_list[1].args[0]
    assert "ORDER BY l.labelled_at DESC LIMIT 1) = $1" in count_sql  # total counts the same filter


@pytest.mark.asyncio
@pytest.mark.parametrize("label, clause", [("labelled", "AND EXISTS"), ("unlabelled", "AND NOT EXISTS")])
async def test_list_labelled_unlabelled(label, clause):
    conn = AsyncMock()
    conn.fetchval.side_effect = [True, 0]
    conn.fetch.return_value = []
    await _list(conn, label=label)
    sql, *params = conn.fetch.await_args.args
    assert f"{clause} (SELECT 1 FROM acp_shared.content_piece_label" in sql and params == [50, 0]


@pytest.mark.asyncio
async def test_list_unknown_label_filter_is_422():
    with pytest.raises(HTTPException) as e:
        await _list(AsyncMock(), label="great")
    assert e.value.status_code == 422
