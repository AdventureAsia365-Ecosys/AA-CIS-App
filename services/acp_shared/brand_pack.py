"""AA-771 — the tenant brand pack: one read of `shared.tenant_brand_rules` that T8 (angles), T9
(writer) and T10 (F9 judge, F2 forbidden words, F11 brand name) all use.

Before this, T8/T9 got only customer_segment + customer_mindset, and the F9 judge fell back to
Adventure Asia's own identity (AA_BRAND_IDENTITY_PROMPT) whenever `system_prompt` was empty —
which it is for every tenant onboarded through the T0 form or a .docx brief (the 4 pilot tenants
have 11/13 brand fields filled and `system_prompt` NULL). S232 panel: 3 of 5 angle briefs broke
an explicit "Do not" rule in the tenant's own style guide; 25/29 LinkedIn posts carried an
invented "At <Brand>, we…" line; t10_brand_voice measured closeness to AA's voice (r=0.11).

The pack is assembled deterministically from the T0 columns at read time (Nghiệp, S233: no LLM
compile step, always current with the tenant's latest T0 save). `system_prompt`, when a tenant
or admin wrote one, is kept and used as the judge rubric's head, as before.
"""
from __future__ import annotations

import json
import re
from typing import Optional, TypedDict
from uuid import UUID

from services.acp_shared.signature_vocabulary import explicit_signature_words


class BrandPack(TypedDict, total=False):
    brand_name: Optional[str]
    brand_type: Optional[str]
    core_idea: Optional[str]
    customer_segment: Optional[str]
    customer_mindset: Optional[str]
    voice: list[str]
    style_guide: Optional[str]
    forbidden_words: list[str]
    good_examples: Optional[str]
    default_cta: Optional[str]
    system_prompt: Optional[str]
    version: Optional[int]
    # AA-800: positioning words the tenant lists as its own in custom_validators.signature_words (the
    # rest of its signature vocabulary is read from its T0 text: signature_vocabulary.signature_words).
    signature_words: list[str]


_PACK_QUERY = """
    SELECT *
    FROM shared.tenant_brand_rules
    WHERE tenant_id = $1::uuid AND is_active = true
    -- same resolution as fetch_brand_rubric_text (AA-700): 'default' first, else newest active
    ORDER BY (brand_name = 'default') DESC, version DESC
    LIMIT 1
"""


def _as_list(value) -> list[str]:
    """voice_examples / forbidden_words are jsonb; asyncpg returns them as JSON text (no codec
    registered, AA-300). Accepts a list, a JSON list string, or a plain comma-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return [p.strip() for p in text.split(",") if p.strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _clean(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def pack_from_row(row) -> BrandPack:
    if row is None:
        return {}
    get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k] if k in row else d)
    name = _clean(get("brand_name"))
    return {
        "brand_name": None if (name or "").lower() == "default" else name,
        "brand_type": _clean(get("brand_type")),
        "core_idea": _clean(get("core_idea")),
        "customer_segment": _clean(get("customer_segment")),
        "customer_mindset": _clean(get("customer_mindset")),
        "voice": _as_list(get("voice_examples")),
        "style_guide": _clean(get("style_guide")),
        "forbidden_words": _as_list(get("forbidden_words")),
        "good_examples": _clean(get("good_examples")),
        "default_cta": _clean(get("default_cta")),  # migration 220; absent before it → None
        "system_prompt": _clean(get("system_prompt")),
        "version": get("version"),
        "signature_words": explicit_signature_words(get("custom_validators")),  # AA-800
    }


async def fetch_brand_pack_conn(conn, tenant_id) -> BrandPack:
    return pack_from_row(await conn.fetchrow(_PACK_QUERY, str(tenant_id)))


async def fetch_brand_pack(tenant_id: UUID | str, pool) -> BrandPack:
    async with pool.acquire() as conn:
        return await fetch_brand_pack_conn(conn, tenant_id)


def has_brand(pack: BrandPack) -> bool:
    """True when the pack says enough about the brand to write and judge against it."""
    return bool(pack.get("core_idea") or pack.get("style_guide") or pack.get("system_prompt"))


def render_brand_block(pack: BrandPack, *, heading: str = "BRAND") -> str:
    """The brand block for the T8 angle prompt, the T9 writer prompt and the T2 tenant-voice prompt
    (AA-792). Empty fields are left out. The self-reference rule stops invented "At <Brand>, we…"
    brand facts (S232)."""
    lines = [f"{heading} (you write as this brand; it is the only brand, so never name or invent "
             f"another brand, and never mention the platform this material came from):"]
    if pack.get("brand_name"):
        lines.append(f"Name: {pack['brand_name']}")
    if pack.get("brand_type"):
        lines.append(f"Brand type: {pack['brand_type']}")
    if pack.get("core_idea"):
        lines.append(f"Core idea: {pack['core_idea']}")
    if pack.get("customer_segment"):
        lines.append(f"Audience: {pack['customer_segment']}")
    if pack.get("customer_mindset"):
        lines.append(f"Audience mindset: {pack['customer_mindset']}")
    if pack.get("voice"):
        lines.append("Voice: " + ", ".join(pack["voice"]))
    if pack.get("style_guide"):
        # AA-775 — plan 09 §3.3 (chị Thư write.py:116-118): the style guide is style, never a source.
        # T8, T9 and T2 (content_generation) all render this block.
        lines.append("Style guide: follow every rule in it, including each \"do not\". How this brand writes, "
                     "in its own words. This is style and never a source: never cite it, never repeat one of "
                     "its example sentences, and never state its claims about pace, rest, balance or "
                     "difficulty as facts about this trip unless the content seed says so.\n"
                     + pack["style_guide"])
    if pack.get("forbidden_words"):
        lines.append("Never use these words or phrases: " + ", ".join(pack["forbidden_words"]))
    if pack.get("good_examples"):
        lines.append("On-brand writing to match in voice (match the voice; do not copy its sentences "
                     "or treat anything in it as a fact about this trip):\n" + pack["good_examples"])
    lines.append("Brand self-reference: at most one sentence about the brand itself, saying only "
                 "what the core idea or style guide above states; never invent how the brand "
                 "works, its group sizes, guides, partners, history or numbers.")
    if len(lines) == 2:  # heading + self-reference only: no T0 data at all
        lines.insert(1, "(No brand identity is set up for this tenant yet: write in a plain, "
                        "warm, specific voice and do not describe the brand at all.)")
    return "\n".join(lines)


def compile_rubric(pack: BrandPack) -> Optional[str]:
    """The F9 judge rubric built from the T0 fields, for a tenant with no `system_prompt`.
    None when the pack has nothing to judge against."""
    if not has_brand(pack):
        return None
    head = [f"BRAND IDENTITY: {pack.get('brand_name') or 'this tenant'}"]
    for label, key in (("Brand type", "brand_type"), ("Core idea", "core_idea"),
                       ("Audience", "customer_segment"), ("Audience mindset", "customer_mindset")):
        if pack.get(key):
            head.append(f"{label}: {pack[key]}")
    if pack.get("voice"):
        head.append("Voice: " + ", ".join(pack["voice"]))
    if pack.get("default_cta"):
        head.append(f"Call to action: {pack['default_cta']}")
    return "\n".join(head)


# ---------------------------------------------------------------- brand-name checks (F11)

_PLATFORM_NAMES_RE = re.compile(r"\bAdventure\s+Asia\b|\bAA\b")
# "At Trail Pulse, we …" / "Here at Atlas & Hearth, our …" — the self-positioning frame the
# writer used to invent brand names ("At Arcadia Adventures, we", "At Adventurous Afternoons").
_AT_BRAND_RE = re.compile(
    r"\b(?:[Hh]ere at|At)\s+((?:[A-Z][\w'’&.-]*|&)(?:\s+(?:[A-Z][\w'’&.-]*|&|of|the|and)){0,4})\s*,"
)
_SELF_RE = re.compile(r"\b(?:we|our|us)\b")


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9& ]+", " ", name.lower()).split())


def foreign_brand_mentions(text: str, brand_name: Optional[str], source_text: str = "") -> list[str]:
    """Names in `text` used as the brand that are not `brand_name`: "At <Other Name>, we…" (a
    first-person word within the next few words) and the platform's own name (Adventure Asia /
    AA) for a tenant that is not that brand. A name that appears in `source_text` is a place
    ("At Paro, we stop for tea"), not a brand, and is skipped."""
    found: list[str] = []
    own = _norm(brand_name or "")
    source = (source_text or "").lower()
    for m in _AT_BRAND_RE.finditer(text or ""):
        if not _SELF_RE.search(" ".join(text[m.end():m.end() + 80].split()[:8])):
            continue
        name = m.group(1).strip()
        if own and (_norm(name) == own or _norm(name) in own or own in _norm(name)):
            continue
        if name.lower() in source:
            continue
        found.append(name)
    if not own or not ("adventure asia" in own or own == "aa"):
        found += [m.group(0) for m in _PLATFORM_NAMES_RE.finditer(text or "")
                  if m.group(0).lower() not in source]
    return found


__all__ = ["BrandPack", "pack_from_row", "fetch_brand_pack", "fetch_brand_pack_conn", "has_brand",
           "render_brand_block", "compile_rubric", "foreign_brand_mentions"]
