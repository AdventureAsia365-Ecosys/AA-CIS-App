"""services.ai_safety.html_sanitizer — AA-1167 (OWASP LLM05, improper output handling): the one sanitizer every AI text
passes through before it leaves the platform.

AI text is written from supplier, tenant and web text (untrusted input), so it can carry markup the writer copied or was
tricked into writing. Python-Markdown passes raw HTML in its input straight through, so before this module a piece with
``<script>`` in its text reached the agency's WordPress site as live script.

- :func:`sanitize_html` — an HTML fragment (the Markdown render of a blog piece) rebuilt from an allow-list: only the
  tags and attributes Markdown itself writes for a blog piece survive, links only with an http(s), mailto or tel URL (or
  a relative one), every other tag is dropped (its text kept), and the elements that run or embed something
  (script, style, iframe, object, ...) are dropped with their content. Comments, processing instructions and doctypes
  go too. The output is rebuilt from parsed tokens, never patched with regexes, so a broken or nested tag cannot slip
  through. Normal Markdown output is unchanged (tests/unit/test_aa1167_html_sanitizer.py).
- :func:`sanitize_plain_text` — a plain-text channel (Facebook post, plain-text download, a WordPress title or meta
  description): HTML tags and comments removed (the dangerous elements with their content), and a ``javascript:`` /
  ``vbscript:`` / ``data:text/html`` URL defused. Text that only looks like a comparison ("< 5 km") is kept.

The allow-lists are security invariants, not settings: an administrator must never be able to allow ``<script>`` or an
event handler. They change only by a reviewed PR with a test (the guard tests/unit/test_aa1167_publish_guard.py keeps
every publish path on this module).

Used by: services/acp_content_writing/export.py (WordPress post body, HTML fragment and document exports, plain-text
export) and services/acp_publish/dispatch.py (Facebook message, WordPress title and meta description).
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Optional

__all__ = ["ALLOWED_ATTRS", "ALLOWED_TAGS", "DROP_WITH_CONTENT", "sanitize_html", "sanitize_plain_text", "safe_url"]

# security-invariant: the tags Python-Markdown ("extra" + "nl2br") writes for a blog piece. Anything else is dropped.
# No img: a blog piece has no images (an injected one would be a tracking pixel or a hotlink).
ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u", "s", "del", "ins",
    "sub", "sup", "small", "mark", "abbr", "blockquote", "code", "pre", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "a", "div",
})
# security-invariant: attributes kept per tag (never on*, target, src or a free style / class / id). The few
# Markdown writes are kept only in the exact form it writes them (_ATTR_VALUE): a table cell's text-align, the
# footnote ids and classes.
ALLOWED_ATTRS = {
    "a": frozenset({"href", "title", "class"}),
    "abbr": frozenset({"title"}),
    "th": frozenset({"align", "style"}),
    "td": frozenset({"align", "style"}),
    "ol": frozenset({"start"}),
    "sup": frozenset({"id"}),
    "li": frozenset({"id"}),
    "div": frozenset({"class"}),
}
_ATTR_VALUE = {
    "style": re.compile(r"text-align: (?:left|right|center);"),
    "class": re.compile(r"footnote|footnote-ref|footnote-backref"),
    "id": re.compile(r"fn(?:ref)?:[0-9A-Za-z_-]{1,40}"),
    "align": re.compile(r"left|right|center", re.I),
    "start": re.compile(r"[0-9]{1,6}"),
}
# security-invariant: elements dropped WITH their content (they run, style or embed something, or hold a form).
DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "noscript", "noembed",
    "noframes", "template", "svg", "math", "xml", "textarea", "select", "option", "title", "head", "base",
    "link", "meta", "form", "button",
})
_VOID = frozenset({"br", "hr"})
# security-invariant: URL schemes a link may carry; a URL without a scheme (relative, #anchor) is kept.
_SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
# Characters a browser ignores inside a URL scheme ("java\tscript:"), stripped before the scheme check.
_URL_NOISE_RE = re.compile(r"[\x00-\x20\x7f-\x9f​-‏  ﻿]+")


def safe_url(value: Optional[str]) -> Optional[str]:
    """``value`` when it is a link a reader may follow (http, https, mailto, tel, or a relative URL), else None."""
    if value is None:
        return None
    probe = _URL_NOISE_RE.sub("", html.unescape(value))
    m = _SCHEME_RE.match(probe)
    if m and m.group(1).lower() not in _SAFE_SCHEMES:
        return None
    if not m and ":" in probe.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]:
        return None          # "javascript&colon;..." style or an odd scheme the regex did not take: refuse
    return value.strip()


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self._drop_depth = 0
        self._drop_tag: Optional[str] = None
        self._open: list[str] = []

    # -- tags
    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, self_closing=True)

    def _start(self, tag: str, attrs, *, self_closing: bool) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag and not self_closing:
                self._drop_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            if not self_closing and tag not in ("base", "link", "meta"):
                self._drop_depth, self._drop_tag = 1, tag
            return
        if tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            name = name.lower()
            if name not in ALLOWED_ATTRS.get(tag, ()):
                continue
            if name == "href":
                value = safe_url(value)
                if value is None:
                    continue
            elif name in _ATTR_VALUE and not _ATTR_VALUE[name].fullmatch(value or ""):
                continue
            kept.append(f' {name}="{html.escape(value or "", quote=True)}"')
        if tag in _VOID:
            self.out.append(f"<{tag}{''.join(kept)} />")
            return
        self.out.append(f"<{tag}{''.join(kept)}>")
        self._open.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if not self._drop_depth:
                    self._drop_tag = None
            return
        if tag not in ALLOWED_TAGS or tag in _VOID or tag not in self._open:
            return
        while self._open:                       # close what is still open inside it (well-formed output)
            top = self._open.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    # -- text
    def handle_data(self, data):
        if not self._drop_depth:
            self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name):
        if not self._drop_depth:
            self.out.append(f"&{name};")          # the parser only matches [a-zA-Z][-.a-zA-Z0-9]*: inert text

    def handle_charref(self, name):
        if not self._drop_depth:
            self.out.append(f"&#{name};" if re.fullmatch(r"[0-9]{1,7}|[xX][0-9a-fA-F]{1,6}", name)
                            else f"&amp;#{name};")

    # comments, <!doctype>, <?pi?>, CDATA: dropped (handle_comment / decl / pi / unknown_decl not overridden)
    def handle_comment(self, data):
        return

    def close(self):
        super().close()
        while self._open:
            self.out.append(f"</{self._open.pop()}>")


def sanitize_html(fragment: Optional[str]) -> str:
    """The allow-list rebuild of an HTML fragment (see the module docstring). Idempotent; never raises."""
    if not fragment:
        return ""
    parser = _Sanitizer()
    parser.feed(fragment)
    parser.close()
    return "".join(parser.out)


_DROP_BLOCK_RE = re.compile(
    r"<\s*(" + "|".join(sorted(DROP_WITH_CONTENT)) + r")\b[^>]*>.*?(?:<\s*/\s*\1\s*>|\Z)", re.I | re.S)
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s[^<>]*)?/?>|<![a-zA-Z][^<>]*>|<\?[^<>]*\?>")
_BAD_URL_RE = re.compile(r"\b(?:java|vb)\s*script\s*:|\bdata\s*:\s*text/html", re.I)


def sanitize_plain_text(text: Optional[str]) -> str:
    """A plain-text channel's text with markup removed: dangerous elements with their content, comments, every other
    tag (its text kept); a ``javascript:`` / ``vbscript:`` / ``data:text/html`` URL is defused to ``blocked:``.
    Idempotent; text with no markup is returned unchanged; never raises."""
    if not text:
        return text or ""
    if "<" not in text and ":" not in text:
        return text
    out = _DROP_BLOCK_RE.sub("", text)
    out = _COMMENT_RE.sub("", out)
    prev = None
    while prev != out:                          # "<scr<b>ipt>" rebuilt by one pass is removed by the next
        prev, out = out, _TAG_RE.sub("", _DROP_BLOCK_RE.sub("", out))
    return _BAD_URL_RE.sub("blocked:", out)
