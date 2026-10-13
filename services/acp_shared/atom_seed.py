"""AA-768 — the source text T8/T9 write from for one atom.

T8/T9 used `tour_atoms.text` alone, which is the atom's short label ("Pawangka Monastery —
visit", 26 characters). A 200-word post written from that is almost all invention: in the S231
pilot the LinkedIn pieces placed a Tibetan monastery "above Luang Prabang" and the F9 fact check
held every one. The atom already carries its source sentence (`evidence`, quoted from the tour
itinerary), an `emotional_hook` and a `season_note`, and its Segment groups the same place+action
across other tours — real, citable source the writer never saw.

`atom_seed_text()` = the label + this atom's evidence/hook/season + up to `MAX_SIBLINGS` distinct
evidence sentences from other live atoms of the same Segment (platform or this tenant's own).
The same text feeds the writer prompt and the T10 gates, so what the writer may cite is exactly
what the gates check against.

S234 (AA-768) — the sibling lines were the source of borrowed places and facts (S233 r1: 'Erdene
Zuu' / 'thousands of monks' in 5/8 Mongolia blogs). Siblings are now added only when the place
card has fewer than `RICH_CARD_FACTS` facts, at most `MAX_SIBLINGS` (2), and never a line naming a
place the moment's own lines do not name. Route moments get no siblings at all
(`route_moment_seeds`), are merged by place card (one block per place, C10), and a moment with
fewer than `THIN_FACTS` facts is tagged thin so it is not made the hook.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from services.acp_content_writing.hygiene import fold, phrase_tokens, proper_nouns

MAX_SIBLINGS = 2
MAX_EVIDENCE_CHARS = 500
RICH_CARD_FACTS = 3  # a card with this many checked facts needs no "other trips" lines
THIN_FACTS = 3  # a moment with fewer facts (card facts + source sentences) is thin

# AA-768 round 5 — + the atom's place and its source tour's country.
# S233 — reads acp_contract.v_active_tour_atoms (a retracted/trashed Master tour never feeds a
# tenant post) and carries itinerary_day ("Day N of the trip").
_ATOM_QUERY = """
    SELECT ta.text, ta.evidence, ta.emotional_hook, ta.season_note, ta.place, ta.itinerary_day,
           rt.country
    FROM acp_contract.v_active_tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.atom_id = $1 AND ta.owner_scope IN ('platform', $2) AND NOT ta.deleted AND NOT ta.is_empty_marker
"""

# Other live atoms of the same Segment, same visibility rule as the atom itself. S233: only from
# tours in the same country (pre-S218 Segments mixed countries — Ladakh/Mongolia evidence reached
# a Tibet seed), never alias members, never another day of the same tour, active tours only;
# shortest first so a whole-day-body fallback evidence does not crowd out real sentences.
_SIBLING_EVIDENCE_QUERY = """
    SELECT DISTINCT ON (ta.evidence) ta.evidence, length(ta.evidence) AS n
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms me ON me.atom_id = m.atom_id
    JOIN silver_aa_internal.raw_tours mrt ON mrt.tour_id = me.tour_id
    JOIN acp_contract.atom_segment_member sib
         ON sib.segment_id = m.segment_id AND sib.atom_id <> m.atom_id AND NOT sib.is_alias
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = sib.atom_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE m.atom_id = $1 AND ta.owner_scope IN ('platform', $2)
      AND NOT ta.deleted AND NOT ta.is_empty_marker AND ta.tour_id <> me.tour_id
      AND rt.country IS NOT DISTINCT FROM mrt.country
      AND ta.evidence IS NOT NULL AND btrim(ta.evidence) <> ''
    LIMIT 40
"""

_CARD_QUERY = """
    SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts
    FROM acp_shared.atom_place_card apc
    JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id
    WHERE apc.atom_id = $1 AND pc.status <> 'rejected'
"""

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def trim_evidence(evidence: Optional[str], place: Optional[str]) -> Optional[str]:
    """A3 stores the WHOLE day body as `evidence` when the model's quote fails the containment
    check (atom_extraction.checkable_evidence). Keep the sentences that name the place (plus the
    next one), else the first sentences, up to MAX_EVIDENCE_CHARS."""
    if not evidence or len(evidence) <= MAX_EVIDENCE_CHARS:
        return evidence
    sents = [x.strip() for x in _SENT_RE.split(" ".join(evidence.split())) if x.strip()]
    words = [w for w in re.findall(r"[a-z]{4,}", (place or "").lower())]
    picked: list[int] = []
    for i, sent in enumerate(sents):
        if words and any(w in sent.lower() for w in words):
            picked += [i, i + 1]
    order = sorted({i for i in picked if i < len(sents)}) or list(range(len(sents)))
    out = ""
    for i in order:
        if len(out) + len(sents[i]) + 1 > MAX_EVIDENCE_CHARS:
            break
        out = f"{out} {sents[i]}".strip()
    return out or sents[0][:MAX_EVIDENCE_CHARS]


def _card_facts(card: Optional[dict]) -> list[str]:
    facts = (card or {}).get("facts") or []
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return [f["text"] for f in facts if isinstance(f, dict) and f.get("text")]


def _location_lines(place: Optional[str], country: Optional[str], card: Optional[dict]) -> list[str]:
    lines: list[str] = []
    if card and card.get("display_name"):
        where = ", ".join(p for p in (card["display_name"], card.get("locality"), card.get("region"),
                                       card.get("country")) if p)
        if place and place.strip() and place.strip().lower() != card["display_name"].lower():
            where += f" (the itinerary calls it {place.strip()})"
        lines.append(f"Location: {where} (do not place it in any other town, region or country)")
        if card.get("naming_note"):
            lines.append(f"Naming: {card['naming_note']}")
    elif country and country.strip():
        where = f"{place.strip()}, {country.strip()}" if place and place.strip() else country.strip()
        lines.append(f"Location: {where} (do not name any other country or region for it)")
    return lines


def _background_block(fact_lines: list[str]) -> str:
    return ("Place background (checked facts about the place itself — you may use these; "
            "they say nothing about what this trip does):\n" + "\n".join(f"- {f}" for f in fact_lines))


def foreign_places(line: str, own_text: str) -> set[str]:
    """Proper nouns in `line` with a word the moment's own text never uses ('Erdene Zuu' in a
    sibling line of an unnamed-ruins moment)."""
    own = set()
    for p in proper_nouns(own_text):
        own |= set(phrase_tokens(p))
    own |= set(re.findall(r"[a-z0-9]+", fold(own_text)))
    return {p for p in proper_nouns(line) if any(t not in own for t in phrase_tokens(p))}


def pick_siblings(sibling_evidence: list[str], own_text: str, *, own_evidence: Optional[str],
                  card_fact_count: int) -> list[str]:
    """S234 sibling rule: none when the card already has `RICH_CARD_FACTS` facts; otherwise up to
    `MAX_SIBLINGS` distinct lines that name no place the moment's own text does not name."""
    if card_fact_count >= RICH_CARD_FACTS:
        return []
    seen = {(own_evidence or "").strip().lower()}
    others: list[str] = []
    for ev in sibling_evidence:
        key = (ev or "").strip().lower()
        if not key or key in seen or foreign_places(ev, own_text):
            continue
        seen.add(key)
        others.append(ev.strip())
        if len(others) >= MAX_SIBLINGS:
            break
    return others


def compose_seed(text: str, evidence: Optional[str], hook: Optional[str], season: Optional[str],
                 sibling_evidence: list[str], *, place: Optional[str] = None,
                 country: Optional[str] = None, day: Optional[int] = None,
                 card: Optional[dict] = None) -> str:
    """Pure: the seed block from an atom's fields. Empty fields are left out; sibling evidence
    follows `pick_siblings` (S234). `card` (AA-772) is the atom's place card: its location
    replaces the bare place + country line, and its checked facts become the PLACE BACKGROUND the
    writer may cite."""
    lines = [text.strip()] + _location_lines(place, country, card)
    if day:
        lines.append(f"Day {day} of the trip")
    if evidence and evidence.strip():
        lines.append(f"Source itinerary: {evidence.strip()}")
    if hook and hook.strip():
        lines.append(f"Why it matters: {hook.strip()}")
    if season and season.strip():
        lines.append(f"Season: {season.strip()}")
    fact_lines = _card_facts(card)
    if fact_lines:
        lines.append(_background_block(fact_lines))
    others = pick_siblings(sibling_evidence, "\n".join(lines), own_evidence=evidence,
                           card_fact_count=len(fact_lines))
    if others:
        lines.append("Other trips that include this same moment (for context only — never promise "
                     "these details for this trip):\n" + "\n".join(f"- {o}" for o in others))
    return "\n".join(lines)


_SOURCE_RE = re.compile(r"Source itinerary: (.*)$", re.M)
_SENTS_RE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def moment_fact_count(seed_block: str) -> int:
    """Facts a moment block gives the writer: its "Place background" bullets plus the sentences
    of its "Source itinerary" text ("Why it matters" is framing, not a fact). S233: the Mongolia
    ruins moment had 1; every other moment 8–11."""
    n = sum(len([x for x in _SENTS_RE.split(m.group(1).strip()) if x.strip()])
            for m in _SOURCE_RE.finditer(seed_block or ""))
    in_bg = False
    for line in (seed_block or "").splitlines():
        if line.startswith("Place background"):
            in_bg = True
            continue
        if in_bg and line.startswith("- "):
            n += 1
        else:
            in_bg = False
    return n


def thin_note(fact_count: int) -> str:
    return (f"Thin moment: only {fact_count} source fact{'s' if fact_count != 1 else ''} — mention it in "
            "one sentence at most; never make it the hook, the title or the climax.")


def merge_key(parts: dict) -> Optional[str]:
    """C10 — route moments about the same place merge: same place card, else the same folded
    card display name + country. A moment with no card never merges."""
    card = parts.get("card") or {}
    if card.get("card_id"):
        return f"card:{card['card_id']}"
    if card.get("display_name"):
        return f"name:{fold(card['display_name'])}|{fold(card.get('country') or parts.get('country'))}"
    return None


def compose_route_moment(group: list[dict]) -> str:
    """One seed block for one or more route moments at the same place (day order): the first
    moment's label and location, one "Day N: <label> — Source itinerary: …" line per moment, the
    hooks and seasons de-duplicated, and the background of the richest card only (S233: the 5
    Khövsgöl cards disagreed — 2% / 0.4% / 70% of the fresh water). No sibling lines. Tagged thin
    when it has fewer than `THIN_FACTS` facts."""
    first = group[0]
    if len(group) == 1:
        block = compose_seed(first["text"], first.get("evidence"), first.get("hook"), first.get("season"), [],
                             place=first.get("place"), country=first.get("country"), day=first.get("day"),
                             card=first.get("card"))
    else:
        lines = [first["text"].strip()] + _location_lines(first.get("place"), first.get("country"), first.get("card"))
        for part in group:
            day = f"Day {part['day']}" if part.get("day") else "Another day"
            ev = (part.get("evidence") or "").strip()
            lines.append(f"{day} of the trip: {part['text'].strip()}" + (f" — Source itinerary: {ev}" if ev else ""))
        for key, label in (("hook", "Why it matters"), ("season", "Season")):
            for value in dict.fromkeys((p.get(key) or "").strip() for p in group):
                if value:
                    lines.append(f"{label}: {value}")
        richest = max((p.get("card") for p in group), key=lambda c: len(_card_facts(c)))
        if _card_facts(richest):
            lines.append(_background_block(_card_facts(richest)))
        block = "\n".join(lines)
    n = moment_fact_count(block)
    return block + ("\n" + thin_note(n) if n < THIN_FACTS else "")


async def atom_seed_parts(conn, atom_id: str, tenant_id: str, *, siblings: bool = True) -> Optional[dict]:
    """The atom's seed fields (None when not live/visible for this tenant). The sibling query
    runs only when `siblings` is set and the place card is not already rich."""
    row = await conn.fetchrow(_ATOM_QUERY, atom_id, str(tenant_id))
    if row is None:
        return None
    card = await conn.fetchrow(_CARD_QUERY, atom_id)
    card = dict(card) if card else None
    sibling_lines: list[str] = []
    if siblings and len(_card_facts(card)) < RICH_CARD_FACTS:
        rows = await conn.fetch(_SIBLING_EVIDENCE_QUERY, atom_id, str(tenant_id))
        sibling_lines = [s for s in (trim_evidence(r["evidence"], row["place"])
                                     for r in sorted(rows, key=lambda r: (r["n"], r["evidence"]))) if s]
    return {"atom_id": atom_id, "text": row["text"], "evidence": trim_evidence(row["evidence"], row["place"]),
            "hook": row["emotional_hook"], "season": row["season_note"], "place": row["place"],
            "country": row["country"], "day": row["itinerary_day"], "card": card, "siblings": sibling_lines}


async def atom_seed_text(conn, atom_id: str, tenant_id: str) -> Optional[str]:
    """None when the atom is not live/visible for this tenant (callers raise their own error)."""
    p = await atom_seed_parts(conn, atom_id, tenant_id)
    if p is None:
        return None
    return compose_seed(p["text"], p["evidence"], p["hook"], p["season"], p["siblings"], place=p["place"],
                        country=p["country"], day=p["day"], card=p["card"])


async def route_moment_seeds(conn, atoms: list, tenant_id: str) -> list[tuple[str, str]]:
    """S234 — a route's moments as [(first atom_id, seed block)], in the given (day) order, with
    same-place moments merged into one block (C10) and no sibling lines. `atoms` is
    [(atom_id, label)]; an atom whose seed cannot be read keeps its own block with the label
    alone (the pre-S234 fallback)."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for atom_id, label in atoms:
        p = await atom_seed_parts(conn, atom_id, tenant_id, siblings=False)
        if p is None:
            p = {"atom_id": atom_id, "text": label or "", "card": None, "label_only": True}
        key = merge_key(p) or f"atom:{atom_id}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    out = []
    for k in order:
        group = groups[k]
        block = group[0]["text"] if group[0].get("label_only") else compose_route_moment(group)
        out.append((group[0]["atom_id"], block))
    return out
