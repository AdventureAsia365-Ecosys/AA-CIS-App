"""AA-1055 PR 1 — which DataForSEO keywords a writer may be given, shared by S1 and T2.

One filter for every writer that reads bought SEO data (root design `docs/audits/2026-11-08-S273-session/
dfs-cost-audit/design.vi.md` section 3, decision 7):

* `relevant_keywords` — the S1 writer v2 rule (AA-645, S233): a keyword is kept only when it is not transactional
  ("price", "booking", "reviews"...) and every content word of it occurs in the source, so a raw search query is
  never forced into a title. Moved here from `services/content_generation/s1_writer_v2.py` (which re-exports it)
  so T2 uses the same function instead of a copy.
* `is_english` — DataForSEO keywords are English search queries; a rewrite in another language can never carry them
  in its seo_title / seo_meta, so T2 gives none to such a rewrite (and is not scored on them).
* `writer_keywords` — the two together: the keywords a writer gets and the only ones `DFS_INTENT_UNDERUSED` may
  judge it on.

Pure: no I/O, no DataForSEO call.
"""
from __future__ import annotations

from typing import Optional

# S1 writer v2 gives the meta at most 3 keywords (AA-645). T2's writer reads at most 5 (prompts.build_rewrite_prompt
# and graph._apply_seo_keywords_used both slice [:5]); the design allows 3-5 for T2.
S1_MAX_KEYWORDS = 3
T2_MAX_KEYWORDS = 5

_SOURCE_FIELDS = ("name", "summary", "description", "highlights", "itineraries", "country", "duration")


def seo_keyword_texts(seo: Optional[dict]) -> list[str]:
    """The keyword strings of a seo dict, flat (`top_keywords`) or nested (`keywords.top_keywords`), DFS order,
    entries either strings or {"keyword": ...} dicts. Empty strings dropped."""
    seo = seo or {}
    raw = (seo.get("keywords") or {}).get("top_keywords") or seo.get("top_keywords") or []
    out: list[str] = []
    for k in raw:
        kw = (k.get("keyword") if isinstance(k, dict) else k) or ""
        kw = str(kw).strip()
        if kw:
            out.append(kw)
    return out


def relevant_keywords(seo: Optional[dict], tour: dict, limit: int = S1_MAX_KEYWORDS) -> list[str]:
    """DFS keywords the meta may use: not transactional, and every content word occurs in the source (the S233
    `keyword_covered` rule — a raw search query is not forced into a title). At most `limit`, in DFS order."""
    from services.acp_content_writing.quality_gates import keyword_covered
    from services.acp_content_writing.service import _TRANSACTIONAL_RE
    from services.quality.s1_facts import field_text

    source = "\n".join(field_text(tour.get(f)) for f in _SOURCE_FIELDS)
    out: list[str] = []
    for kw in seo_keyword_texts(seo):
        if kw in out or _TRANSACTIONAL_RE.search(kw) or not keyword_covered(source, kw):
            continue
        out.append(kw)
        if len(out) >= limit:
            break
    return out


def is_english(language: Optional[str]) -> bool:
    """True for an English rewrite language: a BCP 47 code with primary subtag `en` ("en-US", "EN-GB", "en"), the
    word "English", or nothing at all (every rewrite defaults to en-US)."""
    code = (language or "").strip().lower()
    if not code:
        return True
    return code.replace("_", "-").split("-", 1)[0] in ("en", "english")


def writer_keywords(seo: Optional[dict], tour: dict, language: Optional[str] = None,
                    limit: int = T2_MAX_KEYWORDS) -> list[str]:
    """The DFS keywords a writer may be given for `tour` written in `language`: none for a non-English rewrite,
    else `relevant_keywords`. Also the only keywords `DFS_INTENT_UNDERUSED` may judge a T2 draft on."""
    if not is_english(language):
        return []
    return relevant_keywords(seo, tour, limit=limit)
