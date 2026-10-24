"""AA-768 (S234 round 4) — the final text-integrity pass, fail closed.

The S234 round 3 panel (§1.5 "What the detector misses", §4 fixes 1 and 8) found the most visible
defects of the round were left by the repair steps themselves, after the last check that could see
them: r3 #19 (approved) carried three duplicated sentences, #12 a doubled CTA ('Talk to an Atlas &
Hearth travel designer about how this journey might be arranged. Talk to an Atlas & Hearth travel
designer about this journey.'), #12/#17 a closing recap repeating an earlier sentence, #3 two blank
lines before a 283-character first line, #18 stray leading spaces, #19 trailing whitespace.

`final_pass` runs as the very last step before a piece is checked and again before it is stored
(after `hygiene.ensure_cta`, `trip_facts.add_family_caveats`, every sentence repair, the FAQ and
first-line steps and the SEO fit): it normalises whitespace, keeps exactly one CTA (the configured
line, word for word), deletes a later duplicate when no pronoun is orphaned, deletes a broken
fragment, and returns what it could not fix. What remains is reported by
`hygiene.find_issues()["repeat"]` — the SAME function the F13_repeat gate and the offline measure
script read (lesson: one value, one function) — so a piece is never approved with a visible
duplicate. Pure: no LLM, no DB.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.hygiene import (
    CTA_WINDOW, _FAQ_HEADING_RE, _HASHTAG_LINE_RE, _HEADING_RE, _cta_brand_word, _is_cta_invite, _safe_to_delete,
    brand_words, cta_norm, dedupe, has_cta_literal, sentence_spans, tidy_after_cut,
)
from services.acp_shared.citation_tags import strip_citation_tags

_HEADING_LINE_RE = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]")

# ---------------------------------------------------------------- whitespace

_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s")


def normalise(text: Optional[str]) -> tuple[str, list[str]]:
    """No blank lines or spaces at the start or end of the piece, no trailing spaces, no leading
    spaces on a line (a markdown list item keeps its indent), one blank line before and after a
    heading, never 3+ newlines. Returns (text, log) — one log line when anything changed."""
    src = text or ""
    lines = []
    for ln in src.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = ln.rstrip()
        if not _LIST_LINE_RE.match(ln):
            ln = ln.lstrip()
        lines.append(ln)
    out: list[str] = []
    for k, ln in enumerate(lines):
        if _HEADING_RE.match(ln):
            if out and out[-1] != "":
                out.append("")
            out.append(ln)
            if k + 1 < len(lines) and lines[k + 1] != "":
                out.append("")
            continue
        out.append(ln)
    new = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return new, (["normalised whitespace (blank lines, leading / trailing spaces)"] if new != src.strip("\n") else [])


# ---------------------------------------------------------------- one CTA

def _body_end(text: str) -> int:
    m = _FAQ_HEADING_RE.search(text or "")
    return m.start() if m else len(text or "")


def _body_prose(text: str) -> list[dict]:
    """Prose / bullet sentences before any FAQ heading, hashtag lines left out, in order."""
    end = _body_end(text)
    return [s for s in sentence_spans(text or "")
            if s["end"] <= end and not s["in_faq"] and s["kind"] in ("prose", "bullet")
            and not _HASHTAG_LINE_RE.fullmatch(s["clean"] or "-")]


def _literal_groups(spans: list[dict], want: str) -> list[dict]:
    """Every occurrence of the CTA in the prose, as a group of consecutive sentences: a CTA of two
    sentences ('Ready for the steppe? Ask WildKind Travel …') or one `sentence_spans` splits ('Ask
    Wild Co. Travel …', 'hello@wild.travel. We reply …') is one occurrence, the same match
    `hygiene.has_cta_literal` makes on the joined body (S234 round 4 review). A group:
    {"start", "end", "clean", "idx": [span indices]}."""
    groups: list[dict] = []
    n = len(spans)
    k = 0
    while k < n:
        hit = None
        for m in range(k, min(n, k + 6)):
            joined = " ".join(s["clean"] for s in spans[k:m + 1])
            if f" {want} " in f" {cta_norm(joined)} ":
                hit = m
                break
        if hit is None:
            k += 1
            continue
        # the occurrence starts at the LAST k that still holds it ('… Ask WildKind Travel for the full
        # itinerary.' must not start one sentence early)
        first = k
        while first < hit and f" {want} " in f" {cta_norm(' '.join(s['clean'] for s in spans[first + 1:hit + 1]))} ":
            first += 1
        part = spans[first:hit + 1]
        groups.append({"start": part[0]["start"], "end": part[-1]["end"],
                       "clean": " ".join(s["clean"] for s in part), "idx": list(range(first, hit + 1))})
        k = hit + 1
    return groups


def _invites_brand(sentence: str, cta: Optional[str]) -> bool:
    """`hygiene._is_cta_invite` with the brand word matched CASE-SENSITIVELY (S234 round 4 review:
    'Most groups get to the trail before sunrise' is not a Trail Pulse CTA)."""
    if not _is_cta_invite(sentence, cta):
        return False
    brand = _cta_brand_word(cta)
    return bool(brand) and re.search(rf"\b{re.escape(brand)}\b", sentence or "") is not None


def cta_occurrences(text: str, cta: Optional[str]) -> tuple[list[dict], list[dict]]:
    """(literal, paraphrase) before any FAQ heading. literal: every occurrence of the configured CTA
    word for word (`cta_norm`), a group of one or more consecutive sentences (`_literal_groups`).
    paraphrase: a sentence that invites the reader to the brand in other words ('Talk to an Atlas &
    Hearth travel designer about how this journey might be arranged.', r3 #12) — searched only among
    the CTA_WINDOW prose sentences on either side of the LAST literal occurrence, in its section (no
    heading between), never the piece's first sentence (the hook), and with the brand word matched
    case-sensitively. A brand mention mid-body is not a CTA (S234 round 4 review). Hashtag lines never
    count."""
    want = cta_norm(cta)
    if not want:
        return [], []
    spans = _body_prose(text)
    lit = _literal_groups(spans, want)
    if not lit:
        return [], []
    used = {i for g in lit for i in g["idx"]}
    last = lit[-1]
    lo, hi = last["idx"][0], last["idx"][-1]
    window = list(range(max(1, lo - CTA_WINDOW), lo)) + list(range(hi + 1, min(len(spans), hi + 1 + CTA_WINDOW)))
    para = []
    for i in window:
        if i in used or i == 0:
            continue
        a, b = sorted((spans[i]["start"], last["start"]))
        if _HEADING_LINE_RE.search((text or "")[a:b]):
            continue  # another section
        if _invites_brand(spans[i]["clean"], cta):
            para.append({**spans[i], "idx": [i]})
    return lit, para


def _delete_span(text: str, span: dict) -> str:
    after = span["end"] + 1 if text[span["end"]:span["end"] + 1] == " " else span["end"]
    return tidy_after_cut(text[:span["start"]] + text[after:])


def _deletable(text: str, span: dict) -> bool:
    """Deleting `span` (a sentence, or a group of them) orphans no pronoun in the sentence after it."""
    spans = sentence_spans(text)
    k = next((n for n, s in enumerate(spans) if s["end"] == span["end"]), None)
    return k is not None and _safe_to_delete(spans, k)


def fix_cta(text: str, cta: Optional[str]) -> tuple[str, list[str]]:
    """Exactly one CTA: a CTA reworded next to the literal line (`cta_occurrences`) is deleted (r3
    #12), and an EARLIER occurrence that is ONLY the CTA is deleted. The last literal occurrence — the
    closing line `hygiene.ensure_cta` placed — is never deleted: when the only other copy is inside a
    longer sentence, both stay and `cta_findings` reports it (S234 round 4 review). A delete happens
    only when no pronoun is orphaned. Returns (text, log)."""
    want = cta_norm(cta)
    log: list[str] = []
    for _ in range(4):
        lit, para = cta_occurrences(text, cta)
        if not lit:
            break
        target = next((s for s in para if _deletable(text, s)), None)
        why = "a reworded CTA next to the configured line"
        if target is None and len(lit) > 1:
            target = next((s for s in lit[:-1] if cta_norm(s["clean"]) == want and _deletable(text, s)), None)
            why = "a second copy of the CTA line"
        if target is None:
            break
        text = _delete_span(text, target)
        log.append(f"deleted {why}: '{target['clean'][:160]}'")
    return text, log


def cta_findings(text: str, cta: Optional[str]) -> list[str]:
    """The CTA is said exactly once, word for word: 'the piece has no …' / '… N times' / '… also in
    other words'. 'Missing' only when `hygiene.has_cta_literal` (what `ensure_cta` reads) agrees, so
    the gate never asks for a CTA the chain will not add. [] without a configured CTA."""
    if not cta_norm(cta):
        return []
    lit, para = cta_occurrences(text, cta)
    out = []
    if not lit:
        if not has_cta_literal(text, cta):
            out.append(f"the configured call to action is missing — add it word for word: '{' '.join(cta.split())}'")
    elif len(lit) > 1:
        out += [f"says the call to action {len(lit)} times — keep one: '{s['clean'][:200]}'" for s in lit[:-1]]
    if lit:
        out += [f"repeats the call to action in other words — cut it: '{s['clean'][:200]}'" for s in para]
    return out


# ---------------------------------------------------------------- broken fragments

_OPEN_CLOSE = (("(", ")"), ("[", "]"), ("“", "”"))


def _drop_tags(text: str) -> str:
    """The paragraph without its citation tags — the shared AA-912 definition, linear time (the old
    body "anything but ']'" was O(n^2) on '[R:[F:[F:…' with no ']'). A dangling '[R:id' opener is dropped too:
    quality_gates.strip_citation_tags removes it before the tenant sees the piece, so it is not an
    unbalanced bracket of the prose."""
    return strip_citation_tags(text)


# not brackets or quotes of the prose (S234 round 4 review): an emoticon (':)', ';-('), an inline
# enumerator ('1) a fleece, 2) a hat', 'a) … b) …' — only when there are ≥ 2, so 'see note b)'
# alone still counts), an inch / seconds mark after a digit (6'2", only when the quotes are odd)
_EMOTICON_RE = re.compile(r"(?<!\S)[:;=][-'^]?[()\[\]DPp](?!\w)")
_ENUMERATOR_RE = re.compile(r"(?<![\w(])(?:\d{1,2}|[a-z])\)")
_INCH_RE = re.compile(r"(?<=\d)[\"″]")


def _unbalanced(paragraph: str) -> bool:
    p = _EMOTICON_RE.sub(" ", _drop_tags(paragraph))
    enum = len(_ENUMERATOR_RE.findall(p))
    if enum >= 2:
        p = _ENUMERATOR_RE.sub(" ", p)
    if any(p.count(a) != p.count(b) for a, b in _OPEN_CLOSE):
        return True
    if p.count('"') % 2 == 1:
        p = _INCH_RE.sub("", p)
    return p.count('"') % 2 == 1


def fragments(text: Optional[str]) -> list[dict]:
    """Broken sentences a repair or a cut leaves: [{"text", "why", "span"}] —
    - a prose / FAQ-answer sentence that starts lower-case ('…water. they mean that…' — 0 of 2,284
      sentences in the 80 stored pieces do);
    - a paragraph with an unbalanced bracket or quote (0 of the 80 pieces).
    A list item may start lower-case. A finite-verb test was tried and dropped: 63 of 2,284 stored
    sentences have no verb a word list can see, nearly all fine (imperatives, plural verbs, 'built')."""
    out: list[dict] = []
    body = text or ""
    for s in sentence_spans(body):
        if s["kind"] in ("prose", "faq_a") and re.match(r"[a-zà-öø-ÿ]", s["clean"]):
            out.append({"text": s["clean"], "why": "starts like a broken sentence (lower-case)", "span": s})
    for para in re.split(r"\n\s*\n", body):
        lines = [ln for ln in para.strip().splitlines() if ln.strip()]
        if not lines or _HEADING_RE.match(lines[0].strip()):
            continue
        if _unbalanced(para):
            clean = " ".join(para.split())
            out.append({"text": clean[:300], "why": "has an unbalanced bracket or quote", "span": None})
    return out


def fix_fragments(text: str, cta: Optional[str] = None) -> tuple[str, list[str]]:
    """Delete a lower-case-start PROSE sentence when no pronoun is orphaned (an FAQ answer and an
    unbalanced paragraph are left for F13_repeat). Returns (text, log)."""
    log: list[str] = []
    for _ in range(4):
        prose = [s for s in sentence_spans(text) if s["kind"] == "prose"]
        first = prose[0]["start"] if prose else -1  # the hook is never cut (a one-line piece stays whole)
        target = next((f["span"] for f in fragments(text)
                       if f["span"] is not None and f["span"]["kind"] == "prose" and f["span"]["start"] != first
                       and _deletable(text, f["span"])), None)
        if target is None:
            break
        text = _delete_span(text, target)
        log.append(f"deleted a broken sentence: '{target['clean'][:160]}'")
    return text, log


# ---------------------------------------------------------------- split / open paragraphs, dangling tails (AA-790)

# A word a sentence never ends on: the article, conjunction or possessive a cut left behind ('…the trail
# climbs to the.', '…swim and.'). Prepositions are left out on purpose: English strands them ('a place worth
# coming back to.', 'what the dzong is made of.', 'the people you travel with.').
_TAIL_WORDS = frozenset({"a", "an", "the", "and", "or", "but", "nor", "than", "its", "their", "your", "our", "my"})
# A paragraph that opens on one of these (or lower-case) after a paragraph with no end mark continues it.
_CONTINUATION_OPENERS = frozenset({
    "and", "but", "or", "nor", "which", "whose", "whom", "who", "where", "while", "whereas", "because",
    "although", "though", "unless", "until", "than", "that", "so",
})
_TERMINAL_TAIL_RE = re.compile(r"[.!?…:;][\"'”’)*\]]*$")
_LAST_WORD_RE = re.compile(r"([A-Za-zÀ-ÖØ-öø-ÿ']+)\W*$")
_FIRST_WORD_RE = re.compile(r"^[\W_]*([A-Za-zÀ-ÖØ-öø-ÿ']+)")
_SMALL_WORDS = frozenset({"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "from"})


def _prose_paragraph(para: str, cta: Optional[str]) -> Optional[str]:
    """The paragraph as one clean line when it is prose a reader reads as running text, else None: not a
    heading, a list item, a hashtag line, a FAQ question / answer line, a bold-only line, a short title-like
    line ('Why Day 2 matters', 'Kuang Si Falls') or the configured CTA line."""
    lines = [ln.strip() for ln in para.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    first, last = lines[0], lines[-1]
    if (_HEADING_RE.match(first) or _HEADING_RE.match(last) or _LIST_LINE_RE.match(last + " ")
            or _HASHTAG_LINE_RE.fullmatch(last) or last.startswith(("**Q:", "A:", "Q:"))
            or (last.startswith("**") and last.endswith("**"))):
        return None
    clean = " ".join(_drop_tags(" ".join(lines)).split())
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ']+", clean)
    if len(words) < 4 or all(w[0].isupper() or w.lower() in _SMALL_WORDS for w in words):
        return None
    if cta_norm(cta) and cta_norm(cta) in cta_norm(clean):
        return None
    return clean


def split_paragraphs(text: Optional[str], cta: Optional[str] = None) -> list[dict]:
    """S234 r4 #18 — a sentence broken across a blank line: a prose paragraph that ends with no end mark,
    followed by a prose paragraph that continues it (opens lower-case, or on 'And' / 'But' / 'Which' /
    'Because' …). [{"text", "why", "span": None}] — the two halves, joined. Paragraph-level, like an unbalanced
    bracket: never a sentence a repair can locate or `fix_fragments` can cut."""
    paras = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    out: list[dict] = []
    for prev, nxt in zip(paras, paras[1:]):
        a = _prose_paragraph(prev, cta)
        if a is None or _TERMINAL_TAIL_RE.search(a):
            continue
        b_lines = [ln.strip() for ln in nxt.strip().splitlines() if ln.strip()]
        if not b_lines or _HEADING_RE.match(b_lines[0]) or _LIST_LINE_RE.match(b_lines[0] + " ") \
                or _HASHTAG_LINE_RE.fullmatch(b_lines[0]) or b_lines[0].startswith(("**", "A:", "Q:")):
            continue
        b = " ".join(_drop_tags(" ".join(b_lines)).split())
        m = _FIRST_WORD_RE.match(b)
        if not m:
            continue
        word = m.group(1)
        if not (word[0].islower() or word.lower() in _CONTINUATION_OPENERS):
            continue
        out.append({"text": f"{a[-150:]} ¶ {b[:150]}", "why": "splits one sentence across two paragraphs",
                    "span": None})
    return out


def open_paragraphs(text: Optional[str], cta: Optional[str] = None) -> list[dict]:
    """A prose paragraph that stops with no end mark and is not continued by the next one (that is
    `split_paragraphs`): what is left when the final pass cuts the lower-case half of a split, or a trim drops
    the paragraph that finished the sentence. 0 of the 70 local stored pieces (AA-729 fixture + S234 label
    pack) have one. [{"text", "why", "span": None}]."""
    paras = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    split = {f["text"].split(" ¶ ")[0] for f in split_paragraphs(text, cta)}
    out: list[dict] = []
    for para in paras:
        a = _prose_paragraph(para, cta)
        if a is None or _TERMINAL_TAIL_RE.search(a) or a[-150:] in split:
            continue
        out.append({"text": a[-300:], "why": "stops with no end mark (an unfinished paragraph)", "span": None})
    return out


def dangling_tails(text: Optional[str]) -> list[dict]:
    """S234 r4 #4 / #12 — a prose, bullet or FAQ-answer sentence cut mid-clause: it ends on an article, a
    conjunction or a possessive ('…before lunch at the.', '…time to swim and'), with or without its end
    mark. [{"text", "why", "span": None}] — reported, never cut in code (the sentence still carries content)."""
    out: list[dict] = []
    for s in sentence_spans(text or ""):
        if s["kind"] not in ("prose", "bullet", "faq_a"):
            continue
        m = _LAST_WORD_RE.search(s["clean"])
        if not m or len(s["clean"].split()) < 3:
            continue
        word = m.group(1)
        if word.islower() and word in _TAIL_WORDS:
            out.append({"text": s["clean"], "why": f"ends mid-clause on '{word}'", "span": None})
    return out


def structure_findings(text: Optional[str], cta: Optional[str] = None) -> list[dict]:
    """AA-790 — the structure defects a deterministic edit can leave that `fragments` does not see:
    `split_paragraphs` + `open_paragraphs` + `dangling_tails`. One function for the repair contract
    (new-vs-before, through `hygiene.integrity_problems`), the census script and the tests (lesson: one value,
    one function). Not read by a gate: under the S247 gate freeze a finding the writer itself made stays a
    census number."""
    return split_paragraphs(text, cta) + open_paragraphs(text, cta) + dangling_tails(text)


# ---------------------------------------------------------------- the pass

def integrity_findings(text: str, cta: Optional[str]) -> list[str]:
    """CTA count + lower-case fragments — appended to `hygiene.find_issues()["repeat"]` (F13_repeat).
    An unbalanced bracket or quote is NOT here (`unbalanced_findings`, a warning): nothing
    deterministic fixes it and its quote is a whole paragraph, not a sentence a repair can locate."""
    return cta_findings(text, cta) + [f"{f['why']} — rewrite or cut it: '{f['text'][:200]}'"
                                      for f in fragments(text) if f["span"] is not None]


def unbalanced_findings(text: str) -> list[str]:
    """A paragraph with an unbalanced bracket or quote — `hygiene.find_issues()["hygiene"]` (F13_hygiene,
    warn-only, S234 round 4 review)."""
    return [f"{f['why']}: '{f['text'][:200]}'" for f in fragments(text) if f["span"] is None]


def edit_findings(old_text: str, new_text: str, cta: Optional[str], brand_name: Optional[str] = None) -> list[str]:
    """What a hand edit (the admin sentence apply path) newly broke: `hygiene.integrity_problems`
    (a new repeat / near-duplicate / orphan line / dangling opener / broken fragment) plus a new CTA
    finding (a second CTA, a reworded one next to it, the CTA cut). Findings the old text already had
    are not repeated. Pure."""
    from services.acp_content_writing.hygiene import integrity_problems
    out = integrity_problems(old_text or "", new_text or "", brand_words(brand_name), cta)
    old_cta = set(cta_findings(old_text or "", cta))
    out += [f for f in cta_findings(new_text or "", cta) if f not in old_cta]
    return list(dict.fromkeys(out))


def final_pass(text: str, hyg, *, cta: Optional[str] = None) -> tuple[str, list[str], list[str]]:
    """The last deterministic step before the gates read a piece and before it is stored:
    normalise → one CTA → delete later duplicates (every `hygiene.near_duplicates` kind that is safe
    to delete, orphan lines, far near-verbatim repeats) → delete broken fragments → normalise.
    Returns (text, log, residue) — residue is `hygiene.find_issues(text, hyg)["repeat"]`, the
    F13_repeat findings left (the caller holds the piece on them)."""
    from services.acp_content_writing.hygiene import find_issues
    cta = cta if cta is not None else getattr(hyg, "cta", None)
    text, log = _fix_all(text, cta, brand_words(getattr(hyg, "brand_name", None)))
    residue = find_issues(text, hyg)["repeat"] if hyg is not None else integrity_findings(text, cta)
    return text, log, residue


def settle(text: str, hyg, *, cta: Optional[str] = None) -> str:
    """AA-790 — the text `final_pass` would leave, without its residue read: what the gates see of an edit made
    before the final pass. The repair contract compares two settled texts, so a step is not reverted for a
    duplicate or a fragment the final pass deletes anyway."""
    cta = cta if cta is not None else getattr(hyg, "cta", None)
    return _fix_all(text, cta, brand_words(getattr(hyg, "brand_name", None)))[0]


def _fix_all(text: str, cta: Optional[str], ignore: frozenset) -> tuple[str, list[str]]:
    text, log = normalise(text)
    text, more = fix_cta(text, cta)
    log += more
    text, more = dedupe(text, ignore, cta)
    log += more
    text, more = fix_fragments(text, cta)
    log += more
    text, more = normalise(text)
    log += [m for m in more if m not in log]
    return text, log


__all__ = ["cta_findings", "cta_occurrences", "dangling_tails", "edit_findings", "final_pass", "fix_cta",
           "fix_fragments", "fragments", "integrity_findings", "normalise", "open_paragraphs", "settle",
           "split_paragraphs", "structure_findings", "unbalanced_findings"]
