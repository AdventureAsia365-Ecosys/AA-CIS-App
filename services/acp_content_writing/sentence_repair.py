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
    _FAQ_Q_LINE_RE, _SENTENCE_END_CHARS, _safe_to_delete, content_words, copies, expand_to_sentence,
    integrity_problems, is_sentence_end, near_duplicates, restates, sentence_spans, sentence_tail_end,
    tidy_after_cut,
)
from services.acp_content_writing.quality_gates import OWNED_BY_FIX_STEP

_TAG = r"\[(?:R|F):[^\]]+\]"
_SEP = rf"(?:\s|{_TAG})+"
_F1_QUOTE_RE = re.compile(r"not present in the content seed: '(.+)'\s*$", re.DOTALL)
_F9_CLAIM_RE = re.compile(r'"(.+?)" \(claims: (.+?); source: (.+?)\)(?:; |$)', re.DOTALL)
_TRAILING_TAGS_RE = re.compile(rf"(?:\s?{_TAG})+\s*$")

_BRAND_QUOTE_RE = re.compile(r"^(.+?): '(.+)'\s*$", re.DOTALL)
_ELIGIBLE_GATES = {"F1_grounding", "F9_cta_fact", "F2_brand_forbidden", "F11_brand_name",
                   "F12_scope", "F13_hygiene", "F13_leak", "F15_voice",
                   "F13_repeat", "F17_trip_fact", "F18_rarity"}  # S234 round 3
# Gates whose violations read "<problem>: '<sentence>'" (parsed with _BRAND_QUOTE_RE).
_QUOTED_GATES = {"F2_brand_forbidden", "F11_brand_name", "F12_scope", "F13_hygiene", "F13_leak", "F15_voice",
                 "F13_repeat", "F17_trip_fact", "F18_rarity"}
# S234 round 2 — contract gates a dedicated step in service.py fixes (FAQ regeneration, first-line
# rewrite); like F4_seo_surface they never send a piece to the full rewrite.
OWNED_ELSEWHERE = OWNED_BY_FIX_STEP
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
    if not failed and any(g["gate"] != "F4_seo_surface" and g["gate"] not in OWNED_ELSEWHERE for g in blocking):
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
                if "CTA_NOT_REFLECTED" in v or "unsupported claim(s)" not in v:
                    return None
                claims = _F9_CLAIM_RE.findall(v.split("unsupported claim(s):", 1)[1])
                if not claims:
                    return None
                items += [(s, f"claims {c.strip()} — {why.strip()}", g["gate"]) for s, c, why in claims]
    out: list[dict] = []
    for quote, problem, gate in items:
        span = locate_sentence(content_text, quote)
        if span is None:
            if gate == "F9_cta_fact":
                continue  # S234 round 3 — a judge quote that matches no sentence; the others are still fixed
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
    return any(restates(replacement, s["clean"], ignore) for s in _neighbour_spans(text, start, end))


def _neighbour_spans(text: str, start: int, end: int) -> list[dict]:
    """The NEIGHBOUR_WINDOW sentence spans before and after [start, end) (no headings, hashtag
    lines or FAQ questions)."""
    spans = [s for s in sentence_spans(text) if s["kind"] not in ("heading", "hashtags", "faq_q")]
    before = [s for s in spans if s["end"] <= start][-NEIGHBOUR_WINDOW:]
    after = [s for s in spans if s["start"] >= end][:NEIGHBOUR_WINDOW]
    return before + after


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
    the result only when the integrity check then passes."""
    from services.acp_content_writing.hygiene import orphan_lines
    old = {d["later"]["clean"] for d in near_duplicates(before, ignore, cta)} | set(orphan_lines(before, ignore, cta))
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


def splice_checked(content_text: str, items: list[dict], replacements: list[str], *,
                   ignore: frozenset = frozenset(), cta: Optional[str] = None,
                   moments: Optional[list] = None) -> str:
    """splice() plus the S234 round 2 integrity check (panel §5 fix 7): a repair that leaves a new
    near-verbatim repeat or a paragraph / piece opener with no referent (#19 'What they find at Lake
    Khövsgöl…' after its referent was cut) is reverted — that item keeps its pre-repair sentence
    and gets `"guard": "integrity"` plus the problems. Items are tried one by one so a good repair
    is not lost to a bad neighbour; if the kept set still breaks, every repair is reverted.

    S234 round 3 — the check also catches near-duplicates (a figure told twice, an echoed phrase, an
    orphan line) and, with `moments`, a moment put under another moment's heading; before reverting,
    the NEW later duplicate is deleted when that is safe (`drop_new_duplicates`), and the repair is
    kept when that leaves the piece clean."""
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
    ADD a sentence (the family access caveat) is never a cut. None when nothing could be cut (the
    piece is then held as before). r2 #13 (F1, one sentence) and #7 (F9, one sentence) were held
    at attempt 2/3 with the flagged sentence still in."""
    from services.acp_content_writing.hygiene import _is_cta
    spans = sentence_spans(content_text)
    prose = [sp for sp in spans if sp["kind"] in ("prose", "bullet", "faq_a")]
    first = prose[0]["start"] if prose else -1
    cut_items, reps = [], []
    for item in items:
        if "add ONE" in item.get("problem", ""):
            continue
        k = next((n for n, sp in enumerate(spans) if sp["start"] <= item["start"] < sp["end"]), None)
        if k is None or spans[k]["kind"] == "faq_q" or spans[k]["start"] == first:
            continue
        if _is_cta(spans[k]["clean"], cta) or not _safe_to_delete(spans, k):
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
_FAQ_MARKUP_RE = re.compile(r"^\**\s*Q:\s*|\*+$")

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
    """The deterministic checks on a replacement sentence before it is saved (lesson AA-641: a
    repair needs a guard after it). Returns [{gate, violations, blocking}] — empty means clean.

    Blocking (plan section 4, PR4 — the admin cannot save it): F1 (no number the source lacks), F2
    banned patterns, the brand's forbidden words, F11 (no foreign brand / platform name), and a
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
        qg.gate_brand_name(sentence, grounding, pack.get("brand_name")),
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


__all__ = ["is_sentence_end", "sentence_tail_end", "locate_sentence", "neighbours", "plan", "repeats_neighbour",
           "splice", "splice_checked", "drop_new_duplicates", "delete_flagged", "OWNED_ELSEWHERE", "expand_to_sentence",
           "resolve_span", "tidy_after_cut", "apply_edit", "sanitize_replacement",
           "check_replacement"]
