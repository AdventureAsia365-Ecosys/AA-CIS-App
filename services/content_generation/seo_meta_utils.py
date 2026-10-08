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
from typing import Optional

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
    from .forbidden_words import has_word
    for term in forbidden:
        nt = _normalize_meta(term)
        if nt and has_word(norm, nt):
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


# ── AA-740: seo_title fit (moved from tenant_pipeline, AA-639) ───────────────────────────────────
SEO_TITLE_MAX = 60  # same limit graph.py validate_node applies (SEO_TITLE_TOO_LONG)
_TITLE_SEPARATORS = (" — ", " – ", " | ", ": ", " - ")
_TITLE_DANGLING = {"and", "&", "with", "of", "in", "to", "for", "the", "a", "an", "—", "–", "-", "|", ":", ","}


def fit_seo_title(title: str, max_len: int = SEO_TITLE_MAX) -> str:
    """AA-639 — shorten an SEO title to max_len without an LLM call. First drop trailing
    separator-delimited segments ("Manaslu Circuit Trek — 18 Days | Nepal" → "Manaslu Circuit
    Trek — 18 Days") while that keeps a meaningful title; otherwise cut at the last word boundary
    and trim a dangling connector/punctuation. Titles already within the limit are unchanged."""
    t = " ".join(title.split())
    if len(t) <= max_len:
        return t
    head = t
    while len(head) > max_len:
        cut = max((head.rfind(sep) for sep in _TITLE_SEPARATORS), default=-1)
        if cut <= 0:
            break
        head = head[:cut].rstrip()
    # a clean leading segment (the tour's own name) beats a phrase cut mid-way
    if len(head) <= max_len and len(head) >= min(15, max_len // 2):
        return head
    words = t[: max_len + 1].split(" ")
    if len(t) > max_len:
        words = words[:-1] if len(words) > 1 else words
    while words and words[-1].lower().strip(",:;") in _TITLE_DANGLING:
        words.pop()
    out = " ".join(words).rstrip(" ,;:—–-|")
    return out[:max_len] if out else t[:max_len]


# ── AA-740: deterministic final seo_meta fit (too long AND too short) ────────────────────────────
# Cut points tried for a one-sentence meta that is too long, strongest boundary first.
_META_CLAUSE_CUTS = ("; ", " — ", " – ", ", and ", ", with ", ", ")
_DAYS_RE = re.compile(r"(\d+)\s*(?:-\s*)?days?\b", re.IGNORECASE)
_NIGHTS_RE = re.compile(r"(\d+)\s*nights?\b", re.IGNORECASE)


def tour_days(duration) -> Optional[int]:
    """Days from a raw duration string ("14 DAYS", "13 Nights 14 days", "1 day"); nights + 1 when
    only nights are given. None when nothing usable."""
    d = str(duration or "")
    m = _DAYS_RE.search(d)
    if m:
        return int(m.group(1))
    m = _NIGHTS_RE.search(d)
    return int(m.group(1)) + 1 if m else None


def _cut_long_meta(meta: str, forbidden) -> Optional[str]:
    t = meta.strip()
    for sep in _META_CLAUSE_CUTS:
        idx = [i for i in range(len(t)) if t.startswith(sep, i)]
        for i in reversed(idx):
            head = t[:i].rstrip(" ,;:—–-")
            words = head.split()
            while words and re.sub(r"[^a-z]", "", words[-1].lower()) in BAD_META_ENDINGS:
                words.pop()
            cand = " ".join(words).rstrip(" ,;:—–-") + "."
            if meta_in_band(cand, forbidden):
                return cand
    # last resort: the longest word-boundary cut that lands in band (drops a trailing connector)
    for end in range(SEO_META_MAX - 1, SEO_META_MIN - 2, -1):
        if end >= len(t) or t[end] != " ":
            continue
        words = t[:end].split()
        while words and re.sub(r"[^a-z]", "", words[-1].lower()) in BAD_META_ENDINGS | _META_WEAK_ENDINGS:
            words.pop()
        cand = " ".join(words).rstrip(" ,;:—–-") + "."
        if meta_in_band(cand, forbidden):
            return cand
    return None


# Words a word-boundary cut must not end on (articles, connectors) besides BAD_META_ENDINGS.
_META_WEAK_ENDINGS = frozenset({"a", "an", "the", "by", "of", "via", "into", "through", "between", "as"})
_MENTIONS_DAYS = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty)[- ](day|night)s?\b", re.IGNORECASE)


def _extend_short_meta(meta: str, facts: dict, forbidden) -> Optional[str]:
    t = meta.strip()
    body = t[:-1] if t.endswith(".") else t
    days = tour_days((facts or {}).get("duration"))
    country = ((facts or {}).get("country") or "").strip()
    if days and _MENTIONS_DAYS.search(body):
        days = None  # the meta already states the length; don't say it twice
    tails = []
    if days and country:
        tails += [f", over {days} days in {country}.", f", a {days}-day trip in {country}.",
                  f" Duration: {days} days in {country}."]
    if days:
        tails += [f", over {days} days.", f", a {days}-day trip.", f" Duration: {days} days."]
    if country:
        tails += [f", in {country}."]
    for tail in tails:
        cand = (body + tail) if not tail.startswith(" Duration") else (body + "." + tail)
        if country and country.lower() in body.lower() and f"in {country}" in tail:
            continue  # don't repeat the country the meta already names
        if meta_in_band(cand, forbidden):
            return cand
    return None


def fit_seo_meta_final(meta: str, facts: Optional[dict] = None, tenant_forbidden=None) -> str:
    """AA-740: land seo_meta in [SEO_META_MIN, SEO_META_MAX] as a complete sentence, without an LLM.
    1) already in band → unchanged; 2) AA-641 salvage (complete-sentence prefix); 3) too long →
    cut at the last clause boundary that lands in band; 4) too short → append one clause built
    only from tour facts (days, country). Still out of band → returned unchanged, so the
    SEO_META_TOO_LONG / META_TOO_SHORT hard code keeps blocking Master (AA-736)."""
    if not isinstance(meta, str) or not meta.strip():
        return meta
    forbidden = set(SEO_META_FORBIDDEN) | {w.lower().strip() for w in (tenant_forbidden or []) if w and w.strip()}
    if meta_in_band(meta, forbidden):
        return meta
    fitted = fit_seo_meta(meta, tenant_forbidden)
    if meta_in_band(fitted, forbidden):
        return fitted
    t = meta.strip()
    if len(t) > SEO_META_MAX:
        return _cut_long_meta(t, forbidden) or meta
    if len(t) < SEO_META_MIN:
        return _extend_short_meta(t, facts or {}, forbidden) or meta
    # in length but not a complete sentence: close it if that is all that is missing
    cand = t.rstrip(" ,;:—–-") + "."
    return cand if meta_in_band(cand, forbidden) else meta
