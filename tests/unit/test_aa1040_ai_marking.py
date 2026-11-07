"""AA-1040 — the machine-readable mark of AI-generated content (EU AI Act Art. 50(2), root ADR 0019 d19).

Checks:
  1. ai_marking: the mark of a stamped piece and of a piece older than AA-1040, attribute / meta escaping, the meta
     tags on one line, the IPTC digital source type, the download headers;
  2. export: the fragment carries data-ai-* on <article> and the <meta> tags after it; the document puts them in the
     <head>; no `ai` keeps the AA-613 output byte for byte;
  3. publish: the WordPress post body carries the mark; the Facebook message is the piece text unchanged;
  4. the tenant projection carries `ai` and still nothing of writer_input;
  5. _finalize_piece stamps writer_input.ai_provenance with the ledger's writer model (best-effort), never for a
     failed piece; the export route sends the mark as headers.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import ai_marking, service
from services.acp_content_writing.export import render_content_text_to_document, render_content_text_to_fragment

CREATED = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
MD = "## Lantern Valley\n\nA quiet walk past the old salt mill."
MARK = {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": "writer-model-x",
        "generated_at": "2026-10-20T08:31:00+00:00", "human_edited": False}


# ── 1. the mark ───────────────────────────────────────────────────────────────────────────────────────────────

def test_stamp_is_ai_generated_with_model_and_time():
    s = ai_marking.stamp("writer-model-x", CREATED)
    assert s == {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": "writer-model-x",
                 "generated_at": CREATED.isoformat()}
    assert ai_marking.stamp(None)["model"] is None and ai_marking.stamp("")["generated_at"]


def test_piece_mark_of_an_old_piece_is_still_ai_generated():
    m = ai_marking.piece_mark(CREATED, None)
    assert m == {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": None,
                 "generated_at": CREATED.isoformat(), "human_edited": False}
    assert ai_marking.piece_mark(CREATED, "not json")["ai_generated"] is True


def test_piece_mark_reads_the_stored_provenance_json_and_the_edit():
    prov = json.dumps({**ai_marking.stamp("writer-model-x", CREATED), "human_edited_at": "2026-10-21T10:00:00Z"})
    m = ai_marking.piece_mark(datetime(2020, 1, 1, tzinfo=timezone.utc), prov)
    assert m["model"] == "writer-model-x" and m["generated_at"] == CREATED.isoformat() and m["human_edited"] is True


def test_attributes_and_meta_tags_escape_and_stay_on_one_line():
    m = {**MARK, "model": 'x"><script>'}
    attrs = ai_marking.html_attributes(m)
    assert attrs.startswith(' data-ai-generated="true" data-ai-generator="Adventure Asia Content studio"')
    assert 'data-ai-model="x&quot;&gt;&lt;script&gt;"' in attrs and "<script>" not in attrs
    assert 'data-ai-human-edited="false"' in attrs
    tags = ai_marking.html_meta_tags(m)
    assert "\n" not in tags and "<script>" not in tags
    assert tags.startswith('<meta name="ai-generated" content="true"><meta name="ai-generator"')
    assert f'<meta name="digital-source-type" content="{ai_marking.IPTC_DIGITAL_SOURCE_TYPE}">' in tags
    assert "trainedAlgorithmicMedia" in ai_marking.IPTC_DIGITAL_SOURCE_TYPE


def test_unknown_model_is_left_out_not_written_as_none():
    m = {**MARK, "model": None}
    assert "data-ai-model" not in ai_marking.html_attributes(m)
    assert "ai-model" not in ai_marking.html_meta_tags(m)
    assert "X-AI-Model" not in ai_marking.response_headers(m)


def test_response_headers():
    assert ai_marking.response_headers(MARK) == {"X-AI-Generated": "true", "X-AI-Generator": ai_marking.GENERATOR,
                                                 "X-AI-Model": "writer-model-x"}


# ── 2. export ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_fragment_without_ai_is_unchanged():
    assert render_content_text_to_fragment(MD).startswith("<article>\n<h2>")
    assert render_content_text_to_fragment(MD).endswith("</article>")


def test_fragment_with_ai_marks_the_article_then_the_meta_tags():
    frag = render_content_text_to_fragment(MD, ai=MARK)
    assert frag.startswith('<article data-ai-generated="true"')
    assert "<h2>Lantern Valley</h2>" in frag
    head, _, tail = frag.partition("</article>")
    assert "<meta" not in head, "no <meta> inside the text, a CMS paragraph filter would wrap it"
    assert tail == "\n" + ai_marking.html_meta_tags(MARK)


def test_document_with_ai_puts_meta_in_head_and_attributes_on_article():
    doc = render_content_text_to_document(MD, title="Post", ai=MARK)
    head, _, body = doc.partition("</head>")
    assert '<meta name="ai-generated" content="true">' in head and '<meta name="ai-model"' in head
    assert '<article data-ai-generated="true"' in body and "<meta" not in body
    assert render_content_text_to_document(MD, title="Post").count("<meta") == 1  # charset only, as before


# ── 3. publish ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wordpress_post_body_carries_the_mark():
    from services.acp_publish import dispatch
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://example.test/7"))
    prov = json.dumps(ai_marking.stamp("writer-model-x", CREATED))
    piece = {"content_text": MD, "slug": "s", "angle_name": "A", "seo_title": "T", "meta_description": "M",
             "created_at": CREATED, "ai_provenance": prov}
    with patch.object(dispatch, "_wordpress", return_value=wp):
        await dispatch.publish_to_channel("blog", {}, piece, "k")
    html = wp.create_post.call_args.args[0].content_html
    assert '<article data-ai-generated="true"' in html and 'data-ai-model="writer-model-x"' in html
    assert '<meta name="ai-generated" content="true">' in html


@pytest.mark.asyncio
async def test_wordpress_piece_without_provenance_still_marked():
    from services.acp_publish import dispatch
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://example.test/7"))
    piece = {"content_text": MD, "slug": "s", "angle_name": "A", "seo_title": None, "meta_description": None}
    with patch.object(dispatch, "_wordpress", return_value=wp):
        await dispatch.publish_to_channel("blog", {}, piece, "k")
    html = wp.create_post.call_args.args[0].content_html
    assert 'data-ai-generated="true"' in html and "data-ai-model" not in html


@pytest.mark.asyncio
async def test_facebook_message_is_the_piece_text_unchanged():
    from services.acp_publish import dispatch
    fb = MagicMock()
    fb.create_post = AsyncMock(return_value=MagicMock(post_id="1_2", post_url="https://example.test/1_2"))
    piece = {"content_text": "A sunrise boat on the lake.", "created_at": CREATED, "ai_provenance": None}
    with patch.object(dispatch, "_facebook", return_value=fb):
        await dispatch.publish_to_channel("facebook", {}, piece, "k")
    assert fb.create_post.call_args.args[0].message == "A sunrise boat on the lake."


def test_publish_sql_reads_created_at_and_only_the_provenance_key():
    from api.routers import v1_publish
    sql = v1_publish._PUBLISH_PIECE_SQL
    assert "cp.created_at" in sql and "cp.writer_input -> 'ai_provenance' AS ai_provenance" in sql
    assert "cp.writer_input," not in sql


# ── 4. tenant projection ──────────────────────────────────────────────────────────────────────────────────────

def _row(**kw):
    return {"piece_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "angle_gate_request_id": uuid.uuid4(),
            "attempt_number": 1, "content_text": "Text.", "status": "approved", "held_reason": None,
            "gate_ledger": "[]", "repair_log": "[]", "created_at": CREATED, "seo_title": None,
            "meta_description": None, "slug": None, "channel": "blog", "route_hub_name": None,
            "route_segment_count": None, "flags": "[]", **kw}


def test_tenant_projection_carries_the_mark():
    out = service._tenant_safe_piece(_row(ai_provenance=json.dumps(ai_marking.stamp("writer-model-x", CREATED))))
    assert out["ai"]["ai_generated"] is True and out["ai"]["model"] == "writer-model-x"
    assert "writer_input" not in out and "ai_provenance" not in out


def test_tenant_projection_of_a_row_without_the_column():
    out = service._tenant_safe_piece(_row())
    assert out["ai"] == ai_marking.piece_mark(CREATED.isoformat(), None)


def test_tenant_reads_select_the_provenance_key():
    for sql in (service._FETCH_PIECE_QUERY, service._LATEST_PIECE_FOR_OPTION_QUERY, service._PIECE_EDIT_SQL):
        assert "writer_input -> 'ai_provenance' AS ai_provenance" in sql
    edit = service._PIECE_EDIT_SQL
    assert "human_edited_at" in edit and "WHEN writer_input IS NULL THEN NULL" in edit


# ── 5. finalize + export route ────────────────────────────────────────────────────────────────────────────────

def _finalize_pool(fetchval_side_effect):
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    conn.fetchrow = AsyncMock(return_value=_row())
    conn.execute = AsyncMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool, conn


async def _finalize(pool, status="approved", writer_input=None):
    with patch.object(service, "write_audit_log", new=AsyncMock()):
        await service._finalize_piece(pool, piece_id=uuid.uuid4(), attempt_number=1, content_text="Text.",
                                      status=status, held_reason=None, gate_ledger=[], repair_log=[], flags=[],
                                      writer_input=writer_input if writer_input is not None else {"seed": "s"})


@pytest.mark.asyncio
async def test_finalize_stamps_the_writer_model():
    answer = {"answered_model": "writer-model-x", "fallback_used": False, "route_position": 0,
              "connection_key": "conn-a", "effort": None}
    pool, conn = _finalize_pool([uuid.uuid4(), json.dumps(answer)])
    await _finalize(pool)
    stored = json.loads(conn.fetchrow.call_args.args[15])
    assert stored["seed"] == "s"
    prov = stored[ai_marking.PROVENANCE_KEY]
    assert prov["ai_generated"] is True and prov["model"] == "writer-model-x" and prov["generated_at"]
    # AA-900 W0: one ledger read (WRITER_ANSWER_SQL) gives the mark's model and the admin-only answer stamp
    assert conn.fetchval.call_args_list[1].args[0] == ai_marking.WRITER_ANSWER_SQL
    assert stored[ai_marking.ANSWER_KEY] == answer
    assert set(prov) & {"fallback_used", "route_position", "connection_key"} == set()   # never in the tenant mark


@pytest.mark.asyncio
async def test_finalize_marks_a_fallback_answer():
    answer = {"answered_model": "writer-model-y", "fallback_used": None, "route_position": 1}
    pool, conn = _finalize_pool([uuid.uuid4(), json.dumps(answer)])
    await _finalize(pool, status="held")
    stored = json.loads(conn.fetchrow.call_args.args[15])
    assert stored[ai_marking.ANSWER_KEY]["fallback_used"] is True        # a rung > 0 answered
    assert stored[ai_marking.PROVENANCE_KEY]["model"] == "writer-model-y"


@pytest.mark.asyncio
async def test_finalize_keeps_going_when_the_ledger_cannot_be_read():
    pool, conn = _finalize_pool([uuid.uuid4(), RuntimeError("ledger down")])
    await _finalize(pool, status="held")
    assert json.loads(conn.fetchrow.call_args.args[15])[ai_marking.PROVENANCE_KEY]["model"] is None


@pytest.mark.asyncio
async def test_finalize_of_a_failed_piece_stamps_nothing():
    pool, conn = _finalize_pool([])
    await _finalize(pool, status="failed")
    conn.fetchval.assert_not_called()
    assert ai_marking.PROVENANCE_KEY not in json.loads(conn.fetchrow.call_args.args[15])


@pytest.mark.asyncio
async def test_export_route_sends_the_mark_as_headers():
    from api.routers import v1_content_writing
    request = MagicMock()
    request.app.state.pool = MagicMock()
    piece = {"ready_state": "ready", "channel": "blog", "content_text": MD, "ai": MARK}
    with patch.object(v1_content_writing.service, "fetch_piece", new=AsyncMock(return_value=piece)), \
            patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
        html = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                     format="html", mode="fragment")
        text = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                     format="text", mode="document")
    assert html.headers["x-ai-generated"] == "true" and html.headers["x-ai-model"] == "writer-model-x"
    assert b'<article data-ai-generated="true"' in html.body and b'<meta name="ai-generated"' in html.body
    assert text.headers["x-ai-generated"] == "true" and text.body == MD.encode()


# ── 6. T2 tenant tour versions (AA-1040) ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,edited", [("ai_generated", False), ("re_rewrite", False), ("tenant_edit", True)])
def test_version_mark_follows_edit_source(source, edited):
    m = ai_marking.version_mark(source, CREATED)
    assert m["ai_generated"] is True and m["human_edited"] is edited
    assert m["generated_at"] == (None if edited else CREATED.isoformat())


def test_project_version_carries_the_mark_and_no_hidden_key():
    from api.routers._tenant_projection import forbidden_keys, project_version
    out = project_version({"id": uuid.uuid4(), "status": "ai_generated", "edit_source": "tenant_edit",
                           "created_at": CREATED, "generation_meta": {"x": 1}})
    assert out["ai"]["human_edited"] is True and "generation_meta" not in out
    assert forbidden_keys(out) == []
    assert "ai" not in project_version({"id": uuid.uuid4(), "status": "approved"})  # no edit_source selected


def test_docx_properties_carry_the_mark():
    props = ai_marking.docx_properties(ai_marking.version_mark("ai_generated", CREATED))
    assert props["keywords"] == "ai-generated"
    assert props["comments"].startswith("AI-generated content; generator=Adventure Asia Content studio")
    assert f"generated-at={CREATED.isoformat()}" in props["comments"] and "human-edited=false" in props["comments"]


@pytest.mark.asyncio
async def test_docx_export_file_has_the_ai_properties():
    import io
    from docx import Document
    from api.routers import v1_tours
    row = {"version_number": 2, "rewritten_content": {"name": "Lantern Valley walk"}, "rewrite_language": "en-US",
           "edit_source": "ai_generated", "created_at": CREATED, "tour_id": uuid.uuid4(), "aa_name": "Lantern Valley",
           "aa_subtitle": "", "aa_summary": "A quiet walk.", "aa_highlights": "[]", "aa_itineraries": "",
           "country": "Nowhereland", "duration": "2 days", "inclusions": None, "exclusions": None}
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    class _Scoped:
        async def __aenter__(self):
            return conn, True

        async def __aexit__(self, *a):
            return False

    request = MagicMock()
    request.app.state.pool = MagicMock()
    with patch.object(v1_tours, "acquire_scoped_conn", return_value=_Scoped()), \
            patch.object(v1_tours, "write_audit_log", new=AsyncMock()):
        resp = await v1_tours.export_version_docx(str(uuid.uuid4()), request, tenant={"sub": str(uuid.uuid4())})
    body = b"".join([chunk async for chunk in resp.body_iterator])
    props = Document(io.BytesIO(body)).core_properties
    assert props.keywords == "ai-generated" and "generator=Adventure Asia Content studio" in props.comments
    # S272: the DOCX download carries the X-AI-* headers too (the studio BFF passes them to the browser).
    assert resp.headers["x-ai-generated"] == "true"
    assert resp.headers["x-ai-generator"] == "Adventure Asia Content studio"
    assert resp.headers["content-disposition"].startswith("attachment; filename=tour_")


# ── 6. the A-series Master catalog text (review fix) ──────────────────────────────────────────────────────────

LATER = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)


def test_tour_mark_uses_the_last_master_publish():
    m = ai_marking.tour_mark(LATER, CREATED)
    assert m == {"ai_generated": True, "generator": ai_marking.CATALOG_GENERATOR, "model": None,
                 "generated_at": LATER.isoformat(), "human_edited": None}
    assert ai_marking.tour_mark(None, CREATED)["generated_at"] == CREATED.isoformat()
    assert ai_marking.tour_mark(None, None)["generated_at"] is None


def test_project_tour_marks_master_text_and_hides_the_score():
    from api.routers._tenant_projection import forbidden_keys, project_tour
    row = {"id": uuid.uuid4(), "aa_name": "Lantern Valley walk", "aa_summary": "A quiet walk.",
           "quality_score": 8.1, "quality_score_id": uuid.uuid4(), "published_at": CREATED, "updated_at": LATER}
    out = project_tour(row)
    assert out["ai"]["ai_generated"] is True and out["ai"]["generated_at"] == LATER.isoformat()
    assert "quality_score" not in out and forbidden_keys(out) == []
    # a row without any Master text column (an id lookup) gets no mark
    assert "ai" not in project_tour({"id": uuid.uuid4(), "published_at": CREATED})


def test_pool_listing_selects_the_last_master_publish():
    from api.routers import v1_tours
    assert "pt.published_at, pt.updated_at," in v1_tours._POOL_ROWS_SQL
