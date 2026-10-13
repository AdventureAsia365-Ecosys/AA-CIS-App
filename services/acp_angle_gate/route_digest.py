"""AA-771 / AA-768 (S234, plan C7) — what a Route angle may anchor on.

S233: a Route/Blog request carried ONE atom's seed into T8 — `route_segment_ids[0]`, which on
the Mongolia route was the Day-14 monastery ruins (1 source fact). Every blog angle led with the
ruins ('Lead with the ruins themselves as a physical fact on Day 14') and the writer invented
their state in 6/8 blogs. Every other moment had 8–11 facts.

For a Route request T8 now writes from a compact digest of the whole walk, built from the same
day-ordered, merged moment blocks T9 writes from (`content_writing.service._fetch_route_segments`
— no new LLM call): one line per moment with its day, label, fact count and top facts, then
ANCHOR CANDIDATES (≥ `THIN_FACTS` facts) and THIN MOMENTS. `angle_anchor()` finds which moment an
angle leads with (its name plus the brief's lead/open sentence) so a thin anchor can be re-ranked
below the other angles, and the angle call retried only when all 3 are thin.

Pure: parses the seed blocks atom_seed.compose_seed / compose_route_moment produce.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_shared.atom_seed import THIN_FACTS, moment_fact_count
from services.acp_shared.channel_spec import fold

_DAY_RE = re.compile(r"\bDay (\d+) of the trip\b")
_LOCATION_RE = re.compile(r"^Location: ([^,(]+)", re.M)
_SOURCE_RE = re.compile(r"Source itinerary: (.*)$", re.M)
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")
_TOKEN_RE = re.compile(r"[a-z]+")
_FACT_CHARS = 180
TOP_FACTS = 2
# Words that never tell two moments apart (actions and trip words, not places).
_STOP = frozenset("""the and with from into over this that their there about after before during visit visits visiting
hike hiking walk walking drive driving trek trekking tour tours trip trips stop stops lunch dinner breakfast overnight
stay stays explore exploring discover day days evening morning afternoon free time along around through near
place places site sites area region local""".split())


def _stem(word: str) -> str:
    for suf in ("ings", "ing", "ers", "ies", "ed", "es", "er", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(fold(text)) if len(t) >= 4 and t not in _STOP]


def _facts(block: str) -> list[str]:
    bullets, in_bg = [], False
    for line in block.splitlines():
        if line.startswith("Place background"):
            in_bg = True
            continue
        if in_bg and line.startswith("- "):
            bullets.append(line[2:].strip())
        else:
            in_bg = False
    source = [s.strip() for m in _SOURCE_RE.finditer(block) for s in _SENT_RE.split(m.group(1).strip()) if s.strip()]
    return bullets + source


def route_moments(segments: list[tuple[str, str]]) -> list[dict]:
    """[(atom_id, seed block)] (day order) → [{atom_id, label, place, day, facts, fact_count, thin,
    tokens}]. `tokens` are the moment's own name words that no other moment shares."""
    moments = []
    for atom_id, block in segments:
        label = (block or "").strip().split("\n", 1)[0].strip()
        loc = _LOCATION_RE.search(block or "")
        day = _DAY_RE.search(block or "")
        n = moment_fact_count(block or "")
        moments.append({
            "atom_id": atom_id, "label": label, "place": loc.group(1).strip() if loc else None,
            "day": int(day.group(1)) if day else None, "facts": _facts(block or ""), "fact_count": n,
            "thin": n < THIN_FACTS, "tokens": set(_tokens(f"{label} {loc.group(1) if loc else ''}")),
        })
    for m in moments:
        others = set().union(*[o["tokens"] for o in moments if o is not m]) if len(moments) > 1 else set()
        m["tokens"] = m["tokens"] - others
    return moments


def _name(m: dict) -> str:
    return m["place"] if m.get("place") and fold(m["place"]) not in fold(m["label"]) else m["label"]


def render_digest(moments: list[dict]) -> str:
    """The T8 CONTENT SEED for a Route request."""
    lines = ["ROUTE (this piece covers these moments of the trip, in day order):"]
    for m in moments:
        day = f"Day {m['day']}" if m.get("day") else "Day ?"
        facts = " | ".join(f[:_FACT_CHARS] for f in m["facts"][:TOP_FACTS])
        n = m["fact_count"]
        lines.append(f"- {day} · {_name(m)} · {n} fact{'s' if n != 1 else ''}" + (f" · {facts}" if facts else ""))
    anchors = [_name(m) for m in moments if not m["thin"]]
    thin = [_name(m) for m in moments if m["thin"]]
    if anchors:
        lines.append("ANCHOR CANDIDATES (3 or more source facts — every angle leads with one of these): "
                     + "; ".join(anchors))
    if thin:
        lines.append("THIN MOMENTS (almost no source facts — mention in one sentence at most; never the hook, the "
                     "title or the climax): " + "; ".join(thin))
    return "\n".join(lines)


_LEAD_RE = re.compile(r"\b(?:open|opening|lead|leads|start|begin|hook|anchor|centre|center)\w*\b", re.I)
# A lead verb and the moment right after it: 'lead with X', 'open on X', 'anchor the piece in X'.
_LEAD_VERB_RE = re.compile(r"\b(?:open|lead|start|begin|hook|anchor|centre|center)(?:s|ed|ing)?\s+"
                           r"(?:[\w'’-]+\s+){0,3}?(?:with|on|in|around|by)\b", re.I)
_DAY_MENTION_RE = re.compile(r"\bDay\s+(\d+)\b", re.I)
_SENTS_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|:|,|\s[—–-]\s|\bbut\b|\bthen\b)\s*", re.I)
# A clause that keeps a moment OUT of the lead (S234 review): the T8 prompt itself asks the angle to
# say this about a thin moment, so its mention must never count as the anchor.
_SUPPRESS_RE = re.compile(
    r"\b(?:not|never|nor|no|avoid\w*|don'?t|do not|rather than|instead of|without|skip\w*|downplay\w*)\b"
    r"|\b(?:one|a single|a short|a brief|a passing) (?:sentence|line|mention|aside)\b|\bat most\b|\bbriefly\b"
    r"|\bin passing\b|\bonly (?:mention|touch|note)\w*\b|\bfootnote\b", re.I)


def _lead_clauses(angle: dict) -> str:
    """The brief's lead sentence(s) with every clause that keeps a moment out of the lead removed
    ('not the ruins', 'Avoid anchoring on the ruins', 'Keep the ruins to one sentence at most')."""
    style = str(angle.get("best_final_style") or "")
    sents = []
    for s in (x for x in _SENTS_RE.split(style) if x.strip()):
        kept = [c for c in _CLAUSE_SPLIT_RE.split(s) if c and c.strip() and not _SUPPRESS_RE.search(c)]
        sents.append(", ".join(kept))
    lead = [s for s in sents if s and _LEAD_RE.search(s)]
    return " ".join(lead or [s for s in sents if s][:1])


def _mentions(text: str, moments: list[dict]) -> list[tuple[int, dict]]:
    """(token position, moment) for every moment named in `text` by its own name word or 'Day N'."""
    toks = _tokens(text)
    out: list[tuple[int, dict]] = []
    for m in moments:
        pos = next((i for i, t in enumerate(toks) if t in m["tokens"]), None)
        if pos is not None:
            out.append((pos, m))
        if m.get("day") is not None:
            for dm in _DAY_MENTION_RE.finditer(text):
                if int(dm.group(1)) == m["day"]:
                    out.append((len(_tokens(text[:dm.start()])), m))
    return sorted(out, key=lambda x: x[0])


def angle_anchor(angle: dict, moments: list[dict]) -> Optional[dict]:
    """The moment the angle leads with: in the angle name, the earliest moment named; otherwise in
    the brief's lead sentence(s), the first moment named after a lead verb ('lead/open/start/hook
    with|on X'), else the earliest one named there. Clauses that keep a moment out of the lead
    (negated, 'one sentence at most', 'briefly') are ignored. None when no moment is named."""
    if not moments:
        return None
    named = _mentions(str(angle.get("name") or ""), moments)
    if named:
        return named[0][1]
    lead = _lead_clauses(angle)
    found = _mentions(lead, moments)
    if not found:
        return None
    verb = _LEAD_VERB_RE.search(lead)
    if verb:
        after = len(_tokens(lead[:verb.end()]))
        nxt = next((m for pos, m in found if pos >= after), None)
        if nxt is not None:
            return nxt
    return found[0][1]


def thin_anchor(angle: dict, moments: list[dict]) -> Optional[str]:
    """'<moment> (<n> fact(s))' when the angle leads with a thin moment, else None."""
    m = angle_anchor(angle, moments)
    if m is None or not m["thin"]:
        return None
    return f"{_name(m)} ({m['fact_count']} fact{'s' if m['fact_count'] != 1 else ''})"


__all__ = ["angle_anchor", "render_digest", "route_moments", "thin_anchor", "TOP_FACTS"]
