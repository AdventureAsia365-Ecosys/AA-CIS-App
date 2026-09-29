"""AA-205: shared seo_meta band + sentence helpers (single source of truth).

Imported by graph.py (validate_node) AND flag_fix_node.py (post-repair band guard).
Extracted in AA-205: graph.py imports flag_fix_node, so flag_fix CANNOT import graph.py
back (circular) — this neutral module breaks the cycle and kills BAD_META_ENDINGS drift.

AA-238: SEO_META_FORBIDDEN is the canonical budget/accommodation deny-list (single
source; graph.validate_node + flag_fix_node both consume it). meta_in_band can now
hard-reject forbidden words so a length-padded forbidden meta is never "in band".
AA-239: _salvage_to_band recovers the largest complete-sentence prefix in [MIN, MAX]
(applied to BOTH post and pre) so a cut/no-period meta is never emitted as in-band.
"""
import re
import unicodedata

SEO_META_MIN = 140
SEO_META_MAX = 155

# AA-201: seo_meta must be a complete sentence (port of v5 repair_seo_fields)
BAD_META_ENDINGS = {
    "and", "with", "including", "or", "plus", "to", "for", "from", "in", "on", "at",
}

# AA-238/D4: canonical budget/accommodation deny-list for seo_meta (AA audience = $250k+).
# Single source of truth — graph.validate_node and flag_fix_node both import this.
SEO_META_FORBIDDEN = (
    "hostel", "budget", "public transport", "cheap", "backpacker", "dorm",
)


def _normalize_meta(s: str) -> str:
    """Lowercase, strip accents (NFKD, AA-115), fold hyphens to spaces, collapse runs.
    So 'Public-Transport' and 'public  transport' both match 'public transport'."""
    t = unicodedata.normalize("NFKD", (s or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


def meta_has_forbidden(meta: str, forbidden) -> bool:
    """True if any forbidden term appears in meta (hyphen/space/accent-insensitive)."""
    if not forbidden:
        return False
    norm = _normalize_meta(meta)
    for term in forbidden:
        nt = _normalize_meta(term)
        if nt and nt in norm:
            return True
    return False


def meta_complete_sentence(meta: str) -> bool:
    """True when seo_meta reads as a complete sentence (period end, >=8 words,
    no trailing preposition/conjunction). No verb-whitelist (AA-201 revision)."""
    t = (meta or "").strip()
    if not t.endswith("."):
        return False
    words = t.split()
    if len(words) < 8:
        return False
    last = re.sub(r"[^a-zA-Z]", "", words[-1].lower()) if words else ""
    if last in BAD_META_ENDINGS:
        return False
    return True


def meta_in_band(meta: str, forbidden=None) -> bool:
    """In [SEO_META_MIN, SEO_META_MAX] AND complete sentence AND (AA-238) forbidden-free.
    forbidden=None preserves legacy behavior for callers that pass no deny-list."""
    t = (meta or "").strip()
    if not (SEO_META_MIN <= len(t) <= SEO_META_MAX):
        return False
    if not meta_complete_sentence(t):
        return False
    if forbidden and meta_has_forbidden(t, forbidden):
        return False
    return True


def _trim_to_sentence(text: str, limit: int) -> str:
    """Trim to <= limit, preferring last sentence terminator within limit.
    Trims DOWN only — never pads up. Local mirror of admin_pipeline trim (no cross-import)."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    window = t[:limit]
    cut = max(window.rfind("."), window.rfind("!"), window.rfind("?"))
    if cut != -1:
        return window[:cut + 1].rstrip()
    space = window.rfind(" ")
    if space != -1:
        return window[:space].rstrip()
    return window.rstrip()


def _salvage_to_band(text: str, forbidden=None):
    """AA-239: largest complete-sentence prefix in [MIN, MAX], forbidden-free.
    Returns the prefix, or None when no sentence boundary lands in band (caller escalates).
    Scans every '.'/'!'/'?' (not just the last) and keeps the longest that is >= MIN."""
    t = (text or "").strip()
    cuts = [i for i, ch in enumerate(t) if ch in ".!?"]
    for p in reversed(cuts):
        prefix = t[:p + 1].rstrip()
        if len(prefix) < SEO_META_MIN or len(prefix) > SEO_META_MAX:
            continue
        if not meta_complete_sentence(prefix):
            continue
        if forbidden and meta_has_forbidden(prefix, forbidden):
            continue
        return prefix
    return None


def best_meta_candidate(post_repair: str, pre_repair: str, forbidden=None) -> str:
    """AA-205 deterministic post-repair band guard (no LLM, no padding, no escalate).
    AA-239: salvage applied to BOTH post and pre (largest complete sentence in band),
    so an over-length or cut post is recovered instead of returned raw.
    AA-238: when forbidden is provided, a candidate containing a forbidden word is never
    treated as in-band, so it cannot be returned here.
    Preference: (1) post if in-band; (2) salvaged post; (3) salvaged pre; (4) post unchanged
    (caller re-repairs / flags — never silently to gold)."""
    post = (post_repair or "").strip()
    pre = (pre_repair or "").strip()
    if meta_in_band(post, forbidden):
        return post
    salvaged = _salvage_to_band(post, forbidden)
    if salvaged and meta_in_band(salvaged, forbidden):
        return salvaged
    if pre:
        salvaged = _salvage_to_band(pre, forbidden)
        if salvaged and meta_in_band(salvaged, forbidden):
            return salvaged
    return post


def fit_seo_meta(meta: str, tenant_forbidden=None) -> str:
    """AA-641: deterministic fit of an out-of-band seo_meta before T3 decides to rewrite the tour
    (the meta counterpart of tenant_pipeline.fit_seo_title). Keeps the longest complete-sentence
    prefix inside [SEO_META_MIN, SEO_META_MAX] that is forbidden-free (_salvage_to_band). If no such
    prefix exists — e.g. the meta is too SHORT — it is returned unchanged, so this never produces an
    invalid meta; the existing repair path handles it."""
    forbidden = set(SEO_META_FORBIDDEN) | {w.lower().strip() for w in (tenant_forbidden or []) if w and w.strip()}
    if meta_in_band(meta, forbidden):
        return meta
    salvaged = _salvage_to_band(meta, forbidden)
    return salvaged if salvaged and meta_in_band(salvaged, forbidden) else meta
