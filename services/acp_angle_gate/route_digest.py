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
_DAY_MENTION_RE = re.compile(r"\bDay\s+(\d+)\b", re.I)
_SENTS_RE = re.compile(r"(?<=[.!?])\s+")


def _lead_text(angle: dict) -> str:
    style = str(angle.get("best_final_style") or "")
    sents = [s for s in _SENTS_RE.split(style) if s.strip()]
    lead = [s for s in sents if _LEAD_RE.search(s)]
    return " ".join(lead or sents[:1])


def angle_anchor(angle: dict, moments: list[dict]) -> Optional[dict]:
    """The moment the angle leads with: the earliest mention, in the angle name then the brief's
    lead/open sentence, of a moment's own name word or its 'Day N'. None when no moment is named."""
    if not moments:
        return None
    best: tuple[int, int] | None = None
    found = None
    for part_rank, text in enumerate((str(angle.get("name") or ""), _lead_text(angle))):
        toks = _tokens(text)
        days = [(m_.start(), int(m_.group(1))) for m_ in _DAY_MENTION_RE.finditer(text)]
        for m in moments:
            pos = next((i for i, t in enumerate(toks) if t in m["tokens"]), None)
            if pos is not None and (best is None or (part_rank, pos) < best):
                best, found = (part_rank, pos), m
            if m.get("day") is not None:
                for char_pos, d in days:
                    tok_pos = len(_tokens(text[:char_pos]))
                    if d == m["day"] and (best is None or (part_rank, tok_pos) < best):
                        best, found = (part_rank, tok_pos), m
        if found is not None:
            return found
    return found


def thin_anchor(angle: dict, moments: list[dict]) -> Optional[str]:
    """'<moment> (<n> fact(s))' when the angle leads with a thin moment, else None."""
    m = angle_anchor(angle, moments)
    if m is None or not m["thin"]:
        return None
    return f"{_name(m)} ({m['fact_count']} fact{'s' if m['fact_count'] != 1 else ''})"


__all__ = ["angle_anchor", "render_digest", "route_moments", "thin_anchor", "TOP_FACTS"]
