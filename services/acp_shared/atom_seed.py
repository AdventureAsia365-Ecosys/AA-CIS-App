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
"""
from __future__ import annotations

import json
import re
from typing import Optional

MAX_SIBLINGS = 4
MAX_EVIDENCE_CHARS = 500

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
    SELECT pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note, pc.facts
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


def compose_seed(text: str, evidence: Optional[str], hook: Optional[str], season: Optional[str],
                 sibling_evidence: list[str], *, place: Optional[str] = None,
                 country: Optional[str] = None, day: Optional[int] = None,
                 card: Optional[dict] = None) -> str:
    """Pure: the seed block from an atom's fields. Empty fields are left out; sibling evidence is
    de-duplicated against this atom's own and capped at MAX_SIBLINGS. `card` (AA-772) is the
    atom's place card: its location replaces the bare place + country line, and its checked
    facts become the PLACE BACKGROUND the writer may cite."""
    lines = [text.strip()]
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
    if day:
        lines.append(f"Day {day} of the trip")
    if evidence and evidence.strip():
        lines.append(f"Source itinerary: {evidence.strip()}")
    if hook and hook.strip():
        lines.append(f"Why it matters: {hook.strip()}")
    if season and season.strip():
        lines.append(f"Season: {season.strip()}")
    facts = (card or {}).get("facts") or []
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    fact_lines = [f["text"] for f in facts if isinstance(f, dict) and f.get("text")]
    if fact_lines:
        lines.append("Place background (checked facts about the place itself — you may use these; "
                     "they say nothing about what this trip does):\n" + "\n".join(f"- {f}" for f in fact_lines))
    seen = {(evidence or "").strip().lower()}
    others = []
    for ev in sibling_evidence:
        key = (ev or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            others.append(ev.strip())
        if len(others) >= MAX_SIBLINGS:
            break
    if others:
        lines.append("Other trips that include this same moment (for context only — never promise "
                     "these details for this trip):\n" + "\n".join(f"- {o}" for o in others))
    return "\n".join(lines)


async def atom_seed_text(conn, atom_id: str, tenant_id: str) -> Optional[str]:
    """None when the atom is not live/visible for this tenant (callers raise their own error)."""
    row = await conn.fetchrow(_ATOM_QUERY, atom_id, str(tenant_id))
    if row is None:
        return None
    rows = await conn.fetch(_SIBLING_EVIDENCE_QUERY, atom_id, str(tenant_id))
    siblings = [trim_evidence(r["evidence"], row["place"]) for r in sorted(rows, key=lambda r: (r["n"], r["evidence"]))]
    card = await conn.fetchrow(_CARD_QUERY, atom_id)
    return compose_seed(row["text"], trim_evidence(row["evidence"], row["place"]), row["emotional_hook"],
                        row["season_note"], [s for s in siblings if s], place=row["place"],
                        country=row["country"], day=row["itinerary_day"],
                        card=dict(card) if card else None)
