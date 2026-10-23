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
    _FAQ_Q_LINE_RE, _SENTENCE_END_CHARS, _safe_to_delete, content_words, copies, expand_to_sentence, fold,
    integrity_problems, is_sentence_end, near_duplicates, restates, sentence_spans, sentence_tail_end,
    tidy_after_cut,
)
from services.acp_content_writing.quality_gates import OWNED_BY_FIX_STEP
from services.acp_shared.citation_tags import TAG as _TAG
from services.acp_shared.citation_tags import strip_citation_tags, trailing_tags
from services.quality import repair_contract

# AA-912: one shared tag definition (services/acp_shared/citation_tags.py) — linear, and a dangling
# '[R:id' opener is dropped like a whole tag
_SEP = rf"(?:\s|{_TAG})+"
_F1_QUOTE_RE = re.compile(r"not present in the content seed: '(.+)'\s*$", re.DOTALL)
_F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)

_BRAND_QUOTE_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)
_ELIGIBLE_GATES = {"F1_grounding", "F9_cta_fact", "F2_brand_forbidden", "F11_brand_name",
                   "F12_scope", "F13_hygiene", "F13_leak", "F15_voice",
                   "F13_repeat", "F17_trip_fact", "F18_rarity",  # S234 round 3
                   "F19_authenticity",  # AA-819 (AUP 2026-11)
                   "F2_platform_truth", "F2_superlatives"}  # AA-800
# Gates whose violations read "<problem>: '<sentence>'" (parsed with _BRAND_QUOTE_RE).
_QUOTED_GATES = {"F2_brand_forbidden", "F11_brand_name", "F12_scope", "F13_hygiene", "F13_leak", "F15_voice",
                 "F13_repeat", "F17_trip_fact", "F18_rarity", "F19_authenticity",
                 "F2_platform_truth", "F2_superlatives"}  # AA-800
# S234 round 2 — contract gates a dedicated step in service.py fixes (FAQ regeneration, first-line
# rewrite); like F4_seo_surface they never send a piece to the full rewrite.
OWNED_ELSEWHERE = OWNED_BY_FIX_STEP
# A scope violation in the SEO title or meta is fixed by the SEO fit, not here.
_SEO_FIELD_PREFIXES = ("title ", "meta ")
_SLUG_PREFIXES = ("no slug", "slug ")
NEIGHBOUR_WINDOW = 4
# AA-786 — F9 v1.1 broken-sentence violations: "TEXT_INTEGRITY: '<sentence>'".
TEXT_INTEGRITY_PREFIX = "TEXT_INTEGRITY: "
_INTEGRITY_PROBLEM = ("is broken or garbled as text (bad grammar, a doubled call to action, visible markup or a "
                      "thought cut off) — rewrite it as one clean sentence with the same facts, "
                      "or return \"\" to cut it")


def _clean_quote(q: str) -> str:
    q = strip_citation_tags(q, " ").strip().strip('"“”').strip()
    return " ".join(q.split())


def locate_sentence(text: str, quote: str) -> Optional[tuple[int, int]]:
    """(start, end) of the sentence in `text` that begins with `quote` (citation tags between
    words tolerated, so a quote of the tag-stripped text still matches the tagged piece), extended
    to its own sentence end and trailing tags. None when not found or the quote is too short to
    be unambiguous.

    S234 round 3 (r2 §4 fix 7) — a quote from the middle of a sentence (the F9 judge quoting
    'understood exactly what that meant' of 'Two rivers shape … — and the people who placed it there
    understood exactly what that meant.') is widened to its whole sentence, so the repair never
    splices a full sentence into half of one. A quote that is not in the text word for word (the
    judge's paraphrase) is matched to the ONE sentence it copies (hygiene.copies either way)."""
    words = _clean_quote(quote).split()
    if len(words) < 4:
        return None
    m = re.search(_SEP.join(re.escape(w) for w in words), text)
    if m is None:
        return _fuzzy_locate(text, " ".join(words))
    end = m.end()
    # A quote ending on its own mark is extended from that mark (closers / tags); one ending on
    # "12." of "12.5 km" or "Mt." is not at a sentence end and runs on to the real one.
    tail = sentence_tail_end(text, end - 1 if text[end - 1] in _SENTENCE_END_CHARS else end)
    if tail is None:
        return None
    start = m.start()
    span = next((sp for sp in sentence_spans(text) if sp["start"] < start < sp["end"]), None)
    if span is not None and span["kind"] != "faq_q":
        start = span["start"]
    return start, max(tail, end)


# ── AA-984 — an F9 claim on a section heading ────────────────────────────────────────────────────
# S247 r9 781b5de7: the judge flagged '## What a Ruined Monastery Tells Families About Mongolia'. The F9 gate
# keeps it as a blocking body claim (the fold finds it in the body) but locate_sentence cannot locate a line
# with no end mark, so plan() returned None and the piece was held at attempt 2 with no repair and no cut.
# A heading item spans the heading TEXT after its '## ' marker (every splice keeps the marker), is repaired as
# a heading, and is never deleted: an empty or unusable repair falls back to the section's place name.
_HEADING_MARK_RE = re.compile(r"[ \t]*#{1,6}[ \t]+")
_WORD_RE = re.compile(r"\w+")
HEADING_MAX_CHARS = 90
HEADING_PROBLEM = ("this line is a section HEADING, not a sentence: return a short heading (at most 8 words) "
                   "that names the place or moment of its section, with no end mark and no claim the SOURCE "
                   "does not state; never return \"\"")
# A repaired heading ends on none of these ('?' stays: a question heading is a heading).
_HEADING_END_MARKS = ".!;:,…"


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(fold(text or ""))


def _contains_run(big: list[str], small: list[str]) -> bool:
    n = len(small)
    return 0 < n <= len(big) and any(big[i:i + n] == small for i in range(len(big) - n + 1))


def _heading_text_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of the TEXT of every body heading (after its '#' marker), never the FAQ heading."""
    out = []
    for sp in sentence_spans(text):
        if sp["kind"] != "heading" or sp["in_faq"]:
            continue
        m = _HEADING_MARK_RE.match(sp["text"])
        if m and sp["start"] + m.end() < sp["end"]:
            out.append((sp["start"] + m.end(), sp["end"]))
    return out


# AA-984 (S248 review) — how close a judge quote must be to a heading to be a claim ON it (locate_heading).
HEADING_RUN_MIN_SHARE = 0.6
HEADING_QUOTE_SLACK_WORDS = 2


def locate_heading(text: str, quote: str) -> Optional[tuple[int, int]]:
    """AA-984 — (start, end) of the text of the ONE body heading (never the FAQ heading) a judge quote names:
    the same words; a run of ≥ 3 of the heading's words covering ≥ HEADING_RUN_MIN_SHARE of them; or the whole
    heading (≥ 3 words) inside a quote at most HEADING_QUOTE_SLACK_WORDS longer (a '##', a tag). A longer
    quote that contains a heading is a prose claim that paraphrases it, and a short fragment ('for curious
    children') may sit in any sentence — neither is a heading claim (S248 review), so plan() skips them as
    before. The span starts after the '## ' marker. None when no heading or more than one heading matches."""
    q = _words(strip_citation_tags(_clean_quote(quote), " "))
    if len(q) < 2:
        return None
    hits = []
    for start, end in _heading_text_spans(text):
        h = _words(strip_citation_tags(text[start:end], " "))
        if (h == q
                or (len(q) >= 3 and len(q) >= HEADING_RUN_MIN_SHARE * len(h) and _contains_run(h, q))
                or (len(h) >= 3 and len(q) <= len(h) + HEADING_QUOTE_SLACK_WORDS and _contains_run(q, h))):
            hits.append((start, end))
    return hits[0] if len(hits) == 1 else None


def clean_heading(replacement: Optional[str]) -> str:
    """A repair reply as heading text: tag-free, one line, no '#' marker, bold or wrapping quotes, no end mark
    ('?' stays). "" when nothing heading-like is left or it is longer than HEADING_MAX_CHARS."""
    s = " ".join(strip_citation_tags(replacement or "", " ").split())
    s = re.sub(r"^#{1,6}\s*", "", s).strip().strip("*\"'“”‘’ ").rstrip(_HEADING_END_MARKS + " ").strip()
    if not re.search(r"[^\W\d_]{3,}", s) or len(s) > HEADING_MAX_CHARS:
        return ""
    return s


def heading_fallback(content_text: str, item: dict, moments: Optional[list]) -> Optional[str]:
    """AA-984 — the deterministic replacement of a flagged heading: the itinerary label of the moment its
    section names most (ties → the first named), e.g. 'A local Buddhist monastery'. None without moments,
    when the section names no moment, or when another heading already reads the same."""
    if not moments:
        return None
    from services.acp_content_writing.trip_facts import moments_named  # lazy, like _integrity
    spans = sentence_spans(content_text)
    k = next((n for n, sp in enumerate(spans)
              if sp["kind"] == "heading" and sp["start"] <= item["start"] < sp["end"]), None)
    if k is None:
        return None
    counts: dict[int, int] = {}
    first: dict[int, int] = {}
    body: list[str] = []
    for n, sp in enumerate(spans[k + 1:]):
        if sp["kind"] == "heading" or sp["in_faq"]:
            break
        if sp["kind"] not in ("prose", "bullet"):
            continue
        body.append(sp["clean"])
        for m in moments_named(sp["clean"], moments):
            i = moments.index(m)
            counts[i] = counts.get(i, 0) + 1
            first.setdefault(i, n)
    if not counts:
        # no identifying word of any moment (a generic label 'a local Buddhist monastery' is told as 'the
        # Buddhist monastery'): the moment at least half of whose label words the section uses
        have = set(content_words(" ".join(body)))
        for i, m in enumerate(moments):
            label = set(content_words(m.name))
            if label and 2 * len(label & have) >= len(label):
                counts[i], first[i] = len(label & have), i
    if not counts:
        return None
    best = min(counts, key=lambda i: (-counts[i], first[i]))
    name = clean_heading(moments[best].name)
    if not name:
        return None
    name = name[0].upper() + name[1:]
    taken = {" ".join(_words(content_text[s:e])) for s, e in _heading_text_spans(content_text)}
    return None if " ".join(_words(name)) in taken else name


def _fuzzy_locate(text: str, quote: str) -> Optional[tuple[int, int]]:
    """The one prose sentence the quote copies or is copied by (≥ 80% of the shorter side's copy
    words in order); None when no sentence or more than one sentence does."""
    hits = [sp for sp in sentence_spans(text) if sp["kind"] in ("prose", "bullet", "faq_a")
            and (copies(quote, sp["clean"]) or copies(sp["clean"], quote))]
    if len(hits) != 1:
        return None
    return hits[0]["start"], hits[0]["end"]


def _seo_fields_only(g: dict) -> bool:
    """A failed gate the SEO title/meta fit owns: F4_seo_surface, or a quoted gate (F12_scope)
    whose every violation is in the title or meta."""
    if g["gate"] == "F4_seo_surface" or g["gate"] in OWNED_ELSEWHERE:
        return True
    vs = g.get("violations") or []
    # AA-786 — an F9 unsupported claim in the title / meta is SEO-owned the same way.
    return (g["gate"] in _QUOTED_GATES or g["gate"] == "F9_cta_fact") and bool(vs) \
        and all(v.startswith(_SEO_FIELD_PREFIXES) for v in vs)


def _f9_items(v: str, *, allow_cta: bool = False) -> Optional[list[tuple[str, str, str]]]:
    """The (quote, problem, gate) items of one F9_cta_fact violation; [] for a title/meta one (the
    SEO fields own it), None when the violation needs the full rewrite (a CTA failure, a fact flag
    with no named claim). `allow_cta`: parse the claims of a combined CTA + claim violation anyway
    (the LinkedIn cut, which never rewrites the CTA)."""
    if v.startswith(_SEO_FIELD_PREFIXES):
        return []
    if v.startswith(TEXT_INTEGRITY_PREFIX):
        m = _BRAND_QUOTE_RE.search(v)
        return [(m.group(2), _INTEGRITY_PROBLEM, "F9_cta_fact")] if m else None
    if ("CTA_NOT_REFLECTED" in v and not allow_cta) or "unsupported claim(s)" not in v:
        return None
    claims = _F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1])
    if not claims:
        return None
    return [(s, f"claims {c.strip()} — {why.strip()}", "F9_cta_fact") for s, c, why in claims]


def f9_truth_only(g: dict) -> bool:
    """AA-786 — a failed F9_cta_fact whose every violation is product truth about the text (an
    unsupported claim, TEXT_INTEGRITY, a title/meta claim) and none is a missing CTA."""
    vs = g.get("violations") or []
    return (g["gate"] == "F9_cta_fact" and not g["passed"] and bool(vs)
            and not any("CTA_NOT_REFLECTED" in v for v in vs))


def truth_quotes(g: dict) -> list[tuple[str, str]]:
    """AA-786 — every (quote, problem) of one F9_cta_fact entry that is product truth about the body:
    unsupported claims and TEXT_INTEGRITY sentences — never the CTA, the title or the meta."""
    out: list[tuple[str, str]] = []
    for v in g.get("violations") or []:
        out += [(quote, problem) for quote, problem, _ in _f9_items(v, allow_cta=True) or []]
    return out


def truth_items(content_text: str, g: dict) -> tuple[list[dict], list[str]]:
    """AA-786 — (items, unlocated): the located sentences of truth_quotes(g) in plan()'s item shape —
    what the LinkedIn repair-not-hold cuts — and the quotes that match no sentence of the text."""
    out: list[dict] = []
    unlocated: list[str] = []
    for quote, problem in truth_quotes(g):
        span = locate_sentence(content_text, quote)
        if span is None:
            unlocated.append(_clean_quote(quote))
            continue
        if any(not (span[1] <= o["start"] or span[0] >= o["end"]) for o in out):
            continue
        sentence = " ".join(strip_citation_tags(content_text[span[0]:span[1]], " ").split())
        out.append({"start": span[0], "end": span[1], "sentence": sentence, "problem": problem})
    return out, unlocated


def _slug_violation(g: dict) -> bool:
    """An F4 violation about the slug ('no slug' / "slug '…' is not lowercase-kebab" / 'slug is N
    characters'): the SEO fit only folds the slug to ASCII and never writes a new one, so only a
    rewrite (a new writer envelope) can fix it."""
    return g["gate"] == "F4_seo_surface" and any(v.startswith(_SLUG_PREFIXES) for v in g.get("violations") or [])


def plan(content_text: str, gate_ledger: list[dict]) -> Optional[list[dict]]:
    """The sentences to repair, [] when the only failures are a title/meta scope violation the SEO
    fit owns (nothing to change in the text — the attempt re-runs the fit and the gates), or None
    when this attempt needs the full rewrite instead.
    Each item: {"start", "end", "sentence" (tag-stripped), "problem", "gate" (the failed gate, AA-777)}."""
    blocking = [g for g in gate_ledger if not g["passed"] and g.get("blocking", True)]
    # F4_seo_surface is fixed by the SEO title/meta fit that runs after every attempt
    # (service._fit_seo_meta), not by the text — it never forces a full rewrite (S233). S234 review:
    # the same for an F12_scope whose violations are all in the title/meta.
    failed = [g for g in blocking if not _seo_fields_only(g)]
    # AA-776 — that includes an F4 alone: before, an F4-only ledger fell through to None and took a
    # full Sonnet rewrite (S234: 3 at attempt 1) or was held at attempt 2 without the attempt-3 fit
    # (5). A slug violation still rewrites (the fit never writes a slug); the contract gates alone
    # (F16_faq / F14_first_line) still return None — they hold at once.
    if (not failed and any(g["gate"] not in OWNED_ELSEWHERE for g in blocking)
            and not any(_slug_violation(g) for g in blocking)):
        return []
    if not failed or any(g["gate"] not in _ELIGIBLE_GATES for g in failed):
        return None
    items: list[tuple[str, str, str]] = []
    for g in failed:
        for v in g["violations"]:
            if g["gate"] in _QUOTED_GATES:
                if v.startswith(_SEO_FIELD_PREFIXES):
                    continue
                m = _BRAND_QUOTE_RE.search(v)
                if not m:
                    return None
                items.append((m.group(2), m.group(1), g["gate"]))
            elif g["gate"] == "F1_grounding":
                m = _F1_QUOTE_RE.search(v)
                if not m:
                    return None
                nums = v.split(" not present", 1)[0].replace("sentence states", "").strip()
                items.append((m.group(1), f"states {nums}, which the source does not contain", g["gate"]))
            else:
                # AA-786 — F9: unsupported claims and (v1.1) TEXT_INTEGRITY sentences; a title/meta
                # claim is skipped (SEO-owned); a CTA failure still takes the full rewrite.
                f9 = _f9_items(v)
                if f9 is None:
                    return None
                items += f9
    out: list[dict] = []
    for quote, problem, gate in items:
        span = locate_sentence(content_text, quote)
        heading = False
        if span is None and gate == "F9_cta_fact":
            # AA-984 — an F9 claim on a section heading becomes a heading repair item
            span = locate_heading(content_text, quote)
            heading = span is not None
        if span is None:
            if gate == "F9_cta_fact":
                continue  # S234 round 3 — a judge quote that matches no sentence; the others are still fixed
            return None
        hit = next((o for o in out if not (span[1] <= o["start"] or span[0] >= o["end"])), None)
        if heading:
            if hit is None:
                out.append({"start": span[0], "end": span[1], "sentence": _span_text(content_text, span),
                            "problem": f"{problem}; {HEADING_PROBLEM}", "gate": gate, "gates": [gate],
                            "heading": True})
            continue
        if hit is not None:
            _merge_item(content_text, out, hit, span, problem, gate)
            continue
        # AA-777 — `gate` lets the blog's attempt 3 cut the F1/F9 truth items instead of repairing them again
        out.append({"start": span[0], "end": span[1], "sentence": _span_text(content_text, span),
                    "problem": problem, "gate": gate, "gates": [gate]})
    return out or None


# AA-777 (r6 fix 2) — the gate a merged item reports as its `gate`: a product-truth gate first, so the blog's
# attempt-3 truth cut sees an F9 claim that shares its sentence with an F17 finding.
TRUTH_GATES = ("F1_grounding", "F9_cta_fact")


def _span_text(content_text: str, span: tuple[int, int]) -> str:
    return " ".join(strip_citation_tags(content_text[span[0]:span[1]], " ").split())


def _merge_item(content_text: str, out: list[dict], hit: dict, span: tuple[int, int], problem: str, gate: str) -> None:
    """AA-777 (r6 fix 2) — a second finding on (part of) a sentence already in `out` joins that item instead
    of being dropped: the span becomes the union (a two-sentence judge quote over a one-sentence F17 item
    covers both), the problems are joined, `gates` keeps every gate and `gate` prefers a truth gate. Before,
    the later finding was skipped as 'same sentence flagged twice' — r6 a2133050 / 35ee6a3d: the F9 claim
    was never shown to the repair and the attempt-3 truth cut never saw an F1/F9 item. A family-caveat item
    ('add ONE' sentence) is never merged: the first finding wins, as before.

    r7 review — a played-down hazard ('keep the hazard') DOES merge, either way round, so its keep mark is
    never dropped: the merged item then `keeps_sentence` — never cut (`delete_flagged`, the blog attempt-3
    cut) and an empty repair is not spliced (`splice_checked`). Not merging would drop a hazard finding that
    comes second (F17 timing / pace findings and F1 come before it), leaving a cuttable item on the hazard
    sentence. And the union never widens into the piece's first prose sentence (the hook) when `hit` did
    not already hold it: a judge quote over 'hook + S2' joined to an S2 item made the item uncuttable."""
    if _adds_sentence(hit) or "add ONE" in (problem or "") or hit.get("heading"):
        return  # AA-984 — a heading item is never merged with a sentence finding
    start, end = min(hit["start"], span[0]), max(hit["end"], span[1])
    hook = _hook_span(content_text)
    if hook is not None and start < hook[1] <= hit["start"]:
        start = next((sp["start"] for sp in sentence_spans(content_text) if sp["start"] >= hook[1]), hit["start"])
        start = min(start, hit["start"])
    folded: list[dict] = []
    while True:  # the wider span may overlap other items: they join too (never a family-caveat item)
        more = [o for o in out if o is not hit and o not in folded and not (end <= o["start"] or start >= o["end"])]
        if not more:
            break
        if any(_adds_sentence(o) or o.get("heading") for o in more):
            return
        folded += more
        start, end = min([start] + [o["start"] for o in more]), max([end] + [o["end"] for o in more])
    gates = hit.setdefault("gates", [hit["gate"]])
    for o in folded:
        out.remove(o)
    for p, g in [(problem, gate)] + [(o["problem"], gg) for o in folded for gg in (o.get("gates") or [o["gate"]])]:
        if p and p not in hit["problem"]:
            hit["problem"] = f"{hit['problem']}; ALSO: {p}"
        if g not in gates:
            gates.append(g)
    hit["start"], hit["end"] = start, end
    hit["sentence"] = _span_text(content_text, (start, end))
    hit["gate"] = next((g for g in TRUTH_GATES if g in gates), gates[0])


def repeats_neighbour(text: str, start: int, end: int, replacement: str,
                      ignore: frozenset = frozenset()) -> bool:
    """Does `replacement` (for the sentence at [start, end)) restate one of the
    NEIGHBOUR_WINDOW sentences before or after it?"""
    return any(restates(replacement, s["clean"], ignore) for s in _neighbour_spans(text, start, end))


def _neighbour_spans(text: str, start: int, end: int) -> list[dict]:
    """The NEIGHBOUR_WINDOW sentence spans before and after [start, end) (no headings, hashtag
    lines or FAQ questions)."""
    spans = [s for s in sentence_spans(text) if s["kind"] not in ("heading", "hashtags", "faq_q")]
    before = [s for s in spans if s["end"] <= start][-NEIGHBOUR_WINDOW:]
    after = [s for s in spans if s["start"] >= end][:NEIGHBOUR_WINDOW]
    return before + after


def _adds_sentence(item: dict) -> bool:
    """A repair item that asks to keep its sentence and ADD one (trip_facts family caveat)."""
    return "add ONE" in (item.get("problem") or "")


# AA-777 (r7 review) — a finding whose sentence must stay: the family caveat ('keep this sentence and add ONE')
# and a played-down hazard ('keep the hazard, cut the reassurance', trip_facts.minimised_hazards).
_KEEP_MARKS = ("add ONE", "keep the hazard")


def keeps_sentence(problem: Optional[str]) -> bool:
    """Is this a finding (or a merged item's joined problems) whose sentence is never cut?"""
    return any(m in (problem or "") for m in _KEEP_MARKS)


def _keeps_hazard(item: dict) -> bool:
    return "keep the hazard" in (item.get("problem") or "")


def _hook_span(content_text: str) -> Optional[tuple[int, int]]:
    """The piece's first prose sentence (the hook; the same kinds delete_flagged reads), or None."""
    prose = [sp for sp in sentence_spans(content_text) if sp["kind"] in ("prose", "bullet", "faq_a")]
    return (prose[0]["start"], prose[0]["end"]) if prose else None


def _added_part(original: str, replacement: str) -> str:
    """The sentences of `replacement` that are not the original sentence kept (copied either way)."""
    orig = " ".join(strip_citation_tags(original, " ").split())
    spans = [s["clean"] for s in sentence_spans(replacement) if s["kind"] not in ("heading", "hashtags")]
    return " ".join(s for s in spans if not (s == orig or copies(s, orig) or copies(orig, s)))


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
        if item.get("heading"):
            # AA-984 — a heading is rewritten as a heading (its '## ' marker sits before the span) and never
            # deleted: an empty or unusable reply takes the item's place-name fallback, else the heading stays
            new = clean_heading(new) or clean_heading(item.get("fallback"))
            if not new:
                item["guard"] = "heading_kept"
            elif new != original.strip():
                text = text[:item["start"]] + new + text[item["end"]:]
            continue
        tags = trailing_tags(original)  # AA-912: one backward pass (the regex search was O(n^2))
        new = (new or "").strip()
        if new and guard_repeats and _adds_sentence(item):
            # 'keep this sentence and add ONE' (the family caveat): only the ADDED part is tested, and
            # an added part that restates a neighbour leaves the sentence as it was — never a cut of
            # the anchor (S234 round 3 review: r1 #19's caveat restated its access/permit neighbour)
            added = _added_part(item.get("sentence") or original, new)
            if added and repeats_neighbour(text, item["start"], item["end"], added, ignore):
                item["guard"] = "repeat_kept"
                continue
        elif new and guard_repeats and repeats_neighbour(text, item["start"], item["end"], new, ignore):
            if _keeps_hazard(item):  # r7 review — the hazard sentence is never turned into a cut
                item["guard"] = "repeat_kept"
                continue
            item["guard"] = "repeat"
            new = ""
        if new:
            new = new + tags.rstrip()
            text = text[:item["start"]] + new + text[item["end"]:]
        else:
            after = item["end"] + 1 if text[item["end"]:item["end"] + 1] == " " else item["end"]
            text = text[:item["start"]] + text[after:]
    return text


def _integrity(before: str, after: str, ignore: frozenset, cta: Optional[str], moments: Optional[list]) -> list[str]:
    """hygiene.integrity_problems plus (S234 round 3) a moment the repair put under another moment's
    heading (trip_facts.new_misplaced; r2 #4 Sod Camp in the lake section)."""
    out = integrity_problems(before, after, ignore, cta)
    if moments:
        from services.acp_content_writing.trip_facts import new_misplaced
        out += new_misplaced(before, after, moments, cta)
    return out


def drop_new_duplicates(before: str, after: str, ignore: frozenset = frozenset(), cta: Optional[str] = None,
                        moments: Optional[list] = None) -> str:
    """S234 round 3 (r2 §4 fix 2) — before a repair is reverted, delete what it duplicated: every
    sentence of `after` that is a NEW later near-duplicate (hygiene.near_duplicates), a new orphan
    line, or a new misplaced moment sentence, when deleting it orphans no pronoun. The caller keeps
    the result only when the integrity check then passes.

    Only a sentence the repair WROTE is ever deleted (one whose text is not a sentence of `before`):
    when the repair puts a figure into an EARLIER sentence, the later duplicate is the writer's own
    line (often the core fact of another section) — it stays, and the integrity check reverts the
    repair instead (S234 round 3 review)."""
    from services.acp_content_writing.hygiene import orphan_lines
    old = {d["later"]["clean"] for d in near_duplicates(before, ignore, cta)} | set(orphan_lines(before, ignore, cta))
    old |= {sp["clean"] for sp in sentence_spans(before)}
    text = after
    for _ in range(6):
        targets = [d["later"]["clean"] for d in near_duplicates(text, ignore, cta)]
        targets += orphan_lines(text, ignore, cta)
        if moments:
            from services.acp_content_writing.trip_facts import misplaced_sentences
            had = set(misplaced_sentences(before, moments, cta))
            targets += [m for m in misplaced_sentences(text, moments, cta) if m not in had]
        targets = [t for t in targets if t not in old]
        if not targets:
            break
        spans = sentence_spans(text)
        k = next((n for n, sp in enumerate(spans) if sp["clean"] == targets[0]), None)
        if k is None or not _safe_to_delete(spans, k):
            break
        sp = spans[k]
        cut = sp["end"] + 1 if text[sp["end"]:sp["end"] + 1] == " " else sp["end"]
        text = tidy_after_cut(text[:sp["start"]] + text[cut:])
    return text


# AA-925: new_specifics lives in the repair contract now (services/quality/repair_contract.py, invariant 2);
# re-exported here so the AA-777 call sites and tests keep their import.
new_specifics = repair_contract.new_specifics


def _drop_new_specifics(items: list[dict], replacements: list[str], trip_ctx,
                        source: Optional[str]) -> tuple[list[dict], list[str]]:
    """The (items, replacements) to splice: a replacement that adds a specific (`new_specifics`) is not
    spliced — its sentence stays as it was and the item gets `"guard": "specifics"` plus the findings."""
    keep_items, keep_reps = [], []
    for item, rep in zip(items, replacements):
        added = new_specifics(item.get("sentence") or "", rep, trip_ctx=trip_ctx, source=source)
        if added:
            item["guard"], item["specifics"] = "specifics", added
            continue
        keep_items.append(item)
        keep_reps.append(rep)
    return keep_items, keep_reps


def splice_checked(content_text: str, items: list[dict], replacements: list[str], *,
                   ignore: frozenset = frozenset(), cta: Optional[str] = None,
                   moments: Optional[list] = None, trip_ctx=None, source: Optional[str] = None) -> str:
    """splice() plus the S234 round 2 integrity check (panel §5 fix 7): a repair that leaves a new
    near-verbatim repeat or a paragraph / piece opener with no referent (#19 'What they find at Lake
    Khövsgöl…' after its referent was cut) is reverted — that item keeps its pre-repair sentence
    and gets `"guard": "integrity"` plus the problems. Items are tried one by one so a good repair
    is not lost to a bad neighbour; if the kept set still breaks, every repair is reverted.

    S234 round 3 — the check also catches near-duplicates (a figure told twice, an echoed phrase, an
    orphan line) and, with `moments`, a moment put under another moment's heading; before reverting,
    the NEW later duplicate is deleted when that is safe (`drop_new_duplicates`), and the repair is
    kept when that leaves the piece clean.

    AA-777 (r6 fix 1) — with `source` (the F1 grounding text) and/or `trip_ctx` (the job's
    TripFactContext), a replacement that ADDS a number, time, position or (blog) pace word the original
    sentence and the source lack is not spliced first (`new_specifics`; the item keeps its sentence,
    `"guard": "specifics"`).

    AA-777 (r7 review) — an empty replacement (a cut) of a played-down-hazard item is not spliced either: the
    item keeps its sentence (`"guard": "hazard_kept"`), so the hazard is never lost to a repair."""
    pairs = []
    for it, rep in zip(items, replacements):
        if it.get("heading"):
            # AA-984 — the heading text this item will get: the cleaned reply, else the place-name fallback
            fixed = clean_heading(rep)
            if not fixed:
                fixed = heading_fallback(content_text, it, moments) or ""
                it["fallback_used"] = bool(fixed)
            if not fixed:
                it["guard"] = "heading_kept"
                continue
            pairs.append((it, fixed))
        elif _keeps_hazard(it) and not (rep or "").strip():
            it["guard"] = "hazard_kept"
        else:
            pairs.append((it, rep))
    if not pairs:
        return content_text
    # AA-984 — always from `pairs`: a heading item's replacement is its cleaned reply or its fallback
    items, replacements = [p[0] for p in pairs], [p[1] for p in pairs]
    if trip_ctx is not None or source is not None:
        items, replacements = _drop_new_specifics(items, replacements, trip_ctx, source)
        if not items:
            return content_text
    # AA-925 repair contract (invariants 3 and 5): a replacement that ends mid-sentence, is a cut-and-period of
    # its sentence, or breaks a tag / bold / bracket pair is not spliced (`"guard": "contract"`); the caller
    # counts these items as `degraded: repair_contract`.
    items, replacements, _ = repair_contract.enforce_spans(items, replacements)
    if not items:
        return content_text
    out = splice(content_text, items, replacements, ignore=ignore)
    if not _integrity(content_text, out, ignore, cta, moments):
        return out
    deduped = drop_new_duplicates(content_text, out, ignore, cta, moments)
    if deduped != out and not _integrity(content_text, deduped, ignore, cta, moments):
        for item in items:
            item.setdefault("guard", "dedupe")
        return deduped
    keep_items, keep_reps = [], []
    for item, rep in zip(items, replacements):
        trial = splice(content_text, [dict(item)], [rep], ignore=ignore)
        problems = _integrity(content_text, trial, ignore, cta, moments)
        if problems and _integrity(content_text, drop_new_duplicates(content_text, trial, ignore, cta, moments),
                                   ignore, cta, moments):
            item["guard"], item["integrity"] = "integrity", problems
        else:
            keep_items.append(item)
            keep_reps.append(rep)
    out = splice(content_text, keep_items, keep_reps, ignore=ignore)
    out = drop_new_duplicates(content_text, out, ignore, cta, moments)
    problems = _integrity(content_text, out, ignore, cta, moments)
    if problems:
        for item in keep_items:
            item["guard"], item["integrity"] = "integrity", problems
        return content_text
    return out


def delete_flagged(content_text: str, items: list[dict], *, ignore: frozenset = frozenset(),
                   cta: Optional[str] = None, moments: Optional[list] = None) -> Optional[str]:
    """S234 round 3 (r2 §4 fix 7) — the deterministic fallback when an attempt-3 sentence repair
    cannot run (the repair call failed or returned a bad reply): cut each flagged sentence that is
    safe to cut — not the piece's first prose sentence (the hook), not the CTA, not a FAQ question,
    no pronoun orphaned — and keep the result when the integrity check passes. An item that asks to
    ADD a sentence (the family access caveat) or keep a hazard is never a cut. None when nothing could be cut
    (the piece is then held as before). r2 #13 (F1, one sentence) and #7 (F9, one sentence) were held
    at attempt 2/3 with the flagged sentence still in.

    AA-777 (r7 review) — an item can span several sentences (a merged plan() item, a two-sentence judge
    quote): EVERY sentence inside it is checked (hook, CTA, FAQ question, heading), and the orphaned-pronoun
    test reads the first sentence AFTER the item, not the item's own second sentence.

    AA-984 — a heading item (plan(): an F9 claim on a section heading) is never cut: with `moments` it is
    replaced by its section's place name (heading_fallback), else it stays."""
    from services.acp_content_writing.hygiene import _is_cta
    spans = sentence_spans(content_text)
    prose = [sp for sp in spans if sp["kind"] in ("prose", "bullet", "faq_a")]
    first = prose[0]["start"] if prose else -1
    cut_items, reps = [], []
    for item in items:
        if item.get("heading"):
            # AA-984 — a flagged heading is never cut: it takes its section's place name, or stays
            fallback = heading_fallback(content_text, item, moments)
            if fallback:
                cut_items.append(dict(item, fallback=fallback))
                reps.append(fallback)
            continue
        if keeps_sentence(item.get("problem")):
            continue
        ks = [n for n, sp in enumerate(spans) if sp["start"] < item["end"] and item["start"] < sp["end"]]
        if not ks or any(spans[k]["kind"] in ("faq_q", "heading", "hashtags") or spans[k]["start"] == first
                         or _is_cta(spans[k]["clean"], cta) for k in ks):
            continue
        if not _safe_to_delete(spans, ks[-1]):
            continue
        cut_items.append(dict(item))
        reps.append("")
    if not cut_items:
        return None
    out = tidy_after_cut(splice(content_text, cut_items, reps, ignore=ignore, guard_repeats=False))
    if _integrity(content_text, out, ignore, cta, moments):
        return None
    return out


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


def _faq_question_at(text: str, start: int, end: int) -> Optional[tuple[int, int, int, int]]:
    """When [start, end) lies on a FAQ question line ("**Q: …?**"): (line start, line end,
    question start, question end), else None."""
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", start)
    le = len(text) if le < 0 else le
    if end > le:
        return None
    q = _FAQ_Q_LINE_RE.fullmatch(text, ls, le)
    return (ls, le, q.start(2), q.end(2)) if q else None


def _faq_block_end(text: str, line_end: int) -> int:
    """End of the answer that follows a FAQ question line ending at `line_end`: the "A:" line and
    any non-blank lines after it, up to a blank line, the next question or a heading."""
    pos = line_end
    first = True
    while pos < len(text):
        nxt = text.find("\n", pos + 1)
        nxt = len(text) if nxt < 0 else nxt
        line = text[pos + 1:nxt].strip()
        if not line or line.startswith("**Q:") or line.startswith("#") or (first and not line.startswith("A:")):
            break
        pos, first = nxt, False
    return pos


def apply_edit(text: str, start: int, end: int, replacement: str) -> str:
    """The piece text with text[start:end] replaced (or cut, when `replacement` is empty) through
    splice() — trailing citation tags kept after a rewrite, no silent repeat-guard cut (the
    caller checked the replacement) — then tidied (hygiene.tidy_after_cut: an emptied FAQ answer
    goes with its question, an emptied bullet or section goes). A cut that empties a whole line
    (a list item with no end mark) also takes that line's newline. The text keeps its own
    trailing-newline state."""
    faq_q = _faq_question_at(text, start, end)
    if faq_q:
        ls, le, qs, qe = faq_q
        if not (replacement or "").strip():
            # S234 review: a cut question takes its answer with it (never an orphaned "A:")
            start, end = ls, _faq_block_end(text, le)
            if text[end:end + 1] == "\n":
                end += 1
        else:
            # a rewrite replaces the question text only; the "**Q: " … "**" markup stays
            replacement = _FAQ_MARKUP_RE.sub("", replacement.strip()).strip()
            if start <= qs and end >= qe:
                start, end = qs, qe
    elif not (replacement or "").strip():
        line_start = start == 0 or text[start - 1] == "\n"
        if line_start and text[end:end + 1] == "\n":
            end += 1
    out = tidy_after_cut(splice(text, [{"start": start, "end": end}], [replacement or ""], guard_repeats=False))
    out = out.rstrip("\n")
    return out + "\n" if text.endswith("\n") and out else out


# "**Q: " / "Q: " / "**" an admin may type around a rewritten FAQ question (the line keeps its own)
# AA-912: the trailing run is tried only from its first '*' (was O(n^2) on a long star run; same matches)
_FAQ_MARKUP_RE = re.compile(r"^\**\s*Q:\s*|(?<!\*)\*+$")

# AA-912: same rules as a citation tag (services/acp_shared/citation_tags.py): the id body never holds a
# bracket or a newline (linear), and a dangling '[Fact id=f1' opener is dropped with its id, never the prose after it
_SOURCE_LABEL_RE = re.compile(r"\s?\[(?:Moment|Fact) id=(?:[^\[\]\n]*\]|[\w-]*)")


def sanitize_replacement(replacement: str, channel: Optional[str]) -> str:
    """The tenant-facing clean-up the pipeline runs before it persists content_text, applied to an
    admin replacement (lesson: a deterministic fix runs after the LAST LLM step — the suggest
    call is now that step): drop [R:id]/[F:id] citation tags and the SOURCE's [Moment id=…] /
    [Fact id=…] labels the model may echo, strip markdown on plain-text channels, collapse
    whitespace, and replace em-dashes (hygiene.fix_dashes, AA-778: F2 blocks a residue em-dash, so an
    admin's or the suggest call's dash is converted here rather than refused)."""
    from services.acp_content_writing.generate import strip_markdown
    from services.acp_content_writing.hygiene import fix_dashes
    from services.acp_content_writing.prompts import PLAIN_TEXT_CHANNELS
    text = _SOURCE_LABEL_RE.sub("", strip_citation_tags(replacement))
    if channel in PLAIN_TEXT_CHANNELS:
        text = strip_markdown(text)
    return fix_dashes(" ".join(text.split()))[0]


def check_replacement(replacement: str, grounding: str, brand_pack: Optional[dict], *,
                      writer_input: Optional[dict] = None, text: Optional[str] = None,
                      span: Optional[tuple[int, int]] = None) -> list[dict]:
    """The deterministic checks on a replacement sentence before it is saved (lesson AA-641: a
    repair needs a guard after it). Returns [{gate, violations, blocking}] — empty means clean.

    Blocking (plan section 4, PR4 — the admin cannot save it): F1 (no number the source lacks), F2
    banned patterns, the brand's forbidden words, F11 (no foreign brand / platform name), F19 (no
    review, testimonial, customer first person or independence claim — AA-819), and a
    near-verbatim copy of one of its ±4 neighbours (hygiene.near_verbatim — the rule the pipeline
    auto-deletes on; `splice` would otherwise have turned it into a silent cut).

    Warning only (S234 review — shown, never refused): with the piece's stored `writer_input`,
    F12_scope (a place outside the piece's scope; the lexicon still has false positives, e.g.
    'Mongol' on a Mongolia trip, and F12 stays warn until the replay shows none) and F13_hygiene
    (source-notes leak, unsourced social proof); with `text` and `span`, a looser restatement of a
    neighbour (hygiene.restates — it also matches a brand line that shares one phrase)."""
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
        qg.gate_platform_truth(sentence, grounding),  # AA-800
        qg.gate_brand_name(sentence, grounding, pack.get("brand_name")),
        # AA-819 — an admin suggestion (t9_repair) or edit never puts a review / customer voice in
        qg.gate_authenticity(sentence, (writer_input or {}).get("cta")),
    ]
    out = [{"gate": r["gate"], "violations": list(r["violations"]), "blocking": True}
           for r in results if not r["passed"]]
    wi = writer_input or {}
    repeat_block: list[str] = []
    warn_hygiene: list[str] = []
    if wi.get("seed"):
        ctx = hygiene.HygieneContext(seed=wi["seed"], scope=wi.get("scope"), trip_name=wi.get("trip"),
                                     trip_places=tuple(wi.get("trip_places") or ()),
                                     brand_name=pack.get("brand_name"), cta=wi.get("cta"),
                                     audience=pack.get("customer_segment"), facts=wi.get("facts"))
        found = hygiene.find_issues(sentence, ctx)
        if found["scope"]:
            out.append({"gate": "F12_scope", "violations": list(found["scope"]), "blocking": False})
        # S234 round 2 — leaks are blocking in the pipeline (F13_leak) but a warning on an admin's text;
        # "opens on a pronoun" is meaningless for one sentence out of its piece.
        warn_hygiene += found["hygiene"]
        warn_hygiene += [v for v in found["leak"] if not v.startswith("opens on a pronoun")]
    if text is not None and span is not None:
        ignore = hygiene.brand_words(pack.get("brand_name"))
        near = _neighbour_spans(text, span[0], span[1])
        if any(hygiene.near_verbatim(sentence, s["clean"], ignore) for s in near):
            repeat_block.append(f"repeats a nearby sentence almost word for word: '{sentence[:200]}'")
        elif any(hygiene.restates(sentence, s["clean"], ignore) for s in near):
            warn_hygiene.append(f"may restate a nearby sentence: '{sentence[:200]}'")
    if repeat_block:
        out.append({"gate": "F13_hygiene", "violations": repeat_block, "blocking": True})
    if warn_hygiene:
        out.append({"gate": "F13_hygiene", "violations": warn_hygiene, "blocking": False})
    return out


__all__ = ["is_sentence_end", "sentence_tail_end", "locate_sentence", "locate_heading", "clean_heading",
           "heading_fallback", "neighbours", "plan", "repeats_neighbour",
           "splice", "splice_checked", "drop_new_duplicates", "delete_flagged", "OWNED_ELSEWHERE", "expand_to_sentence",
           "resolve_span", "tidy_after_cut", "apply_edit", "sanitize_replacement",
           "check_replacement"]
