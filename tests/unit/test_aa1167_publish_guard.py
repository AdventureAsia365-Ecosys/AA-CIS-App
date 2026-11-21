"""AA-1167 (OWASP LLM05) — every path that sends AI text out passes the output sanitizer.

1. Behaviour, one test per path, with a piece whose text carries a script, an iframe, an event handler and a
   javascript: link next to normal blog markup: the WordPress post (body, title, meta description), the Facebook
   post, the My Content exports (HTML fragment, HTML document, plain text, through the export route), and the subject
   gate's view of the published HTML. The normal markup survives.
2. Structure (the CI guard): every Markdown render in api/, services/ and shared/ is wrapped in sanitize_html; every
   place that builds a WordPress or social post (BlogContent / SocialPost) or calls an adapter's create_post is a
   registered publish site with a behaviour test above. A new render or publish path fails here until it is wired.
Invented text only."""
import ast
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import ai_marking
from services.acp_content_writing.export import (render_content_text_to_document, render_content_text_to_fragment,
                                                 render_content_text_to_plain)

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("api", "services", "shared")

DIRTY_MD = ("## Corran Reed Market\n\nTraders sell **woven mats** at dawn, see [the map](https://example.org/map).\n\n"
            '<script>steal()</script><iframe src="https://frame.example/x"></iframe>'
            '<p onmouseover="grab()">Hover text</p> <a href="javascript:alert(1)">deal</a>\n\n'
            "- Smoked lake fish\n- Reed hats\n")
BAD = ("<script", "steal()", "<iframe", "frame.example", "onmouseover", "grab()", "javascript:")
GOOD_HTML = ("<h2>Corran Reed Market</h2>", "<strong>woven mats</strong>", 'href="https://example.org/map"',
             "<li>Smoked lake fish</li>", "Hover text")
MARK = {"ai_generated": True, "generator": ai_marking.GENERATOR, "model": "writer-model-x",
        "generated_at": "2026-11-20T08:31:00+00:00", "human_edited": False}


def _clean(text: str) -> None:
    low = text.lower()
    for needle in BAD:
        assert needle.lower() not in low, (needle, text[:400])


# ── 1. behaviour per path ─────────────────────────────────────────────────────────────────────────────────────

def _piece(**kw):
    return {"content_text": DIRTY_MD, "slug": "corran", "angle_name": "Market", "seo_title": "Corran market",
            "meta_description": "Reed mats and lake fish.", "created_at": None, "ai_provenance": None, **kw}


@pytest.mark.asyncio
async def test_wordpress_post_is_sanitized():
    from services.acp_publish import dispatch
    wp = MagicMock()
    wp.create_post = AsyncMock(return_value=MagicMock(post_id=7, post_url="https://example.test/7"))
    piece = _piece(seo_title='Corran <script>x()</script>market <img src=x onerror="y()">',
                   meta_description='Reed mats <iframe src="https://frame.example"></iframe>and fish.')
    with patch.object(dispatch, "_wordpress", return_value=wp):
        await dispatch.publish_to_channel("blog", {}, piece, "k")
    content = wp.create_post.call_args.args[0]
    _clean(content.content_html)
    for good in GOOD_HTML:
        assert good in content.content_html
    assert '<article data-ai-generated="true"' in content.content_html     # our wrapper and mark stay
    for text in (content.title, content.seo_title, content.seo_meta):
        assert "<" not in text and "onerror" not in text and "frame.example" not in text
    assert content.title == "Corran market" and content.seo_meta == "Reed mats and fish."


@pytest.mark.asyncio
async def test_facebook_post_is_sanitized():
    from services.acp_publish import dispatch
    fb = MagicMock()
    fb.create_post = AsyncMock(return_value=MagicMock(post_id="1_2", post_url="https://example.test/1_2"))
    with patch.object(dispatch, "_facebook", return_value=fb):
        await dispatch.publish_to_channel("facebook", {}, _piece(), "k")
    message = fb.create_post.call_args.args[0].message
    _clean(message)
    assert "Traders sell **woven mats** at dawn" in message and "Hover text" in message
    assert dispatch.post_text("facebook", _piece()) == message          # the stored text is the posted text


def test_export_renders_are_sanitized():
    frag = render_content_text_to_fragment(DIRTY_MD, ai=MARK)
    doc = render_content_text_to_document(DIRTY_MD, title="Post", ai=MARK)
    plain = render_content_text_to_plain(DIRTY_MD, ai=MARK)
    for html in (frag, doc):
        _clean(html)
        for good in GOOD_HTML:
            assert good in html
    _clean(plain)
    assert "Traders sell **woven mats** at dawn" in plain


@pytest.mark.asyncio
async def test_my_content_export_route_is_sanitized():
    from api.routers import v1_content_writing
    request = MagicMock()
    request.app.state.pool = MagicMock()
    piece = {"ready_state": "ready", "channel": "blog", "content_text": DIRTY_MD, "ai": MARK}
    with patch.object(v1_content_writing.service, "fetch_piece", new=AsyncMock(return_value=piece)), \
            patch.object(v1_content_writing, "write_audit_log", new=AsyncMock()):
        for fmt, mode in (("html", "fragment"), ("html", "document"), ("text", "document")):
            resp = await v1_content_writing.export_piece(uuid.uuid4(), request, tenant={"sub": str(uuid.uuid4())},
                                                         format=fmt, mode=mode)
            _clean(resp.body.decode())


def test_subject_gate_reads_the_sanitized_render():
    from services.acp_shared import subject_gate
    html = subject_gate.rendered_html(DIRTY_MD)
    _clean(html)
    assert html.startswith("<h2>Corran Reed Market</h2>")


def test_clean_blog_markdown_publishes_unchanged_by_the_sanitizer():
    import markdown
    md = "## Corran\n\nA walk past the **reed** stalls.\n\n- One\n- Two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    raw = markdown.markdown(md, extensions=["extra", "nl2br"])
    assert render_content_text_to_fragment(md) == f"<article>\n{raw}\n</article>"   # the AA-613 shape, unchanged


# ── 2. structure: no render or publish path around the sanitizer ──────────────────────────────────────────────

# Every place that builds an outgoing post or calls a channel adapter's create_post, with the test that proves it
# sends sanitized text. A new one fails until it is listed here with its test.
PUBLISH_SITES = {
    "services/acp_publish/dispatch.py::publish_to_channel": "test_wordpress_post_is_sanitized, "
                                                            "test_facebook_post_is_sanitized",
}
_POST_TYPES = {"BlogContent", "SocialPost"}


def _py_files():
    for d in SCAN_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if "migrations" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else ""
        return f"{base}.{f.attr}" if base else f.attr
    return f.id if isinstance(f, ast.Name) else ""


def test_every_markdown_render_is_sanitized():
    found = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "markdown.markdown":
                parent = parents.get(node)
                if not (isinstance(parent, ast.Call) and _call_name(parent).endswith("sanitize_html")):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert found == [], "a Markdown render not wrapped in sanitize_html: " + ", ".join(found)


def test_every_publish_site_is_registered():
    found = set()
    for path in _py_files():
        rel = str(path.relative_to(ROOT))
        if rel.startswith("services/acp_s4_blog/cms/") or rel == "services/acp_publish/facebook.py":
            continue                                    # the adapters themselves (they post what they are given)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in _functions(tree):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and (_call_name(node) in _POST_TYPES
                                                   or _call_name(node).endswith("create_post")):
                    found.add(f"{rel}::{fn.name}")
    assert found == set(PUBLISH_SITES), f"unregistered publish sites: {sorted(found - set(PUBLISH_SITES))}"
