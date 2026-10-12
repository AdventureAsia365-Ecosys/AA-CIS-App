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

from typing import Optional

MAX_SIBLINGS = 4

_ATOM_QUERY = """
    SELECT text, evidence, emotional_hook, season_note
    FROM acp_contract.tour_atoms
    WHERE atom_id = $1 AND owner_scope IN ('platform', $2) AND NOT deleted AND NOT is_empty_marker
"""

# Other live atoms of the same Segment (any tour), same visibility rule as the atom itself.
_SIBLING_EVIDENCE_QUERY = """
    SELECT DISTINCT ta.evidence
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.atom_segment_member sib ON sib.segment_id = m.segment_id AND sib.atom_id <> m.atom_id
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = sib.atom_id
    WHERE m.atom_id = $1 AND ta.owner_scope IN ('platform', $2)
      AND NOT ta.deleted AND NOT ta.is_empty_marker
      AND ta.evidence IS NOT NULL AND btrim(ta.evidence) <> ''
    LIMIT 20
"""


def compose_seed(text: str, evidence: Optional[str], hook: Optional[str], season: Optional[str],
                 sibling_evidence: list[str]) -> str:
    """Pure: the seed block from an atom's fields. Empty fields are left out; sibling evidence is
    de-duplicated against this atom's own and capped at MAX_SIBLINGS."""
    lines = [text.strip()]
    if evidence and evidence.strip():
        lines.append(f"Source itinerary: {evidence.strip()}")
    if hook and hook.strip():
        lines.append(f"Why it matters: {hook.strip()}")
    if season and season.strip():
        lines.append(f"Season: {season.strip()}")
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
        lines.append("Other itineraries with the same moment:\n" + "\n".join(f"- {o}" for o in others))
    return "\n".join(lines)


async def atom_seed_text(conn, atom_id: str, tenant_id: str) -> Optional[str]:
    """None when the atom is not live/visible for this tenant (callers raise their own error)."""
    row = await conn.fetchrow(_ATOM_QUERY, atom_id, str(tenant_id))
    if row is None:
        return None
    siblings = [r["evidence"] for r in await conn.fetch(_SIBLING_EVIDENCE_QUERY, atom_id, str(tenant_id))]
    siblings.sort()  # deterministic choice of which siblings make the cap
    return compose_seed(row["text"], row["evidence"], row["emotional_hook"], row["season_note"], siblings)
