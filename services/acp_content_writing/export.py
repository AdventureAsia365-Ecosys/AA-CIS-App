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

import markdown

_HTML_DOC_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
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


def render_content_text_to_fragment(content_text: str) -> str:
    """AA-613 — convert a blog piece's markdown `content_text` into a bare HTML `<article>`
    fragment (no <html>/<head>/<style> wrapper). This is what gets PUBLISHED to a CMS
    (WordPress etc.) and what a tenant embeds into their own page — the surrounding document
    chrome is the CMS's job, not ours. Same markdown renderer as the document mode below, only
    the wrapper differs."""
    body = markdown.markdown(content_text, extensions=["extra", "nl2br"])
    return f"<article>\n{body}\n</article>"


def render_content_text_to_document(content_text: str, *, title: str = "Content") -> str:
    """AA-613 — convert a blog piece's markdown `content_text` into a standalone, openable HTML
    document (full <html>/<head>/<style> chrome). This is the DOWNLOAD/preview mode — a real
    .html file a tenant can open directly, not a snippet meant to be inlined."""
    return _HTML_DOC_TEMPLATE.format(
        title=html_escape.escape(title),
        body=render_content_text_to_fragment(content_text),
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
