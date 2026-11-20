"""AA-1168 — EU AI Act Art. 50: the visible disclosure and the machine-readable mark on every publish channel.

AA-1040 marked the HTML (data-ai-* on <article>, <meta name="ai-*">) and the download headers. This adds:
  1. the disclosure helpers: one wording, the HTML paragraph, the social message, the plain-text trailer with one
     parseable `AI-Content:` line, the WordPress REST post meta keys;
  2. HTML exports and the WordPress body end the <article> with the disclosure paragraph (no `ai` = AA-613 output);
  3. the plain-text export carries the disclosure and the mark in its body (a saved .txt keeps no headers);
  4. WordPress: the post sends the mark as REST post meta next to the Yoast keys;
  5. Facebook: the post text ends with the disclosure and #AIgenerated, and the retry lookup (AA-726) searches for the
     same message, whatever the piece's provenance says.
Invented text only.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import ai_marking
from services.acp_content_writing.export import (
    render_content_text_to_document,
    render_content_text_to_fragment,
    render_content_text_to_plain,
)
from services.acp_s4_blog.cms.base import BlogContent
from services.acp_s4_blog.cms.wordpress import WordPressAdapter
from tests.unit.safe_http_fakes import TARGET, fake_request

CREATED = datetime(2026, 10, 20, 8, 30, tzinfo=timezone.utc)
MD = "## Lantern Valley\n\nA quiet walk past the old salt mill."
MARK = {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": "writer-model-x",
        "generated_at": "2026-10-20T08:31:00+00:00", "human_edited": False}
DISCLOSURE_P = ('<p class="ai-disclosure" data-ai-disclosure="true"><em>'
                'This content was created with the help of AI.</em></p>')


def _parse_mark_line(line: str) -> dict:
    assert line.startswith(ai_marking.TEXT_MARK_LABEL + " ")
    pairs = line[len(ai_marking.TEXT_MARK_LABEL) + 1:].split("; ")
    return dict(p.split("=", 1) for p in pairs)


# ── 1. helpers ────────────────────────────────────────────────────────────────────────────────────────────────

def test_disclosure_html_is_one_paragraph_with_its_own_data_attribute():
    assert ai_marking.disclosure_html(MARK) == DISCLOSURE_P
    assert ai_marking.disclosure_html() == DISCLOSURE_P   # same wording whatever the mark says


def test_social_message_appends_disclosure_and_tag_after_a_blank_line():
    msg = ai_marking.social_message("A sunrise boat on the lake.  \n", MARK)
    assert msg == "A sunrise boat on the lake.\n\nThis content was created with the help of AI. #AIgenerated"


def test_social_message_does_not_depend_on_the_mark():
    """The retry lookup matches the message exactly: a mark read later (an edit stamped human_edited_at, a model
    filled in) must not change the text."""
    other = {**MARK, "model": None, "human_edited": True, "generated_at": None}
    assert ai_marking.social_message("x", MARK) == ai_marking.social_message("x", other)


def test_text_mark_line_parses_and_cleans_values():
    m = {**MARK, "model": "odd;model\nname"}
    line = ai_marking.text_mark_line(m)
    assert "\n" not in line
    fields = _parse_mark_line(line)
    assert fields == {"ai-generated": "true", "ai-generator": ai_marking.GENERATOR, "ai-model": "odd,model name",
                      "ai-generated-at": "2026-10-20T08:31:00+00:00", "ai-human-edited": "false",
                      "digital-source-type": ai_marking.IPTC_DIGITAL_SOURCE_TYPE}


def test_text_mark_line_leaves_out_an_unknown_model_and_time():
    fields = _parse_mark_line(ai_marking.text_mark_line({**MARK, "model": None, "generated_at": None,
                                                         "human_edited": True}))
    assert "ai-model" not in fields and "ai-generated-at" not in fields and fields["ai-human-edited"] == "true"


def test_wordpress_post_meta_keys_and_string_values():
    meta = ai_marking.wordpress_post_meta(MARK)
    assert meta == {"ai_generated": "true", "ai_generator": ai_marking.GENERATOR, "ai_model": "writer-model-x",
                    "ai_generated_at": "2026-10-20T08:31:00+00:00", "ai_human_edited": "false",
                    "ai_digital_source_type": ai_marking.IPTC_DIGITAL_SOURCE_TYPE}
    assert not any(k.startswith("_") for k in meta), "a leading _ is protected meta the REST API never writes"
    assert "ai_model" not in ai_marking.wordpress_post_meta({**MARK, "model": None})


# ── 2. HTML exports / WordPress body ──────────────────────────────────────────────────────────────────────────

def test_fragment_ends_the_article_with_the_disclosure():
    frag = render_content_text_to_fragment(MD, ai=MARK)
    article, _, tail = frag.partition("</article>")
    assert article.rstrip().endswith(DISCLOSURE_P)
    assert frag.count(DISCLOSURE_P) == 1
    assert "<meta" not in article and tail == "\n" + ai_marking.html_meta_tags(MARK)


def test_fragment_without_ai_has_no_disclosure():
    assert "ai-disclosure" not in render_content_text_to_fragment(MD)


def test_document_shows_the_disclosure_in_the_body():
    doc = render_content_text_to_document(MD, title="Post", ai=MARK)
    head, _, body = doc.partition("</head>")
    assert DISCLOSURE_P in body and "ai-disclosure" not in head
    assert "ai-disclosure" not in render_content_text_to_document(MD, title="Post")


# ── 3. plain-text export ──────────────────────────────────────────────────────────────────────────────────────

def test_plain_text_keeps_the_text_then_the_disclosure_and_the_mark():
    out = render_content_text_to_plain(MD + "\n\n", ai=MARK)
    text, _, trailer = out.partition("\n\n---\n")
    assert text == MD
    disclosure, mark_line, end = trailer.split("\n")
    assert disclosure == ai_marking.DISCLOSURE_TEXT and end == ""
    assert _parse_mark_line(mark_line)["ai-model"] == "writer-model-x"


@pytest.mark.asyncio
async def test_export_route_text_body_and_html_carry_the_disclosure():
    from api.routers import v1_content_writing
    request = MagicMock()
    request.app.state.pool = MagicMock()
    piece = {"ready_state": "ready", "channel": "facebook", "content_text": "A short post.", "ai": MARK}
    blog = {**piece, "channel": "blog", "content_text": MD}
    with patch.object(v1_content_writing.service, "fetch_piece", new=AsyncMock(side_effect=[piece, blog])), \
            patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
        text = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                     format="text", mode="document")
        html = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                     format="html", mode="document")
    body = text.body.decode()
    assert body.startswith("A short post.\n\n---\n" + ai_marking.DISCLOSURE_TEXT + "\n")
    assert ai_marking.text_mark_line(MARK) in body and text.headers["x-ai-generated"] == "true"
    assert DISCLOSURE_P.encode() in html.body


@pytest.mark.asyncio
async def test_export_route_text_of_a_piece_without_a_mark_is_still_marked():
    from api.routers import v1_content_writing
    request = MagicMock()
    request.app.state.pool = MagicMock()
    piece = {"ready_state": "ready", "channel": "facebook", "content_text": "A short post.", "created_at": CREATED}
    with patch.object(v1_content_writing.service, "fetch_piece", new=AsyncMock(return_value=piece)), \
            patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
        text = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                     format="text", mode="document")
    fields = _parse_mark_line(text.body.decode().rstrip("\n").rsplit("\n", 1)[-1])
    assert fields["ai-generated"] == "true" and fields["ai-generated-at"] == CREATED.isoformat()


# ── 4. WordPress post meta ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wordpress_publish_sends_the_mark_as_post_meta_and_the_disclosure():
    from services.acp_publish import dispatch
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://example.test/7"))
    piece = {"content_text": MD, "slug": "s", "angle_name": "A", "seo_title": "T", "meta_description": "M",
             "created_at": CREATED, "ai_provenance": json.dumps(ai_marking.stamp("writer-model-x", CREATED))}
    with patch.object(dispatch, "_wordpress", return_value=wp):
        await dispatch.publish_to_channel("blog", {}, piece, "k")
    content = wp.create_post.call_args.args[0]
    assert content.meta == ai_marking.wordpress_post_meta(dispatch.piece_ai_mark(piece))
    assert content.meta["ai_model"] == "writer-model-x"
    assert DISCLOSURE_P in content.content_html


@pytest.mark.asyncio
async def test_wordpress_adapter_merges_the_meta_with_the_seo_keys():
    adapter = WordPressAdapter(wp_url="https://tenant-blog.example", username="editor", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="s", seo_title="T", seo_meta="M", status="publish",
                          meta={"ai_generated": "true"})
    fake = fake_request(201, body_text=json.dumps({"id": 1, "link": "https://tenant-blog.example/1"}))
    with patch(TARGET, fake):
        await adapter.create_post(content)
    sent = fake.call_args.kwargs["json"]["meta"]
    assert sent == {"_yoast_wpseo_title": "T", "_yoast_wpseo_metadesc": "M", "ai_generated": "true"}


@pytest.mark.asyncio
async def test_wordpress_adapter_without_meta_sends_the_seo_keys_only():
    adapter = WordPressAdapter(wp_url="https://tenant-blog.example", username="editor", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="s", seo_title="T", seo_meta="M")
    fake = fake_request(201, body_text=json.dumps({"id": 1, "link": "https://tenant-blog.example/1"}))
    with patch(TARGET, fake):
        await adapter.create_post(content)
    assert fake.call_args.kwargs["json"]["meta"] == {"_yoast_wpseo_title": "T", "_yoast_wpseo_metadesc": "M"}


# ── 5. Facebook ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_facebook_retry_lookup_searches_for_the_posted_message():
    from services.acp_publish import dispatch
    fb = MagicMock()
    fb.create_post = AsyncMock(return_value=MagicMock(post_id="1_2", post_url="https://example.test/1_2"))
    fb.find_recent_post = AsyncMock(return_value=None)
    piece = {"content_text": "A sunrise boat on the lake.", "created_at": CREATED, "ai_provenance": None}
    with patch.object(dispatch, "_facebook", return_value=fb):
        assert await dispatch.find_remote("facebook", {}, piece, "k") is None
        await dispatch.publish_to_channel("facebook", {}, piece, "k")
    posted = fb.create_post.call_args.args[0].message
    assert fb.find_recent_post.call_args.args[0] == posted == dispatch.facebook_message(piece)
    assert posted.endswith(f"{ai_marking.DISCLOSURE_TEXT} {ai_marking.SOCIAL_DISCLOSURE_TAG}")
