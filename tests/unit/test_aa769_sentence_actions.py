"""AA-769 (S234) — cut / rewrite one flagged sentence from the Content Quality drawer: edit-ready
highlights, the deterministic apply (lock, span match, checks, splice, audit) and the one cheap
suggest call (stage t9_repair, logged with the piece id)."""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import admin_content_quality as cq
from services.acp_content_writing import generate, service
from services.acp_content_writing import sentence_repair as sr
from services.acp_shared.audit_log import AdminAuditAction
from shared.llm_client.role_config import SAFE_DEFAULTS

SECRET = "s3cret"
PIECE_ID = str(uuid.uuid4())
TENANT_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)


TEXT = ("Opening line here. The monastery sits above Luang Prabang today. "
        "At Arcadia Adventures, we build it. It is a hidden gem.")
F9_PHRASE = {"gate": "F9_brand_style", "passed": False, "blocking": False,
             "violations": ['audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "hidden gem"']}
WRITER_INPUT = {"seed": "The monastery sits on a hill. Groups of 7-16.", "facts": "",
                "brand_pack": {"brand_name": "Trail Pulse", "forbidden_words": ["cosy"],
                               "customer_segment": "families"},
                "trip": "Laos Loop", "destination": "Laos", "channel": "linkedin"}


# ── highlights / flagged sentences ────────────────────────────────────────────────────────────

def test_highlights_phrase_expands_to_sentence():
    [h] = cq.highlights(TEXT, [], [F9_PHRASE])
    assert h["text"] == "hidden gem"
    assert TEXT[h["sentence_start"]:h["sentence_end"]] == "It is a hidden gem."
    assert h["sentence"] == "It is a hidden gem." and len(h["key"]) == 12


def test_highlights_merge_same_sentence_two_gates():
    text = "Intro here. It is a hidden gem with 900 steps. Bye now."
    f1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "violations": [
        "sentence states ['900'] not present in the content seed: 'It is a hidden gem with 900 steps.'"]}
    hs = cq.highlights(text, [f1], [F9_PHRASE])
    assert len(hs) == 2  # the display marks stay per gate
    [item] = cq.flagged_sentences(text, hs)
    assert item["gates"] == ["F1_grounding", "F9_brand_style"] and item["blocking"] is True
    assert item["text"] == "It is a hidden gem with 900 steps." == text[item["start"]:item["end"]]
    assert len(item["problems"]) == 2


# ── sentence_repair helpers ───────────────────────────────────────────────────────────────────

def test_expand_to_sentence_stops_at_newline_and_keeps_tags():
    text = "First one. A hidden gem here. [R:a1] Next.\n- a hidden gem list item\nEnd."
    i = text.find("hidden gem")
    s, e = sr.expand_to_sentence(text, i, i + 10)
    assert text[s:e] == "A hidden gem here. [R:a1]"
    j = text.rfind("hidden gem")
    s, e = sr.expand_to_sentence(text, j, j + 10)
    assert text[s:e] == "- a hidden gem list item"


def test_expand_keeps_a_located_whole_sentence():
    text = "Intro. The monastery clings to a cliff [R:a1] face above a drop. [R:a1] Next one."
    span = sr.locate_sentence(text, "The monastery clings to a cliff face above")
    assert sr.expand_to_sentence(text, *span) == span


def test_tidy_after_cut_collapses_blank_lines_and_spaces():
    assert sr.tidy_after_cut("A. \n\n\n\nB.  C.\n D") == "A.\n\nB. C.\nD"


def test_apply_edit_cut_at_paragraph_end_leaves_no_debris():
    text = "A one. Cut me. [R:x]\n\nB two."
    assert sr.apply_edit(text, 7, 20, "") == "A one.\n\nB two."
    assert sr.apply_edit("A one.\n\nCut me.\n\nB two.", 8, 15, "") == "A one.\n\nB two."
    assert sr.apply_edit("A one.\n- item here\nB two.", 7, 18, "") == "A one.\nB two."
    assert sr.apply_edit(text, 7, 20, "New one.") == "A one. New one. [R:x]\n\nB two."


def test_resolve_span_exact_moved_and_ambiguous():
    assert sr.resolve_span("Xa. Bb.", 4, 7, "Bb.") == (4, 7)
    assert sr.resolve_span("New. Xa. Bb.", 4, 7, "Bb.") == (9, 12)
    assert sr.resolve_span("Bb. Bb.", 9, 12, "Bb.") is None
    assert sr.resolve_span("Xa.", 0, 3, "Gone.") is None


def test_check_replacement_runs_the_deterministic_gates():
    grounding = service.grounding_from_writer_input(WRITER_INPUT)
    pack = WRITER_INPUT["brand_pack"]
    assert sr.check_replacement("The monastery sits on a hill above town.", grounding, pack) == []
    assert sr.check_replacement("Groups of 7-16 walk up.", grounding, pack) == []  # source number
    gates = lambda s: [f["gate"] for f in sr.check_replacement(s, grounding, pack)]  # noqa: E731
    assert gates("The monastery has 900 steps.") == ["F1_grounding"]
    assert gates("The rooms are cosy at dawn.") == ["F2_brand_forbidden"]
    assert "F2_banned_patterns" in gates("The view is breathtaking at dawn.")
    assert gates("Adventure Asia runs it every week.") == ["F11_brand_name"]
    assert sr.check_replacement("", grounding, pack) == []


# ── grounding: one implementation ─────────────────────────────────────────────────────────────

def test_grounding_from_writer_input_matches_grounding_text():
    ctx = {"atom_text": "seed text", "route_segments": [("a1", "m1")], "facts_text": "[Fact id=f] 3 days",
           "brand_audience": {"brand_name": "Terra", "core_idea": "slow", "customer_segment": "kids 7-16"},
           "chosen": {"name": "A"}, "goal": {"key": "promotion"}, "channel": "blog", "trip_name": "T",
           "destination": "Laos", "cta": "Ask"}
    assert service.grounding_from_writer_input(service.writer_input_snapshot(ctx)) == service._grounding_text(ctx)
    ctx2 = {"atom_text": "only seed"}
    assert service.grounding_from_writer_input(service.writer_input_snapshot(ctx2)) == "only seed"


# ── stage + generate ──────────────────────────────────────────────────────────────────────────

def test_role_config_has_t9_repair_haiku():
    cfg = SAFE_DEFAULTS["t9_repair"]
    assert (cfg.stage, cfg.role, cfg.model_id, tuple(cfg.fallback_model_ids)) == \
        ("t9_repair", "writer", "haiku", ("sonnet",))


def _llm(content='{"replacements": ["x"]}'):
    fake = MagicMock()
    fake.generate.return_value = MagicMock(content=content, model_used="haiku-4-5", cost_usd=0.0013,
                                           input_tokens=900, output_tokens=40, stop_reason="end_turn",
                                           satellite_account="acc3", fallback_used=False)
    return fake


def test_repair_sentences_defaults_unchanged():
    items = [{"sentence": "A.", "problem": "p"}]
    with patch.object(generate, "LLMClient", return_value=_llm()) as m, \
         patch.object(generate, "record_call_sync") as rec:
        assert generate.repair_sentences(sentences=items, content_seed="seed", channel="blog") == (["x"], 0.0013)
    req = m.return_value.generate.call_args.args[0]
    assert (req.stage, req.max_tokens) == ("t9_write", generate._MAX_TOKENS)
    kw = rec.call_args.kwargs
    assert kw["stage"] == "t9_write" and kw["content_piece_id"] is None
    assert kw["quality_signal"] == {"channel": "blog", "attempt": 2, "mode": "sentence_repair", "sentences": 1}


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────

def _request(conn):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    req = MagicMock()
    req.app.state.pool.acquire.return_value = ctx
    return req


def _conn(row):
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchrow.return_value = row
    return conn


def _piece_row(status="held", text=TEXT, writer_input=WRITER_INPUT, channel="linkedin", published=False):
    return {"content_text": text, "status": status, "tenant_id": TENANT_ID,
            "writer_input": json.dumps(writer_input) if writer_input is not None else None,
            "channel": channel, "published": published, "angle_gate_request_id": uuid.uuid4()}


SENT = "At Arcadia Adventures, we build it."
START = TEXT.index(SENT)


async def _apply(conn, role="admin", **body):
    body = {"start": START, "end": START + len(SENT), "text": SENT, "mode": "cut", **body}
    with patch.object(cq, "_load_piece_detail", new=AsyncMock(return_value={"piece_id": PIECE_ID})):
        return await cq.apply_sentence(PIECE_ID, cq.SentenceApplyBody(**body), _request(conn),
                                       x_admin_secret=SECRET, x_admin_user_id="u1", x_admin_role=role)


def _updates(conn):
    return [c for c in conn.execute.await_args_list if "UPDATE acp_shared.content_piece" in c.args[0]]


def _audits(conn):
    return [c for c in conn.execute.await_args_list if "INSERT INTO acp_shared.audit_log" in c.args[0]]


@pytest.mark.asyncio
async def test_apply_cut_updates_text_and_writes_audit():
    conn = _conn(_piece_row())
    out = await _apply(conn, gate="F11_brand_name", problem="foreign brand")
    assert out == {"piece_id": PIECE_ID}
    assert "FOR UPDATE" in conn.fetchrow.await_args.args[0]
    [upd] = _updates(conn)
    assert upd.args[2] == "Opening line here. The monastery sits above Luang Prabang today. It is a hidden gem."
    [audit] = _audits(conn)
    tenant, actor, action, rtype, rid, details = audit.args[1:]
    assert (tenant, actor, action, rtype, rid) == (str(TENANT_ID), "admin:u1", AdminAuditAction.SENTENCE_CUT,
                                                   "content_piece", PIECE_ID)
    d = json.loads(details)
    assert d["before"] == SENT and d["after"] == "" and d["mode"] == "cut" and d["gate"] == "F11_brand_name"
    assert d["length_before"] == len(TEXT) and d["published"] is False


@pytest.mark.asyncio
async def test_apply_relocates_moved_span():
    conn = _conn(_piece_row(text="New first sentence. " + TEXT))
    await _apply(conn)
    [upd] = _updates(conn)
    assert SENT not in upd.args[2] and upd.args[2].startswith("New first sentence. Opening line here.")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Totally different text now.", f"{SENT} {SENT} More words here."])
async def test_apply_rejects_stale_span(text):
    conn = _conn(_piece_row(text=text))
    with pytest.raises(HTTPException) as e:
        await _apply(conn, start=500, end=530)
    assert e.value.status_code == 409
    assert not _updates(conn) and not _audits(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["processing", "failed"])
async def test_apply_rejects_status_processing_or_failed(status):
    conn = _conn(_piece_row(status=status))
    with pytest.raises(HTTPException) as e:
        await _apply(conn)
    assert e.value.status_code == 409 and not _updates(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement,gate", [
    ("We build it with 900 guides.", "F1_grounding"),
    ("Adventure Asia builds it.", "F11_brand_name"),
    ("We build it, and it is cosy.", "F2_brand_forbidden"),
])
async def test_apply_rewrite_blocked_by_deterministic_guard(replacement, gate):
    conn = _conn(_piece_row())
    with pytest.raises(HTTPException) as e:
        await _apply(conn, mode="manual", replacement=replacement)
    assert e.value.status_code == 422 and gate in e.value.detail
    assert not _updates(conn) and not _audits(conn)


@pytest.mark.asyncio
async def test_apply_rewrite_saves_and_audits_the_suggestion():
    conn = _conn(_piece_row())
    await _apply(conn, mode="rewrite", replacement="Trail Pulse builds it.", suggestion="Trail Pulse builds it.",
                 suggestion_model="haiku-4-5", suggestion_cost_usd=0.0013)
    [upd] = _updates(conn)
    assert "Trail Pulse builds it. It is a hidden gem." in upd.args[2]
    [audit] = _audits(conn)
    d = json.loads(audit.args[6])
    assert audit.args[3] == AdminAuditAction.SENTENCE_REWRITTEN
    assert d["after"] == "Trail Pulse builds it." and d["suggestion_model"] == "haiku-4-5"


@pytest.mark.asyncio
async def test_apply_refuses_cut_that_empties_body():
    conn = _conn(_piece_row(text=SENT))
    with pytest.raises(HTTPException) as e:
        await _apply(conn, start=0, end=len(SENT))
    assert e.value.status_code == 422 and not _updates(conn)


@pytest.mark.asyncio
async def test_apply_refuses_cut_under_blog_minimum():
    body = "Word " * 940 + SENT  # 4,700 chars + the sentence: the cut lands under the blog minimum
    conn = _conn(_piece_row(text=body, channel="blog"))
    with pytest.raises(HTTPException) as e:
        await _apply(conn, start=body.index(SENT), end=len(body))
    assert e.value.status_code == 422 and "minimum" in e.value.detail


@pytest.mark.asyncio
async def test_apply_manual_without_writer_input_uses_rest_of_piece_as_source():
    conn = _conn(_piece_row(writer_input=None, text="There are 12 rooms. " + TEXT))
    await _apply(conn, mode="manual", replacement="All 12 rooms are ours.")
    assert _updates(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["content", "reviewer", None])
async def test_write_endpoints_require_admin_role_header(role):
    conn = _conn(_piece_row())
    with pytest.raises(HTTPException) as e:
        await _apply(conn, role=role)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await cq.suggest_sentence(PIECE_ID, cq.SentenceSuggestBody(start=0, end=1, text="x"), _request(conn),
                                  x_admin_secret=SECRET, x_admin_role=role)
    assert e.value.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_suggest_uses_cheap_stage_and_logs_piece():
    conn = _conn(_piece_row())
    body = cq.SentenceSuggestBody(start=START, end=START + len(SENT), text=SENT, problem="foreign brand",
                                  note="keep it short")
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["Trail Pulse builds it."]}')) \
            as m, patch.object(generate, "record_call_sync") as rec:
        out = await cq.suggest_sentence(PIECE_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role="admin")
    req = m.return_value.generate.call_args.args[0]
    assert req.stage == "t9_repair" and req.max_tokens <= 400
    assert "foreign brand Editor note: keep it short" in req.user_prompt and "BRAND: Trail Pulse" in req.user_prompt
    kw = rec.call_args.kwargs
    assert kw["stage"] == "t9_repair" and kw["content_piece_id"] == PIECE_ID
    assert kw["quality_signal"]["mode"] == "admin_suggest"
    assert out["replacement"] == "Trail Pulse builds it." and out["checks"] == []
    assert out["model"] == "haiku-4-5" and out["cost_usd"] == 0.0013
    conn.execute.assert_not_awaited()  # nothing written to the piece


@pytest.mark.asyncio
async def test_suggest_reports_checks_on_a_bad_suggestion():
    conn = _conn(_piece_row())
    body = cq.SentenceSuggestBody(start=START, end=START + len(SENT), text=SENT)
    with patch.object(generate, "LLMClient", return_value=_llm('{"replacements": ["Adventure Asia builds it."]}')), \
            patch.object(generate, "record_call_sync"):
        out = await cq.suggest_sentence(PIECE_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role="admin")
    assert [c["gate"] for c in out["checks"]] == ["F11_brand_name"]


@pytest.mark.asyncio
async def test_suggest_409_without_writer_input():
    conn = _conn(_piece_row(writer_input=None))
    body = cq.SentenceSuggestBody(start=START, end=START + len(SENT), text=SENT)
    with patch.object(generate, "LLMClient") as m:
        with pytest.raises(HTTPException) as e:
            await cq.suggest_sentence(PIECE_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role="admin")
    assert e.value.status_code == 409
    m.assert_not_called()


@pytest.mark.asyncio
async def test_suggest_502_on_unreadable_reply():
    conn = _conn(_piece_row())
    body = cq.SentenceSuggestBody(start=START, end=START + len(SENT), text=SENT)
    with patch.object(generate, "LLMClient", return_value=_llm("not json at all")), \
            patch.object(generate, "record_call_sync"):
        with pytest.raises(HTTPException) as e:
            await cq.suggest_sentence(PIECE_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role="admin")
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_piece_detail_cost_includes_content_piece_id_calls_and_admin_edits():
    from datetime import datetime, timezone
    row = {"piece_id": uuid.UUID(PIECE_ID), "tenant_id": TENANT_ID, "job_id": uuid.uuid4(),
           "request_id": uuid.uuid4(), "content_text": TEXT, "gate_ledger": "[]", "flags": json.dumps([F9_PHRASE]),
           "published": True}
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    edit = {"action": AdminAuditAction.SENTENCE_CUT, "actor": "admin:u1",
            "details": json.dumps({"before": "x", "after": ""}),
            "created_at": datetime(2026, 10, 13, tzinfo=timezone.utc)}
    conn.fetch.side_effect = [[{"stage": "t9_repair", "role": "writer", "calls": 1, "cost_usd": 0.001}], [], [edit]]
    out = await cq.get_piece(PIECE_ID, _request(conn), x_admin_secret=SECRET)
    cost_sql, job_id, pid = conn.fetch.await_args_list[0].args
    assert "content_piece_id = $2" in cost_sql and pid == PIECE_ID and job_id == row["job_id"]
    edits_sql, *params = conn.fetch.await_args_list[2].args
    assert "acp_shared.audit_log" in edits_sql and params == [str(TENANT_ID), PIECE_ID,
                                                              "content_piece.admin_sentence_%"]
    assert out["admin_edits"][0]["details"] == {"before": "x", "after": ""}
    assert out["published"] is True and out["flagged_sentences"][0]["gates"] == ["F9_brand_style"]
    assert out["cost_by_stage"][0]["stage"] == "t9_repair"
