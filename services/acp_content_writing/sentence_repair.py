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
the same way F2/F11 do, so they can be repaired here once they block. The sentence primitives
(`is_sentence_end`, `sentence_tail_end`, `expand_to_sentence`, `tidy_after_cut`) live in
hygiene.py — one implementation — and are re-exported here for the AA-769 admin sentence actions.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.hygiene import (
    _SENTENCE_END_CHARS, expand_to_sentence, is_sentence_end, restates, sentence_spans, sentence_tail_end,
    tidy_after_cut,
)

_TAG = r"\[(?:R|F):[^\]]+\]"
_SEP = rf"(?:\s|{_TAG})+"
_F1_QUOTE_RE = re.compile(r"not present in the content seed: '(.+)'\s*$", re.DOTALL)
_F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)
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
    # A quote ending on its own mark is extended from that mark (closers / tags); one ending on
    # "12." of "12.5 km" or "Mt." is not at a sentence end and runs on to the real one.
    tail = sentence_tail_end(text, end - 1 if text[end - 1] in _SENTENCE_END_CHARS else end)
    if tail is None:
        return None
    return m.start(), max(tail, end)


def _seo_fields_only(g: dict) -> bool:
    """A failed gate the SEO title/meta fit owns: F4_seo_surface, or a quoted gate (F12_scope)
    whose every violation is in the title or meta."""
    if g["gate"] == "F4_seo_surface":
        return True
    vs = g.get("violations") or []
    return g["gate"] in _QUOTED_GATES and bool(vs) and all(v.startswith(_SEO_FIELD_PREFIXES) for v in vs)


def plan(content_text: str, gate_ledger: list[dict]) -> Optional[list[dict]]:
    """The sentences to repair, [] when the only failures are a title/meta scope violation the SEO
    fit owns (nothing to change in the text — the attempt re-runs the fit and the gates), or None
    when this attempt needs the full rewrite instead.
    Each item: {"start", "end", "sentence" (tag-stripped), "problem"}."""
    blocking = [g for g in gate_ledger if not g["passed"] and g.get("blocking", True)]
    # F4_seo_surface is fixed by the SEO title/meta fit that runs after every attempt
    # (service._fit_seo_meta), not by the text — it never forces a full rewrite (S233). S234 review:
    # the same for an F12_scope whose violations are all in the title/meta.
    failed = [g for g in blocking if not _seo_fields_only(g)]
    if not failed and any(g["gate"] != "F4_seo_surface" for g in blocking):
        return []
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
           ignore: frozenset = frozenset(), guard_repeats: bool = True) -> str:
    """Put each replacement in place of its sentence (last first, so offsets stay valid). The
    original sentence's trailing citation tags are kept after a non-empty replacement; an empty
    replacement deletes the sentence. S234 guard: a replacement that restates a neighbouring
    sentence is turned into a delete (the item gets `"guard": "repeat"`). `ignore`: words the
    repeat test skips (the brand name). `guard_repeats=False` (the AA-769 admin apply, which
    refuses such a replacement up front in check_replacement) never turns a rewrite into a cut."""
    text = content_text
    for item, new in sorted(zip(items, replacements), key=lambda p: p[0]["start"], reverse=True):
        original = text[item["start"]:item["end"]]
        tags = _TRAILING_TAGS_RE.search(original)
        new = (new or "").strip()
        if new and guard_repeats and repeats_neighbour(text, item["start"], item["end"], new, ignore):
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


# ── AA-769 (S234) — admin cut / rewrite of one flagged sentence ─────────────────────────────────

def resolve_span(text: str, start: int, end: int, expected: str) -> Optional[tuple[int, int]]:
    """Where `expected` (the span the admin saw) sits in the CURRENT text: at (start, end) when it
    still matches there, else at its one and only occurrence. None when it is gone or ambiguous —
    the piece changed since the drawer loaded, so nothing is cut blindly."""
    if not expected:
        return None
    if 0 <= start <= end <= len(text) and text[start:end] == expected:
        return start, end
    if text.count(expected) == 1:
        i = text.index(expected)
        return i, i + len(expected)
    return None


def apply_edit(text: str, start: int, end: int, replacement: str) -> str:
    """The piece text with text[start:end] replaced (or cut, when `replacement` is empty) through
    splice() — trailing citation tags kept after a rewrite, no silent repeat-guard cut (the
    caller checked the replacement) — then tidied (hygiene.tidy_after_cut: an emptied FAQ answer
    goes with its question, an emptied bullet or section goes). A cut that empties a whole line
    (a list item with no end mark) also takes that line's newline. The text keeps its own
    trailing-newline state."""
    if not (replacement or "").strip():
        line_start = start == 0 or text[start - 1] == "\n"
        if line_start and text[end:end + 1] == "\n":
            end += 1
    out = tidy_after_cut(splice(text, [{"start": start, "end": end}], [replacement or ""], guard_repeats=False))
    out = out.rstrip("\n")
    return out + "\n" if text.endswith("\n") and out else out


_SOURCE_LABEL_RE = re.compile(r"\s?\[(?:Moment|Fact) id=[^\]]*\]")


def sanitize_replacement(replacement: str, channel: Optional[str]) -> str:
    """The tenant-facing clean-up the pipeline runs before it persists content_text, applied to an
    admin replacement (lesson: a deterministic fix runs after the LAST LLM step — the suggest
    call is now that step): drop [R:id]/[F:id] citation tags and the SOURCE's [Moment id=…] /
    [Fact id=…] labels the model may echo, strip markdown on plain-text channels, collapse
    whitespace."""
    from services.acp_content_writing.generate import strip_markdown
    from services.acp_content_writing.prompts import PLAIN_TEXT_CHANNELS
    from services.acp_content_writing.quality_gates import strip_citation_tags
    text = _SOURCE_LABEL_RE.sub("", strip_citation_tags(replacement or ""))
    if channel in PLAIN_TEXT_CHANNELS:
        text = strip_markdown(text)
    return " ".join(text.split())


def check_replacement(replacement: str, grounding: str, brand_pack: Optional[dict], *,
                      writer_input: Optional[dict] = None, text: Optional[str] = None,
                      span: Optional[tuple[int, int]] = None) -> list[dict]:
    """The deterministic gates a replacement sentence must pass before it is saved (lesson AA-641:
    a repair needs a guard after it): F1 (no number the source lacks), F2 banned patterns, the
    brand's forbidden words and F11 (no foreign brand / platform name). With the piece's stored
    `writer_input` (S234, AA-768 hygiene) also F12_scope (no place outside the piece's scope) and
    F13_hygiene (no source-notes leak, no unsourced social proof) — the same rules the drawer
    flags, so a rewrite of an F12/F13 sentence cannot bring the problem back. With the piece
    `text` and the sentence `span`, a replacement that restates one of its neighbours fails
    F13_hygiene too (splice would otherwise have turned it into a silent cut). Returns the failed
    gates as [{gate, violations}] — empty means it may be saved."""
    from services.acp_content_writing import hygiene
    from services.acp_content_writing import quality_gates as qg
    sentence = qg.strip_citation_tags(replacement or "").strip()
    if not sentence:
        return []
    pack = brand_pack or {}
    results = [
        qg.gate_grounding(sentence, grounding),
        qg.gate_banned_patterns(sentence, grounding),
        qg.gate_brand_forbidden_words(sentence, grounding, pack.get("forbidden_words")),
        qg.gate_brand_name(sentence, grounding, pack.get("brand_name")),
    ]
    failed = [{"gate": r["gate"], "violations": list(r["violations"])} for r in results if not r["passed"]]
    wi = writer_input or {}
    hygiene_issues: list[str] = []
    if wi.get("seed"):
        ctx = hygiene.HygieneContext(seed=wi["seed"], scope=wi.get("scope"), trip_name=wi.get("trip"),
                                     brand_name=pack.get("brand_name"), cta=wi.get("cta"))
        found = hygiene.find_issues(sentence, ctx)
        if found["scope"]:
            failed.append({"gate": "F12_scope", "violations": list(found["scope"])})
        hygiene_issues += found["hygiene"]
    if text is not None and span is not None and repeats_neighbour(
            text, span[0], span[1], sentence, hygiene.brand_words(pack.get("brand_name"))):
        hygiene_issues.append(f"restates a nearby sentence: '{sentence[:200]}'")
    if hygiene_issues:
        failed.append({"gate": "F13_hygiene", "violations": hygiene_issues})
    return failed


__all__ = ["is_sentence_end", "sentence_tail_end", "locate_sentence", "neighbours", "plan", "repeats_neighbour",
           "splice", "expand_to_sentence", "resolve_span", "tidy_after_cut", "apply_edit", "sanitize_replacement",
           "check_replacement"]
