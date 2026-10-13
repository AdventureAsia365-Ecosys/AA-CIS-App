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
    _FAQ_HEADING_RE, _HASHTAG_LINE_RE, _HEADING_RE, _is_cta_invite, _safe_to_delete, brand_words, cta_norm,
    dedupe, sentence_spans, tidy_after_cut,
)

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


def cta_occurrences(text: str, cta: Optional[str]) -> tuple[list[dict], list[dict]]:
    """(literal, paraphrase) — prose sentences before any FAQ heading that carry the configured CTA
    word for word (`cta_norm`), and the ones that invite the reader to the brand in other words
    (`hygiene._is_cta_invite`: 'Talk to an Atlas & Hearth travel designer about how this journey might
    be arranged.'). Hashtag lines never count."""
    want = cta_norm(cta)
    if not want:
        return [], []
    end = _body_end(text)
    lit, para = [], []
    for s in sentence_spans(text or ""):
        if s["end"] > end or s["in_faq"] or s["kind"] not in ("prose", "bullet"):
            continue
        if _HASHTAG_LINE_RE.fullmatch(s["clean"] or "-"):
            continue
        if f" {want} " in f" {cta_norm(s['clean'])} ":
            lit.append(s)
        elif _is_cta_invite(s["clean"], cta):
            para.append(s)
    return lit, para


def _delete_span(text: str, span: dict) -> str:
    after = span["end"] + 1 if text[span["end"]:span["end"] + 1] == " " else span["end"]
    return tidy_after_cut(text[:span["start"]] + text[after:])


def _deletable(text: str, span: dict) -> bool:
    spans = sentence_spans(text)
    k = next((n for n, s in enumerate(spans) if s["start"] == span["start"]), None)
    return k is not None and _safe_to_delete(spans, k)


def fix_cta(text: str, cta: Optional[str]) -> tuple[str, list[str]]:
    """Exactly one CTA: a CTA reworded next to the literal line is deleted (r3 #12), and a second
    literal line that is ONLY the CTA is deleted (the last one stays — the closing line). A delete
    happens only when no pronoun is orphaned. Returns (text, log)."""
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
            if target is None and cta_norm(lit[-1]["clean"]) == want and _deletable(text, lit[-1]):
                target = lit[-1]
            why = "a second copy of the CTA line"
        if target is None:
            break
        text = _delete_span(text, target)
        log.append(f"deleted {why}: '{target['clean'][:160]}'")
    return text, log


def cta_findings(text: str, cta: Optional[str]) -> list[str]:
    """The CTA is said exactly once, word for word: 'the piece has no …' / '… N times' / '… also in
    other words'. [] without a configured CTA."""
    if not cta_norm(cta):
        return []
    lit, para = cta_occurrences(text, cta)
    out = []
    if not lit:
        out.append(f"the configured call to action is missing — add it word for word: '{' '.join(cta.split())}'")
    elif len(lit) > 1:
        out += [f"says the call to action {len(lit)} times — keep one: '{s['clean'][:200]}'" for s in lit[:-1]]
    if lit:
        out += [f"repeats the call to action in other words — cut it: '{s['clean'][:200]}'" for s in para]
    return out


# ---------------------------------------------------------------- broken fragments

_OPEN_CLOSE = (("(", ")"), ("[", "]"), ("“", "”"))
_TAG_RE = re.compile(r"\[(?:R|F):[^\]]+\]")


def _unbalanced(paragraph: str) -> bool:
    p = _TAG_RE.sub("", paragraph)
    if any(p.count(a) != p.count(b) for a, b in _OPEN_CLOSE):
        return True
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


# ---------------------------------------------------------------- the pass

def integrity_findings(text: str, cta: Optional[str]) -> list[str]:
    """CTA count + broken fragments — appended to `hygiene.find_issues()["repeat"]` (F13_repeat)."""
    return cta_findings(text, cta) + [f"{f['why']} — rewrite or cut it: '{f['text'][:200]}'" for f in fragments(text)]


def final_pass(text: str, hyg, *, cta: Optional[str] = None) -> tuple[str, list[str], list[str]]:
    """The last deterministic step before the gates read a piece and before it is stored:
    normalise → one CTA → delete later duplicates (every `hygiene.near_duplicates` kind that is safe
    to delete, orphan lines, far near-verbatim repeats) → delete broken fragments → normalise.
    Returns (text, log, residue) — residue is `hygiene.find_issues(text, hyg)["repeat"]`, the
    F13_repeat findings left (the caller holds the piece on them)."""
    from services.acp_content_writing.hygiene import find_issues
    cta = cta if cta is not None else getattr(hyg, "cta", None)
    ignore = brand_words(getattr(hyg, "brand_name", None))
    text, log = normalise(text)
    text, more = fix_cta(text, cta)
    log += more
    text, more = dedupe(text, ignore, cta)
    log += more
    text, more = fix_fragments(text, cta)
    log += more
    text, more = normalise(text)
    log += [m for m in more if m not in log]
    residue = find_issues(text, hyg)["repeat"] if hyg is not None else integrity_findings(text, cta)
    return text, log, residue


__all__ = ["cta_findings", "cta_occurrences", "final_pass", "fix_cta", "fix_fragments", "fragments",
           "integrity_findings", "normalise"]
