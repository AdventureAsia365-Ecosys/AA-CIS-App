"""AA-1049 W3 (root ADR 0020 S3, D5) — code-composed T9 contract fields: the SEO title / meta / slug and the counts of
the CTA and FAQ pairs.

`build_seo(...)` runs BEFORE any SEO-fit model call. It keeps the writer's own value when it passes the caller's own
acceptance test (`title_ok` / `meta_ok`: the policy band read from the writing-policy registry, the keyword, scope,
product and authenticity checks of service._fit_seo_meta — the very same predicates, so nothing the fit call would
refuse is ever accepted here), else tries deterministic shortenings of it and keeps the first that passes:

- meta: the leading whole sentences (never a cut inside a sentence: lesson S232, a mid-phrase cut reads broken);
- title: the part before a separator (" | ", " — ", " – ", ": ") — the main title without its tail.

Nothing is invented: a candidate is a prefix of the writer's own value at a sentence or separator boundary. A value
under the band floor cannot be lengthened by code (the S272 country tail was withdrawn), so it still goes to the fit
call, and F4 reports what is left (as on main).

`contract_counts(text, cta)` is the post-assert view the replay harness and the final contract read: CTA lines (the
configured line, word for word) and FAQ question/answer pairs.

Pure: no model call, no DB, no network.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from services.acp_content_writing.quality_gates import faq_pair_count
from services.acp_shared.citation_tags import strip_citation_tags

_SENTENCE_END_RE = re.compile(r"[.!?]\s+(?=[A-Z0-9\"'“‘(])")
# A full stop after one of these tokens (matched case-insensitively on the last token, its leading punctuation and
# final full stop removed) is not a sentence end. An explicit list, not a length rule (review S272: "approx.", "vs.",
# "Prof.", "Capt." ended sentences under the old 1-3 letter rule, while "Bay." / "Hue." / "Spa." did not).
ABBREVIATIONS = frozenset({"mt", "st", "dr", "mr", "mrs", "ms", "prof", "capt", "approx", "vs", "etc", "e.g", "i.e",
                           "incl", "est", "ca", "no", "ave", "rd", "a.m", "p.m"})
_TOKEN_EDGE = "\"'“‘(["


def _not_an_end(head: str) -> bool:
    """True when the full stop that ends `head` belongs to an abbreviation or an initial, not to a sentence: the last
    token is in ABBREVIATIONS, is a single letter ("J."), or has an inner period ("U.S.", "e.g.")."""
    if not head.endswith("."):
        return False
    token = head.rsplit(None, 1)[-1].lstrip(_TOKEN_EDGE)[:-1]
    return token.lower() in ABBREVIATIONS or len(token) == 1 or "." in token
TITLE_SEPARATORS = (" | ", " — ", " – ", ": ")


def meta_candidates(meta: Optional[str]) -> list[str]:
    """The leading whole sentences of a meta, longest first (the whole value excluded)."""
    if not isinstance(meta, str) or not meta.strip():
        return []
    text = " ".join(meta.split())
    ends = [m.start() + 1 for m in _SENTENCE_END_RE.finditer(text) if not _not_an_end(text[:m.start() + 1])]
    return [text[:e] for e in reversed(ends)]


def title_candidates(title: Optional[str]) -> list[str]:
    """The title cut before each separator it contains, longest first (the whole value excluded)."""
    if not isinstance(title, str) or not title.strip():
        return []
    t = " ".join(title.split())
    cuts = {t[:m.start()].rstrip(" :|—–") for sep in TITLE_SEPARATORS for m in re.finditer(re.escape(sep), t)}
    return sorted((c for c in cuts if c and c != t), key=len, reverse=True)


def build_seo(seo_meta: dict, *, title_ok: Callable[[Optional[str]], bool],
              meta_ok: Callable[[Optional[str]], bool]) -> tuple[dict, list[str]]:
    """(seo_meta with each failing field replaced by its first passing code candidate, log of the replacements).
    A field whose writer value passes, or that has no passing candidate, is left as it is."""
    out = dict(seo_meta or {})
    log: list[str] = []
    for key, ok, cands in (("seo_title", title_ok, title_candidates), ("meta_description", meta_ok, meta_candidates)):
        value = out.get(key)
        if ok(value):
            continue
        pick = next((c for c in cands(value) if ok(c)), None)
        if pick is not None:
            out[key] = pick
            log.append(f"{key} shortened in code to {len(pick)} chars (was {len(value or '')})")
    return out, log


def fields_ok(seo_meta: dict, *, title_ok: Callable[[Optional[str]], bool],
              meta_ok: Callable[[Optional[str]], bool]) -> bool:
    return bool(seo_meta) and title_ok(seo_meta.get("seo_title")) and meta_ok(seo_meta.get("meta_description"))


def contract_counts(text: str, cta: Optional[str]) -> dict:
    """The code-composed contract counts of a final text: CTA occurrences (word for word) and FAQ pairs."""
    plain = strip_citation_tags(text or "")
    line = " ".join((cta or "").split())
    return {"cta": plain.count(line) if line else 0, "faq_pairs": faq_pair_count(plain)}
