"""AA-1167 (OWASP LLM05) — services/ai_safety/html_sanitizer.py: the allow-list rebuild of AI HTML and the plain-text
cleaner. Normal blog markup (what Python-Markdown writes for a T9 blog piece) passes byte for byte; scripts, frames,
event handlers, javascript: / data: links, styles and classes go. Invented text only."""
import markdown
import pytest

from services.ai_safety.html_sanitizer import (ALLOWED_TAGS, DROP_WITH_CONTENT, safe_url, sanitize_html,
                                               sanitize_plain_text)

BLOG_MD = """## Morning at Corran Market

Walk the **old quay** at *dawn*, then see [the reed market](https://example.org/corran?day=1&lang=en).
Boats arrive < 5 km from the shore & leave by noon. It's "quiet" then.

- Woven reed mats
- Smoked lake fish

1. Breakfast on the quay
2. Boat to Isle Marrow

> Traders call out prices from flat boats.

| Stop | Time |
|:---|---:|
| Corran | Morning |

A `code` word, an entity &copy; and a numbered one &#169;.

## FAQ

**Q: When does the market open?**
A: At first light.[^1]

[^1]: Times change with the season.

---
"""


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=["extra", "nl2br"])


def test_normal_blog_markup_survives_byte_for_byte():
    html = _md(BLOG_MD)
    assert "<table>" in html and 'class="footnote"' in html and "<blockquote>" in html   # the fixture covers them
    assert sanitize_html(html) == html


def test_sanitize_is_idempotent():
    dirty = _md(BLOG_MD) + '<script>x()</script><a href="javascript:y()" onclick="z()">l</a><div style="a">d</div>'
    once = sanitize_html(dirty)
    assert sanitize_html(once) == once


@pytest.mark.parametrize("dirty,gone", [
    ('<p>Hi</p><script>alert(1)</script>', ["<script", "alert(1)"]),
    ('<p>Hi</p><SCRIPT type="text/javascript">alert(1)</SCRIPT >', ["<script", "alert(1)"]),
    ('<iframe src="https://frame.example/x"></iframe><p>t</p>', ["<iframe", "frame.example"]),
    ('<p onmouseover="steal()">t</p>', ["onmouseover", "steal()"]),
    ('<img src="x" onerror="alert(1)">', ["<img", "onerror"]),
    ('<a href="javascript:alert(1)">deal</a>', ["javascript:"]),
    ('<a href=" JaVaScRiPt:alert(1)">deal</a>', ["avascript"]),
    ('<a href="java&#x09;script:alert(1)">deal</a>', ["script:", "alert"]),
    ('<a href="data:text/html;base64,PHNjcmlwdD4=">deal</a>', ["data:"]),
    ('<a href="vbscript:msgbox(1)">deal</a>', ["vbscript"]),
    ('<svg onload="x()"><script>y()</script></svg>', ["<svg", "onload", "y()"]),
    ('<style>body{display:none}</style><p>t</p>', ["<style", "display:none"]),
    ('<form action="https://bad.example"><input name="card"><button>Pay</button></form>', ["<form", "<input",
                                                                                          "bad.example", "Pay"]),
    ('<div style="position:fixed" class="wp-block-button">t</div>', ["style=", "wp-block"]),
    ('<p>t</p><!-- hidden instruction for crawlers -->', ["<!--", "hidden instruction"]),
    ('<object data="x.swf"></object><embed src="y.swf">', ["<object", "<embed", ".swf"]),
    ('<meta http-equiv="refresh" content="0;url=https://bad.example"><p>t</p>', ["<meta", "bad.example"]),
    ('<a href="https://ok.example" target="_blank" rel="opener">ok</a>', ["target=", "rel="]),
])
def test_dangerous_markup_is_removed(dirty, gone):
    out = sanitize_html(dirty).lower()
    for needle in gone:
        assert needle.lower() not in out, (needle, out)


def test_text_of_an_unknown_tag_is_kept_and_escaped():
    assert sanitize_html("<span>Reed <font>mats</font></span> a < b") == "Reed mats a &lt; b"
    assert sanitize_html("<scr<script>x</script>ipt>alert(2)</script>").count("<script") == 0


def test_safe_links_and_markdown_attributes_are_kept():
    out = sanitize_html('<a href="https://example.org/a?b=1&amp;c=2" title="T">x</a><a href="/rel">r</a>'
                        '<a href="mailto:info@example.org">m</a><a href="#fn:1" class="footnote-ref">1</a>'
                        '<td style="text-align: right;">v</td><td style="color: red">w</td>')
    assert 'href="https://example.org/a?b=1&amp;c=2" title="T"' in out
    assert 'href="/rel"' in out and 'href="mailto:info@example.org"' in out
    assert '<a href="#fn:1" class="footnote-ref">' in out
    assert '<td style="text-align: right;">v</td>' in out and "<td>w</td>" in out


def test_unclosed_tags_are_closed():
    assert sanitize_html("<p><strong>open") == "<p><strong>open</strong></p>"
    assert sanitize_html("</p>stray") == "stray"


def test_allow_lists_never_hold_an_active_element():
    assert not ALLOWED_TAGS & {"script", "iframe", "img", "object", "embed", "svg", "style", "form", "input"}
    assert {"script", "iframe", "style", "svg", "object", "embed"} <= DROP_WITH_CONTENT


@pytest.mark.parametrize("url,ok", [
    ("https://example.org", True), ("http://example.org", True), ("mailto:a@example.org", True),
    ("tel:+0012345", True), ("/path", True), ("#anchor", True), ("?q=1", True),
    ("javascript:x", False), ("JAVASCRIPT:x", False), (" javascript:x", False), ("java\nscript:x", False),
    ("data:text/html,x", False), ("vbscript:x", False), ("file:///etc/passwd", False), ("javascript&colon;x", False),
])
def test_safe_url(url, ok):
    assert (safe_url(url) is not None) is ok


def test_plain_text_keeps_clean_text_unchanged():
    for text in ("Plain text, 10:30 departure, a <3 heart, < 5 km, x > y.", "Line one\n\nLine two #Tag", ""):
        assert sanitize_plain_text(text) == text


def test_plain_text_removes_markup_and_defuses_script_urls():
    dirty = ("Go <b>now</b>. <script>steal()</script><iframe src='https://frame.example'></iframe>"
             "<!-- note --> javascript:alert(1) <scr<script>y</script>ipt>z</script> data:text/html,x")
    out = sanitize_plain_text(dirty)
    assert out.startswith("Go now. ")
    for needle in ("<", "steal()", "frame.example", "note", "javascript:", "data:text/html"):
        assert needle not in out, (needle, out)
    assert sanitize_plain_text(out) == out
