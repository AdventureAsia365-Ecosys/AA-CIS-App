"""AA-768 — sentence-level repair for T9 fidelity failures.

S232 re-runs: when attempt 1 failed only on invented facts (F1 numbers / F9 unsupported claims),
the attempt-2 full rewrite fixed the flagged sentences but wrote NEW invented ones (trail-pulse:
"pine forest" flagged → "most sacred site" appeared), or kept a flagged one. Results swung 1–3/5
between identical runs.

When every blocking failure is a quoted sentence (F1_grounding or an F9 FACT_CHECK with named
claims), attempt 2 instead rewrites ONLY those sentences: the writer returns one replacement per
sentence and the code splices them in. Everything else stays byte-identical, so no new claim can
appear. Any other failure (CTA, SEO, structure, banned words…) — or a sentence that cannot be
located — falls back to the full rewrite, unchanged.

S234 — `splice` refuses a replacement that restates one of its ±4 neighbouring sentences (it
deletes the sentence instead): the S233 G7 repeats (#2/#9/#14) were repairs that re-stated the
sentence two places earlier. F12_scope / F13_hygiene / F15_voice violations quote their sentence
the same way F2/F11 do, so they can be repaired here once they block. `expand_to_sentence` and
`tidy_after_cut` (hygiene.py) are re-exported for the AA-769 admin sentence actions.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.hygiene import expand_to_sentence, restates, sentence_spans, tidy_after_cut

_TAG = r"\[(?:R|F):[^\]]+\]"
_SEP = rf"(?:\s|{_TAG})+"
_F1_QUOTE_RE = re.compile(r"not present in the content seed: '(.+)'\s*$", re.DOTALL)
_F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)
_SENTENCE_TAIL_RE = re.compile(rf"[^.!?\n]*[.!?]+[\"'”’)]*(?:\s?{_TAG})*")
_TRAILING_TAGS_RE = re.compile(rf"(?:\s?{_TAG})+\s*$")

_BRAND_QUOTE_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)
_ELIGIBLE_GATES = {"F1_grounding", "F9_cta_fact", "F2_brand_forbidden", "F11_brand_name",
                   "F12_scope", "F13_hygiene", "F15_voice"}
# Gates whose violations read "<problem>: '<sentence>'" (parsed with _BRAND_QUOTE_RE).
_QUOTED_GATES = {"F2_brand_forbidden", "F11_brand_name", "F12_scope", "F13_hygiene", "F15_voice"}
# A scope violation in the SEO title or meta is fixed by the SEO fit, not here.
_SEO_FIELD_PREFIXES = ("title ", "meta ")
NEIGHBOUR_WINDOW = 4


def _clean_quote(q: str) -> str:
    q = re.sub(_TAG, " ", q).strip().strip('"“”').strip()
    return " ".join(q.split())


def locate_sentence(text: str, quote: str) -> Optional[tuple[int, int]]:
    """(start, end) of the sentence in `text` that begins with `quote` (citation tags between
    words tolerated, so a quote of the tag-stripped text still matches the tagged piece), extended
    to its own sentence end and trailing tags. None when not found or the quote is too short to
    be unambiguous."""
    words = _clean_quote(quote).split()
    if len(words) < 4:
        return None
    m = re.search(_SEP.join(re.escape(w) for w in words), text)
    if m is None:
        return None
    end = m.end()
    if text[end - 1] not in ".!?":
        tail = _SENTENCE_TAIL_RE.match(text, end)
        if tail is None:
            return None
        end = tail.end()
    else:
        trailing = re.match(rf"[\"'”’)]*(?:\s?{_TAG})*", text[end:])
        end += trailing.end() if trailing else 0
    return m.start(), end


def plan(content_text: str, gate_ledger: list[dict]) -> Optional[list[dict]]:
    """The sentences to repair, or None when this attempt needs the full rewrite instead.
    Each item: {"start", "end", "sentence" (tag-stripped), "problem"}."""
    # F4_seo_surface is fixed by the SEO title/meta fit that runs after every attempt
    # (service._fit_seo_meta), not by the text — it never forces a full rewrite (S233).
    failed = [g for g in gate_ledger if not g["passed"] and g.get("blocking", True)
              and g["gate"] != "F4_seo_surface"]
    if not failed or any(g["gate"] not in _ELIGIBLE_GATES for g in failed):
        return None
    items: list[tuple[str, str]] = []
    for g in failed:
        for v in g["violations"]:
            if g["gate"] in _QUOTED_GATES:
                if v.startswith(_SEO_FIELD_PREFIXES):
                    continue
                m = _BRAND_QUOTE_RE.search(v)
                if not m:
                    return None
                items.append((m.group(2), m.group(1)))
            elif g["gate"] == "F1_grounding":
                m = _F1_QUOTE_RE.search(v)
                if not m:
                    return None
                nums = v.split(" not present", 1)[0].replace("sentence states", "").strip()
                items.append((m.group(1), f"states {nums}, which the source does not contain"))
            else:
                if "CTA_NOT_REFLECTED" in v or "unsupported claim(s)" not in v:
                    return None
                claims = _F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1])
                if not claims:
                    return None
                items += [(s, f"claims {c.strip()} — {why.strip()}") for s, c, why in claims]
    out: list[dict] = []
    for quote, problem in items:
        span = locate_sentence(content_text, quote)
        if span is None:
            return None
        if any(not (span[1] <= o["start"] or span[0] >= o["end"]) for o in out):
            continue  # same sentence flagged twice
        sentence = " ".join(re.sub(_TAG, " ", content_text[span[0]:span[1]]).split())
        out.append({"start": span[0], "end": span[1], "sentence": sentence, "problem": problem})
    return out or None


def repeats_neighbour(text: str, start: int, end: int, replacement: str,
                      ignore: frozenset = frozenset()) -> bool:
    """Does `replacement` (for the sentence at [start, end)) restate one of the
    NEIGHBOUR_WINDOW sentences before or after it?"""
    spans = [s for s in sentence_spans(text) if s["kind"] not in ("heading", "hashtags", "faq_q")]
    before = [s for s in spans if s["end"] <= start][-NEIGHBOUR_WINDOW:]
    after = [s for s in spans if s["start"] >= end][:NEIGHBOUR_WINDOW]
    return any(restates(replacement, s["clean"], ignore) for s in before + after)


def splice(content_text: str, items: list[dict], replacements: list[str], *,
           ignore: frozenset = frozenset()) -> str:
    """Put each replacement in place of its sentence (last first, so offsets stay valid). The
    original sentence's trailing citation tags are kept after a non-empty replacement; an empty
    replacement deletes the sentence. S234 guard: a replacement that restates a neighbouring
    sentence is turned into a delete (the item gets `"guard": "repeat"`). `ignore`: words the
    repeat test skips (the brand name)."""
    text = content_text
    for item, new in sorted(zip(items, replacements), key=lambda p: p[0]["start"], reverse=True):
        original = text[item["start"]:item["end"]]
        tags = _TRAILING_TAGS_RE.search(original)
        new = (new or "").strip()
        if new and repeats_neighbour(text, item["start"], item["end"], new, ignore):
            item["guard"] = "repeat"
            new = ""
        if new:
            new = new + (tags.group(0).rstrip() if tags else "")
            text = text[:item["start"]] + new + text[item["end"]:]
        else:
            after = item["end"] + 1 if text[item["end"]:item["end"] + 1] == " " else item["end"]
            text = text[:item["start"]] + text[after:]
    return text


def neighbours(content_text: str, item: dict, n: int = 2) -> list[str]:
    """The `n` sentences before and after a repair item (tag-stripped) — context for the repair
    call, so it can be told not to restate them."""
    spans = [s for s in sentence_spans(content_text) if s["kind"] not in ("heading", "hashtags")]
    before = [s["clean"] for s in spans if s["end"] <= item["start"]][-n:]
    after = [s["clean"] for s in spans if s["start"] >= item["end"]][:n]
    return before + after


__all__ = ["expand_to_sentence", "locate_sentence", "neighbours", "plan", "repeats_neighbour", "splice",
           "tidy_after_cut"]
