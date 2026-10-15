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
    # S234 stack on AA-768: the bullet marker stays out of the span (cut → empty bullet → tidied away)
    assert text[s:e] == "a hidden gem list item"


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
    assert kw["quality_signal"] == {"channel": "blog", "attempt": 2, "mode": "sentence_repair", "sentences": 1,
                                    "source_chars": len("seed")}


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
    assert out == {"piece_id": PIECE_ID, "warnings": []}
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
    # S234 stack: the neighbours go along as CONTEXT, same as the T9 sentence repair
    assert "CONTEXT: Opening line here. | The monastery sits above Luang Prabang today. | It is a hidden gem." \
        in req.user_prompt
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
           "published": True, "published_at": datetime(2026, 10, 12, tzinfo=timezone.utc), "exported_at": None}
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
                                                              "content_piece.admin_sentence_%", "content_piece"]
    assert out["admin_edits"][0]["details"] == {"before": "x", "after": ""}
    assert out["published"] is True and out["flagged_sentences"][0]["gates"] == ["F9_brand_style"]
    assert out["cost_by_stage"][0]["stage"] == "t9_repair"
    # AA-802 — published 12/10, admin edit 13/10: the tenant's published post holds the older text
    assert out["exported"] is False
    assert out["tenant_copy"]["older_copy"] is True and out["tenant_copy"]["edits_after_delivery"] == 1


# ── S234 review: sentence boundaries (decimals, abbreviations) ────────────────────────────────

DECIMAL_TEXT = ("Intro here. Over three unforgettable days you will walk through quiet pine forests and terraced "
                "valleys, covering roughly 12.5 km each day with your guide. Then lunch.")


def test_f1_sentence_with_a_decimal_is_cut_whole():
    sent = DECIMAL_TEXT[12:DECIMAL_TEXT.index(" Then")]
    f1 = {"gate": "F1_grounding", "passed": False, "blocking": True, "violations": [
        f"sentence states ['12.5'] not present in the content seed: '{sent[:100]}'"]}
    [item] = cq.flagged_sentences(DECIMAL_TEXT, cq.highlights(DECIMAL_TEXT, [f1], []))
    assert item["text"] == sent
    assert sr.apply_edit(DECIMAL_TEXT, item["start"], item["end"], "") == "Intro here. Then lunch."


def test_locate_sentence_quote_ending_inside_a_decimal_runs_to_the_real_end():
    text = "Intro. It is about 12.5 km of trail along the ridge today. Next one."
    span = sr.locate_sentence(text, "It is about 12.")
    assert text[span[0]:span[1]] == "It is about 12.5 km of trail along the ridge today."


@pytest.mark.parametrize("sentence", [
    "It takes 1.5 hours to reach this hidden gem by boat.",
    "Visit Mt. Fuji, a hidden gem.",
    "The trail climbs 2.5 km to a hidden gem viewpoint.",
    "Visit St. Petersburg, a hidden gem city.",
    "Trips to the U.S. are a hidden gem for some.",
])
def test_phrase_cut_never_leaves_a_fragment(sentence):
    text = f"Intro here. {sentence} Then lunch."
    i = text.index("hidden gem")
    s, e = sr.expand_to_sentence(text, i, i + len("hidden gem"))
    assert text[s:e] == sentence
    assert sr.apply_edit(text, s, e, "") == "Intro here. Then lunch."


def test_is_sentence_end_rules():
    assert sr.is_sentence_end("Done. Next", 4)
    assert sr.is_sentence_end("Done.", 4)
    assert sr.is_sentence_end('He said "go." Then', 11)
    assert sr.is_sentence_end("Done. [R:a1] Next", 4)
    assert not sr.is_sentence_end("12.5 km", 2)
    assert not sr.is_sentence_end("Mt. Fuji", 2)
    assert not sr.is_sentence_end("e.g. this", 3)
    assert sr.is_sentence_end("It costs $12. Then", 12)


# ── S234 review: the replacement gets the tenant-facing clean-up ──────────────────────────────

def test_sanitize_replacement_strips_tags_labels_and_plain_text_markdown():
    assert sr.sanitize_replacement("We visit **Tiger's Nest** [R:abc] [Moment id=m1].", "linkedin") == \
        "We visit Tiger's Nest."
    assert sr.sanitize_replacement("We visit **Tiger's Nest** [F:f1].", "blog") == "We visit **Tiger's Nest**."


@pytest.mark.asyncio
async def test_apply_saves_the_replacement_without_citation_tag_or_markdown():
    conn = _conn(_piece_row())
    await _apply(conn, mode="manual", replacement="**Trail Pulse** builds it. [R:abc]")
    [upd] = _updates(conn)
    assert "Trail Pulse builds it. It is a hidden gem." in upd.args[2]
    assert "[R:" not in upd.args[2] and "**" not in upd.args[2]
    assert json.loads(_audits(conn)[0].args[6])["after"] == "Trail Pulse builds it."


@pytest.mark.asyncio
async def test_apply_saves_with_warnings_and_refuses_only_blocking_checks():
    """S234 review: F12_scope / F13 hygiene on the admin's own text are warnings (saved, returned);
    only F1/F2/brand-forbidden/F11/near-verbatim repeats are a 422."""
    warn = [{"gate": "F12_scope", "violations": ["names 'Mongol', outside this piece's scope: 'x'"],
             "blocking": False}]
    conn = _conn(_piece_row())
    with patch.object(sr, "check_replacement", return_value=warn):
        out = await _apply(conn, mode="manual", replacement="Trail Pulse builds it.")
    assert out["warnings"] == warn and len(_updates(conn)) == 1
    assert json.loads(_audits(conn)[0].args[6])["warnings"] == warn
    conn = _conn(_piece_row())
    with patch.object(sr, "check_replacement", return_value=warn + [
            {"gate": "F11_brand_name", "violations": ["foreign brand"], "blocking": True}]):
        with pytest.raises(HTTPException) as e:
            await _apply(conn, mode="manual", replacement="Trail Pulse builds it.")
    assert e.value.status_code == 422 and "F11_brand_name" in e.value.detail and "F12" not in e.value.detail
    assert not _updates(conn)


@pytest.mark.asyncio
async def test_apply_rejects_a_replacement_that_is_only_a_tag():
    conn = _conn(_piece_row())
    with pytest.raises(HTTPException) as e:
        await _apply(conn, mode="manual", replacement="[R:abc]")
    assert e.value.status_code == 422 and not _updates(conn)


@pytest.mark.asyncio
async def test_suggest_returns_the_cleaned_replacement():
    conn = _conn(_piece_row())
    body = cq.SentenceSuggestBody(start=START, end=START + len(SENT), text=SENT)
    reply = '{"replacements": ["**Trail Pulse** builds it. [R:a1]"]}'
    with patch.object(generate, "LLMClient", return_value=_llm(reply)), patch.object(generate, "record_call_sync"):
        out = await cq.suggest_sentence(PIECE_ID, body, _request(conn), x_admin_secret=SECRET, x_admin_role="admin")
    assert out["replacement"] == "Trail Pulse builds it."


def test_repair_sentences_passes_optional_context():
    items = [{"sentence": "A.", "problem": "p", "context": ["Before.", "After."]}]
    with patch.object(generate, "LLMClient", return_value=_llm()) as m, patch.object(generate, "record_call_sync"):
        generate.repair_sentences(sentences=items, content_seed="seed", channel="blog")
    assert "CONTEXT: Before. | After." in m.return_value.generate.call_args.args[0].user_prompt


# ── S234: regenerate the T8 angles of one request ─────────────────────────────────────────────

REQUEST_ID = str(uuid.uuid4())


def _angle_request_conn(status="approved", goal="inspire", busy=0):
    conn = _conn({"request_id": uuid.UUID(REQUEST_ID), "tenant_id": TENANT_ID, "goal": goal, "status": status})
    conn.fetchval.return_value = busy
    conn.fetch.return_value = [{"idx": 0, "name": "Ruins at dawn", "best_final_style": "s", "recommended": True,
                                "chosen": True}]
    return conn


async def _regenerate(conn, role="admin"):
    with patch("shared.jobs.registry.enqueue", new=AsyncMock(return_value=("job-1", True))) as enq:
        out = await cq.regenerate_angles(REQUEST_ID, _request(conn), x_admin_secret=SECRET,
                                         x_admin_user_id="u1", x_admin_role=role)
    return out, enq


@pytest.mark.asyncio
async def test_regenerate_angles_clones_the_request_and_leaves_the_old_one_alone():
    """S234 review: the old request's option rows are what its pieces join to (wizard resume, My
    Content, the admin drawer) — regenerate must not touch them."""
    conn = _angle_request_conn()
    out, enq = await _regenerate(conn)
    new_id = out["request_id"]
    assert new_id != REQUEST_ID and uuid.UUID(new_id)
    assert out == {"request_id": new_id, "regenerated_from": REQUEST_ID, "job_id": "job-1",
                   "status": "pending_goal", "goal": "inspire"}
    assert "FOR UPDATE" in conn.fetchrow.await_args.args[0]
    args, kw = enq.await_args.args, enq.await_args.kwargs
    assert args[0] is conn and args[1] == "t8_angles"
    assert args[2] == {"request_id": new_id, "tenant_id": str(TENANT_ID), "goal": "inspire",
                       "regenerated_from": REQUEST_ID}
    assert kw["created_by"] == "admin:u1"
    calls = conn.execute.await_args_list
    sqls = [c.args[0] for c in calls]
    # nothing on the old request or its option rows is updated or deleted
    assert not any("UPDATE acp_shared.angle_gate" in q or "DELETE" in q for q in sqls)
    [clone] = [c for c in calls if "INSERT INTO acp_shared.angle_gate_request" in c.args[0]]
    assert clone.args[1:] == (REQUEST_ID, new_id)
    for col in ("atom_id", "trip_id", "channel", "goal", "cta", "subject_id", "route_segment_ids"):
        assert col in clone.args[0]
    assert "'pending_goal'" in clone.args[0]
    [audit] = [c for c in calls if "INSERT INTO acp_shared.audit_log" in c.args[0]]
    assert audit.args[3] == AdminAuditAction.ANGLES_REGENERATED and audit.args[4] == "angle_gate_request"
    assert audit.args[5] == REQUEST_ID
    d = json.loads(audit.args[6])
    assert d["job_id"] == "job-1" and d["previous_status"] == "approved" and d["new_request_id"] == new_id
    assert d["previous_angles"][0]["name"] == "Ruins at dawn"
    # the busy check looks for a regenerate of THIS request still in flight
    assert "regenerated_from" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,goal,busy", [("pending_goal", "inspire", 0), ("pending_goal", None, 0),
                                              ("approved", "inspire", 1)])
async def test_regenerate_angles_409_when_generating_or_writing(status, goal, busy):
    conn = _angle_request_conn(status=status, goal=goal, busy=busy)
    with pytest.raises(HTTPException) as e:
        await _regenerate(conn)
    assert e.value.status_code == 409
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_regenerate_angles_404_and_admin_only():
    conn = _conn(None)
    with pytest.raises(HTTPException) as e:
        await _regenerate(conn)
    assert e.value.status_code == 404
    conn = _angle_request_conn()
    with pytest.raises(HTTPException) as e:
        await _regenerate(conn, role="content")
    assert e.value.status_code == 403
    conn.fetchrow.assert_not_awaited()


def test_set_goal_and_generate_never_overwrites_option_rows():
    """Option rows are written once per request (regenerate clones the request instead)."""
    import inspect
    from services.acp_angle_gate import service as ag
    assert "ON CONFLICT" not in inspect.getsource(ag.set_goal_and_generate)


def test_bff_write_rule_covers_regenerate_angles():
    from pathlib import Path
    rbac = (Path(__file__).resolve().parents[2] / "frontend/lib/rbac.ts").read_text()
    assert '{ prefix: "content-quality/requests/", roles: ["admin"] }' in rbac


@pytest.mark.asyncio
async def test_apply_cut_that_leaves_a_fragment_returns_an_f13_repeat_warning():
    """S234 round 4 review — the final integrity pass does not run on a hand edit, so what the edit
    newly breaks (here a lower-case fragment left by cutting the sentence's first half) is returned as
    a non-blocking F13_repeat warning; the edit is still saved."""
    text = "Opening line here. The valley is wide, and then the valley opens. The camps move."
    sent = "The valley is wide,"
    start = text.index(sent)
    conn = _conn(_piece_row(text=text, writer_input=None))
    out = await _apply(conn, start=start, end=start + len(sent), text=sent)
    [w] = [w for w in out["warnings"] if w["gate"] == "F13_repeat"]
    assert w["blocking"] is False and any("lower-case" in v for v in w["violations"])
    assert len(_updates(conn)) == 1
