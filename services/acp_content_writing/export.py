"""
services.acp_content_writing.export — AA-569: tenant-facing content_piece download (My Content).

Text export is always available (every channel — just `content_text` as-is, already citation-tag
-stripped by run_write_background()/quality_gates.py before persist, nothing further to do here).
HTML export is Blog-only: T9's blog prompt (AA-452, services/acp_content_writing/prompts.py) is
the only channel instructed to write real markdown (`## ` H2 sections, `## FAQ` with `**Q: .../
A:**` pairs) — every other channel's content_text is plain prose, so running it through a
markdown renderer would be a no-op at best and could mis-render a literal "#"/"*" the tenant
actually typed at worst. The router enforces this (400 for html on a non-blog piece), not this
module — this module only renders, it doesn't decide when rendering is appropriate.
"""
from __future__ import annotations

import html as html_escape
from typing import Optional

import markdown

from services.acp_content_writing import ai_marking

_HTML_DOC_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{ai_meta}<title>{title}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; max-width: 680px; margin: 40px auto;
          padding: 0 20px; line-height: 1.65; color: #1F2933; }}
  h1, h2, h3 {{ font-family: -apple-system, 'Segoe UI', sans-serif; color: #1F2933; }}
  a {{ color: #B5791F; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def render_content_text_to_fragment(content_text: str, *, ai: Optional[dict] = None,
                                    meta_tags: bool = True) -> str:
    """AA-613 — convert a blog piece's markdown `content_text` into a bare HTML `<article>`
    fragment (no <html>/<head>/<style> wrapper). This is what gets PUBLISHED to a CMS
    (WordPress etc.) and what a tenant embeds into their own page — the surrounding document
    chrome is the CMS's job, not ours. Same markdown renderer as the document mode below, only
    the wrapper differs.

    AA-1040 — `ai` (the piece's mark, ai_marking.piece_mark()): the `<article>` carries the
    `data-ai-*` attributes and, with `meta_tags`, the `<meta name="ai-*">` tags follow it on one
    line (after the article, so a CMS's paragraph filter never puts them inside the text). Every
    caller with a real piece passes it; None keeps the AA-613 output byte for byte."""
    body = markdown.markdown(content_text, extensions=["extra", "nl2br"])
    if ai is None:
        return f"<article>\n{body}\n</article>"
    tail = f"\n{ai_marking.html_meta_tags(ai)}" if meta_tags else ""
    return f"<article{ai_marking.html_attributes(ai)}>\n{body}\n</article>{tail}"


def render_content_text_to_document(content_text: str, *, title: str = "Content",
                                    ai: Optional[dict] = None) -> str:
    """AA-613 — convert a blog piece's markdown `content_text` into a standalone, openable HTML
    document (full <html>/<head>/<style> chrome). This is the DOWNLOAD/preview mode — a real
    .html file a tenant can open directly, not a snippet meant to be inlined.

    AA-1040 — with `ai`, the `<meta name="ai-*">` tags sit in the `<head>` (where they belong in a
    document) and the `<article>` carries the `data-ai-*` attributes."""
    return _HTML_DOC_TEMPLATE.format(
        title=html_escape.escape(title),
        ai_meta=f"{ai_marking.html_meta_tags(ai)}\n" if ai is not None else "",
        body=render_content_text_to_fragment(content_text, ai=ai, meta_tags=False),
    )


def render_content_text_to_html(content_text: str, *, title: str = "Content") -> str:
    """Backward-compatible alias for the document mode (AA-569's original single-mode name).
    Kept so existing callers/tests don't break; new callers should pick document vs fragment
    explicitly."""
    return render_content_text_to_document(content_text, title=title)


__all__ = [
    "render_content_text_to_html",
    "render_content_text_to_document",
    "render_content_text_to_fragment",
]
