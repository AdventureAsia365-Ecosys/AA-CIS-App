"""AA-916 WM-06 — the reason codes an admin attaches to an action on writer output (app ADR 0010).

The eight codes are the CHECK set of `shared.writing_signal.reason_code` (CIS 277). Adding one needs a migration
first (lesson: a value code writes must be in the CHECK before the code deploys).

`suggested_reason(gate)` is the chip the admin UI preselects for a flagged sentence: deterministic, no model. Gate
names come from the quality registry (AA-926: a gate-name literal outside services/quality/ fails the lint); a gate
with no explicit entry falls back to its taxonomy classes, then to `style_only`.
"""
from __future__ import annotations

from typing import Literal, Optional, get_args

from services.quality import registry as qr

ReasonCode = Literal["fact_not_in_source", "optional_as_included", "wrong_place_or_time", "voice_off_brand",
                     "ai_phrasing", "format_channel", "repair_residue", "style_only"]
REASON_CODES: tuple[str, ...] = get_args(ReasonCode)

FACT_NOT_IN_SOURCE: ReasonCode = "fact_not_in_source"
DEFAULT_REASON: ReasonCode = "style_only"

# Explicit gate -> chip (plan AA-916 S3). Everything else goes through the taxonomy classes below.
_BY_GATE: dict[str, ReasonCode] = {
    # facts the source does not support
    qr.F1_GROUNDING: "fact_not_in_source",
    qr.F9_CTA_FACT: "fact_not_in_source",
    qr.F17_TRIP_FACT: "fact_not_in_source",
    qr.F2_PLATFORM_TRUTH: "fact_not_in_source",
    qr.T3_GROUNDING: "fact_not_in_source",
    # a place / time outside the piece's scope
    qr.F12_SCOPE: "wrong_place_or_time",
    # the brand's voice, words and name
    qr.F9_BRAND_STYLE: "voice_off_brand",
    qr.F9_BRAND_VOICE: "voice_off_brand",
    qr.F15_VOICE: "voice_off_brand",
    qr.F15_VOICE_WARN: "voice_off_brand",
    qr.F11_BRAND_NAME: "voice_off_brand",
    qr.F2_BRAND_FORBIDDEN: "voice_off_brand",
    qr.T2_FORBIDDEN_WORD: "voice_off_brand",
    # generic AI wording
    qr.F2_BANNED_PATTERNS: "ai_phrasing",
    qr.F2_STYLE_WARN: "ai_phrasing",
    qr.F2_SUPERLATIVES: "ai_phrasing",
    # what a repair step left behind
    qr.F13_HYGIENE: "repair_residue",
    qr.F13_LEAK: "repair_residue",
    qr.F13_REPEAT: "repair_residue",
    # channel shape and length
    qr.F14_SOCIAL_SHAPE: "format_channel",
    qr.F14_FIRST_LINE: "format_channel",
    qr.F4_EXTREME_LENGTH: "format_channel",
    qr.F4_SEO_SURFACE: "format_channel",
    qr.F16_FAQ: "format_channel",
    qr.F0_COMPLETE: "format_channel",
}

# Taxonomy class (qtax-v1) -> chip, for a gate without an explicit entry. Checked in this order.
_BY_CLASS: tuple[tuple[str, ReasonCode], ...] = (
    ("fact.", "fact_not_in_source"),
    ("other.f6", "optional_as_included"),
    ("scope.", "wrong_place_or_time"),
    ("other.v1", "ai_phrasing"),
    ("other.lexicon", "voice_off_brand"),
    ("struct.", "format_channel"),
)


def suggested_reason(gate: Optional[str]) -> ReasonCode:
    """The chip to preselect for a sentence flagged by `gate` (the first gate of a flagged sentence)."""
    if not gate:
        return DEFAULT_REASON
    hit = _BY_GATE.get(gate)
    if hit:
        return hit
    for cls in qr.gate_classes(gate):
        for prefix, code in _BY_CLASS:
            if cls == prefix or (prefix.endswith(".") and cls.startswith(prefix)):
                return code
    return DEFAULT_REASON


def is_reason_code(value: Optional[str]) -> bool:
    return value in REASON_CODES


__all__ = ["DEFAULT_REASON", "FACT_NOT_IN_SOURCE", "REASON_CODES", "ReasonCode", "is_reason_code",
           "suggested_reason"]
