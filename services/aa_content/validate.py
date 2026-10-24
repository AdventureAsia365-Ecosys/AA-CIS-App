"""Field rules for AA-authored content (AA-977). One home for the admin API, the store and the seed script.

The SQL CHECKs of CIS 275 hold the same limits (kind, locale, group_key, title 1-300, body <= 20000, applies_to keys);
this module adds what SQL cannot see, with an error the editor can show next to the field:

- ``applies_to.country_codes``: upper-case ISO 3166-1 alpha-2 of a country AA sells (``services/places/guards.py``
  AA_ISO2), no duplicates;
- ``applies_to.tours``: ``{source_system: "cis", source_id}`` with a source_id of 1-128 characters (the catalog key);
- at publish: a title and a body, and for a prep pack the visa rule of the contract (AaContent.body_markdown: "Visa
  items are an official link only", Q1 2.3): every section that mentions a visa (visa / e-visa / thị thực) carries an
  https link, so the CRM never shows visa advice without the official source.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from services.places.guards import AA_ISO2

KINDS = ("prep_pack", "starter_template", "campaign_block")
LOCALES = ("en", "vi")
GROUP_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
TITLE_MAX = 300
BODY_MAX = 20000
SOURCE_ID_MAX = 128
AA_COUNTRY_CODES = frozenset(code.upper() for code in AA_ISO2)

_VISA_RE = re.compile(r"\bvisas?\b|thị\s+thực", re.IGNORECASE)
_HTTPS_LINK_RE = re.compile(r"https://[^\s)\]>]+", re.IGNORECASE)
_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")


class ContentInvalid(ValueError):
    """A value the contract or the editor rules refuse: 422 ``{code, field, detail}``."""

    def __init__(self, code: str, field: str, detail: str) -> None:
        super().__init__(code, field, detail)
        self.code, self.field, self.detail = code, field, detail

    def __str__(self) -> str:
        return self.detail


def kind(value: Any) -> str:
    if value not in KINDS:
        raise ContentInvalid("invalid_kind", "kind", f"kind must be one of {', '.join(KINDS)}")
    return value


def locale(value: Any) -> str:
    if value not in LOCALES:
        raise ContentInvalid("invalid_locale", "locale", f"locale must be one of {', '.join(LOCALES)}")
    return value


def group_key(value: Any) -> str:
    if not isinstance(value, str) or not GROUP_KEY_RE.match(value):
        raise ContentInvalid("invalid_group_key", "group_key",
                             "group_key: 2-80 characters, lower-case letters, digits and '-', not starting with '-'")
    return value


def title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentInvalid("title_required", "title", "A title is required")
    text = value.strip()
    if len(text) > TITLE_MAX:
        raise ContentInvalid("title_too_long", "title", f"The title has more than {TITLE_MAX} characters")
    return text


def body(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContentInvalid("invalid_body", "body_markdown", "body_markdown must be text")
    if len(value) > BODY_MAX:
        raise ContentInvalid("body_too_long", "body_markdown", f"The text has more than {BODY_MAX} characters")
    return value


def applies_to(value: Any) -> dict:
    """The contract's AaContent.applies_to, normalized (country codes upper-cased, duplicates dropped)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ContentInvalid("invalid_applies_to", "applies_to", "applies_to must be an object")
    extra = sorted(set(value) - {"country_codes", "tours"})
    if extra:
        raise ContentInvalid("invalid_applies_to", "applies_to",
                             f"applies_to takes only country_codes and tours (not {', '.join(extra)})")
    out: dict[str, list] = {}
    if "country_codes" in value:
        codes = value["country_codes"]
        if not isinstance(codes, list):
            raise ContentInvalid("invalid_country", "applies_to.country_codes", "country_codes must be a list")
        seen: list[str] = []
        for code in codes:
            text = code.strip().upper() if isinstance(code, str) else ""
            if text not in AA_COUNTRY_CODES:
                raise ContentInvalid("invalid_country", "applies_to.country_codes",
                                     f"{code!r} is not the ISO code of a country Adventure Asia sells")
            if text not in seen:
                seen.append(text)
        out["country_codes"] = seen
    if "tours" in value:
        tours = value["tours"]
        if not isinstance(tours, list):
            raise ContentInvalid("invalid_tour", "applies_to.tours", "tours must be a list")
        keys: list[dict] = []
        for tour in tours:
            if (not isinstance(tour, dict) or set(tour) != {"source_system", "source_id"}
                    or tour["source_system"] != "cis" or not isinstance(tour["source_id"], str)
                    or not 1 <= len(tour["source_id"].strip()) <= SOURCE_ID_MAX):
                raise ContentInvalid("invalid_tour", "applies_to.tours",
                                     'each tour is {"source_system": "cis", "source_id": "<1-128 characters>"}')
            key = {"source_system": "cis", "source_id": tour["source_id"].strip()}
            if key not in keys:
                keys.append(key)
        out["tours"] = keys
    return out


def _sections(text: str) -> list[str]:
    """Blank-line separated blocks; a block of headings only is joined to the block after it, so '## Visa' + its
    paragraph count as one section."""
    sections: list[str] = []
    pending = ""
    for block in _BLOCK_SPLIT_RE.split(text):
        if not block.strip():
            continue
        lines = [line for line in block.splitlines() if line.strip()]
        if all(line.lstrip().startswith("#") for line in lines):
            pending = f"{pending}\n{block}" if pending else block
            continue
        sections.append(f"{pending}\n{block}" if pending else block)
        pending = ""
    if pending:
        sections.append(pending)
    return sections


def visa_sections_without_link(text: str) -> list[str]:
    """The sections that mention a visa without an https link (the first 80 characters of each)."""
    return [s.strip()[:80] for s in _sections(text) if _VISA_RE.search(s) and not _HTTPS_LINK_RE.search(s)]


def publishable(kind_value: str, title_value: Any, body_value: Any) -> None:
    """Raise :class:`ContentInvalid` when a draft may not be published as it is."""
    title(title_value)
    text = body(body_value)
    if not text.strip():
        raise ContentInvalid("body_required", "body_markdown", "The text is empty")
    if kind_value == "prep_pack":
        missing = visa_sections_without_link(text)
        if missing:
            raise ContentInvalid(
                "visa_needs_official_link", "body_markdown",
                "Visa information must point to the official source: add the official https link to the section "
                f"starting \"{missing[0]}\"")


def optional_filter(value: Optional[str], allowed: tuple[str, ...], field: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise ContentInvalid(f"invalid_{field}", field, f"{field} must be one of {', '.join(allowed)}")
    return value
