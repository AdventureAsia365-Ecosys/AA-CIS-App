"""services.acp_content_writing.ai_disclosure_setting — the AI disclosure wording as a setting (CIS 463, EU AI Act Art.
50; tech lead S278 / S279 rule: a customer-visible wording is editable on an admin page, seeded, audited, never a code
literal or an env var alone).

``cis.content.ai_disclosure`` (Platform › Settings, area content, value type json):

    {"text": {"en": "This content was created with the help of AI.", "vi": "..."}, "social_tag": "#AIgenerated"}

- ``text``: the visible disclosure line per language (two-letter code). ``en`` is required (the fallback of every other
  language); each line is non-empty, one line, at most :data:`TEXT_MAX_CHARS` characters.
- ``social_tag``: the hashtag a social text post ends with (one token, ``#`` then 2 to 50 letters, digits or ``_``).

An administrator can reword the line, add a language or change the tag; the line itself is a code invariant: it cannot
be blanked or switched off (an empty text, a missing ``en`` or a missing tag is refused by :func:`value_problem`, and a
stored value that breaks it is ignored by :func:`config`, which then serves :data:`DEFAULT`).

:func:`value_problem` is the shape check the settings API runs on a proposal and again on approval (the generic json
check of shared/settings/reader.py accepts any object). :func:`config`, :func:`text_for` and :func:`social_tag` read
the value in force through ``shared.settings`` and never raise: any read problem serves :data:`DEFAULT`, which equals
the CIS 463 seed (tests/unit/test_aa1168_ai_disclosure_setting.py).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

KEY = "cis.content.ai_disclosure"
FALLBACK_LANG = "en"
# The CIS 463 seed default; served whenever the setting cannot be read or holds a value that breaks the shape below.
DEFAULT_TEXT_EN = "This content was created with the help of AI."
DEFAULT_TEXT_VI = "Nội dung này được tạo với sự hỗ trợ của AI."
DEFAULT_SOCIAL_TAG = "#AIgenerated"
DEFAULT: dict = {"text": {"en": DEFAULT_TEXT_EN, "vi": DEFAULT_TEXT_VI}, "social_tag": DEFAULT_SOCIAL_TAG}

TEXT_MAX_CHARS = 200  # technical-constant: the shape of a one-line disclosure (validator bound, not a policy value)
_LANG_RE = re.compile(r"^[a-z]{2}$")
_TAG_RE = re.compile(r"^#[A-Za-z0-9_]{2,50}$")
_FIELDS = frozenset({"text", "social_tag"})


def value_problem(value: Any) -> Optional[str]:
    """Why ``value`` is not a valid disclosure setting (None when it is). The API answers 422 with this reason."""
    if not isinstance(value, dict):
        return "value must be a JSON object with \"text\" and \"social_tag\""
    extra = sorted(set(value) - _FIELDS)
    if extra:
        return f"unknown field {extra[0]!r} (allowed: text, social_tag)"
    text = value.get("text")
    if not isinstance(text, dict) or not text:
        return "\"text\" must be an object of language code to disclosure line, with \"en\""
    if FALLBACK_LANG not in text:
        return "\"text\" must have an English line (\"en\"): it is the fallback of every other language"
    for lang, line in text.items():
        if not isinstance(lang, str) or not _LANG_RE.match(lang):
            return f"language code {lang!r} must be two lowercase letters (en, vi, ...)"
        if not isinstance(line, str) or not line.strip():
            return f"the {lang} line must not be empty: the disclosure cannot be switched off"
        if "\n" in line or "\r" in line:
            return f"the {lang} line must be one line"
        if len(line.strip()) > TEXT_MAX_CHARS:
            return f"the {lang} line is longer than {TEXT_MAX_CHARS} characters"
    tag = value.get("social_tag")
    if not isinstance(tag, str) or not tag.strip():
        return "\"social_tag\" must not be empty: a social post always carries the tag"
    if not _TAG_RE.match(tag):
        return "\"social_tag\" must be one hashtag: # then 2 to 50 letters, digits or _"
    return None


def _normalized(value: dict) -> dict:
    return {"text": {lang: line.strip() for lang, line in value["text"].items()}, "social_tag": value["social_tag"]}


def config() -> dict:
    """The disclosure in force: the setting's value when it reads and fits :func:`value_problem`, else :data:`DEFAULT`
    (never raises; a refused stored value is logged once per read)."""
    try:
        from shared import settings

        value = settings.get(KEY)
    except Exception as exc:  # noqa: BLE001 — the disclosure must never fail a publish or an export
        logger.warning("ai_disclosure_setting_unreadable", key=KEY, error=f"{type(exc).__name__}: {exc}"[:200])
        return _normalized(DEFAULT)
    problem = value_problem(value)
    if problem:
        logger.warning("ai_disclosure_setting_refused", key=KEY, problem=problem)
        return _normalized(DEFAULT)
    return _normalized(value)


def lang_of(lang: Optional[str]) -> str:
    """'vi', 'VI', 'vi-VN', 'vi_VN' -> 'vi'; None or anything else unusable -> 'en'."""
    code = (lang or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if _LANG_RE.match(code) else FALLBACK_LANG


def text_for(lang: Optional[str] = None, cfg: Optional[dict] = None) -> str:
    """The disclosure line in ``lang`` (the English line when the setting has none for it)."""
    text = (cfg or config())["text"]
    return text.get(lang_of(lang)) or text[FALLBACK_LANG]


def social_tag(cfg: Optional[dict] = None) -> str:
    return (cfg or config())["social_tag"]


__all__ = ["DEFAULT", "DEFAULT_SOCIAL_TAG", "DEFAULT_TEXT_EN", "DEFAULT_TEXT_VI", "FALLBACK_LANG", "KEY",
           "TEXT_MAX_CHARS", "config", "lang_of", "social_tag", "text_for", "value_problem"]
