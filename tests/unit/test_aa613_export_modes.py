"""AA-613 — export.py gains two explicit HTML render modes:
  - document: full standalone <!DOCTYPE html> file (tenant download/preview), = the old
    render_content_text_to_html behavior.
  - fragment: bare <article> (published to a CMS / embedded), also used by v1_publish.py to fix
    the markdown-into-content_html bug.
"""
from services.acp_content_writing.export import (
    render_content_text_to_document,
    render_content_text_to_fragment,
    render_content_text_to_html,
)

_MD = "## A Title\n\nSome **bold** body text."


class TestFragmentMode:
    def test_renders_markdown_inside_bare_article(self):
        frag = render_content_text_to_fragment(_MD)
        assert frag.startswith("<article>")
        assert frag.rstrip().endswith("</article>")
        assert "<h2>A Title</h2>" in frag
        assert "<strong>bold</strong>" in frag

    def test_no_document_chrome(self):
        frag = render_content_text_to_fragment(_MD)
        assert "<!DOCTYPE html>" not in frag
        assert "<html" not in frag
        assert "<style>" not in frag


class TestDocumentMode:
    def test_is_standalone_document_wrapping_the_fragment(self):
        doc = render_content_text_to_document(_MD, title="My Post")
        assert doc.strip().startswith("<!DOCTYPE html>")
        assert "<title>My Post</title>" in doc
        assert "<article>" in doc  # fragment is nested inside the document
        assert "<h2>A Title</h2>" in doc

    def test_title_html_escaped(self):
        doc = render_content_text_to_document("x", title="<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in doc
        assert "&lt;script&gt;" in doc


class TestBackwardCompatAlias:
    def test_html_alias_equals_document_mode(self):
        assert render_content_text_to_html(_MD, title="T") == render_content_text_to_document(_MD, title="T")
