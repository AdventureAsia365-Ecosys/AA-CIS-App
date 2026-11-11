"""AA-1081 — the T7 minimum seed for Blog (the AA-1077 memo, option C), behind `T7_BLOG_MIN_SEED_WORDS`.

The AA-741 phase 3 audit (`o1 PIECE SEED`, root `docs/audits/2026-11-10-S276-aa741-phase3/` §9.3): a Blog piece's
length follows its seed (r = 0.424), and a seed of <= 991 words gave 9 F4 (too short) holds in 12 pieces. The AA-1077
memo recommends that a Subject whose seed cannot carry a ~900-word article is not proposed on Blog (it stays on the
shorter Channels). This module holds the setting and the estimate; `slate._fetch_short_blog_seeds()` applies it.

**Setting**: env `T7_BLOG_MIN_SEED_WORDS` (an integer number of words), read per call. Unset, empty, 0, negative or
not a number = off (the default): the Slate behaves exactly as before. The tech lead's decision only sets the number.

**Blog seed words** (the audit's measure: the writer's seed words + its route moment words) estimated at T7 from what
the pick would hand T9: the seed atom's `atom_seed_text()` (label, evidence, hook, season, siblings, place card) plus
`route_moment_seeds()` over the walk the pick resolves (`slate._segment_walk()`), with the moments A3 excluded from
the walk dropped as T9's `_fetch_route_segments()` does. DB reads only, no LLM.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from services.acp_shared.atom_seed import atom_seed_text, route_moment_seeds

MIN_SEED_ENV = "T7_BLOG_MIN_SEED_WORDS"

# The audit script's word regex (p3_1_holds_deep.py WORD_RE), so a number set from the memo means the same thing here.
WORD_RE = re.compile(r"[A-Za-zÀ-ɏ0-9]+(?:['’-][A-Za-zÀ-ɏ0-9]+)*")


def min_seed_words() -> int:
    """The Blog minimum seed in words; 0 = off (unset, empty, 0, negative or not an integer)."""
    raw = (os.environ.get(MIN_SEED_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


def seed_words(text: Optional[str]) -> int:
    return len(WORD_RE.findall(text or ""))


# One live, non-alias member atom per Segment on the walk's tour (first by atom_id, as T9's _ROUTE_SEGMENT_TEXT_QUERY),
# with A3's exclusion (every current ranking row of the (segment, tour) excluded) and the walk order fields.
_WALK_ATOMS_SQL = """
    SELECT DISTINCT ON (m.segment_id) m.segment_id, m.atom_id, ta.text, ta.itinerary_day, ta.created_at,
           (SELECT bool_and(ar.excluded_reason IS NOT NULL) FROM acp_contract.atom_ranking ar
            WHERE ar.segment_id = m.segment_id AND ar.tour_id = ta.tour_id
              AND ar.superseded_at IS NULL) AS excluded
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
    WHERE m.segment_id = ANY($1::text[]) AND ta.tour_id = $2::uuid
      AND ta.owner_scope IN ('platform', $3::text)
      AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
    ORDER BY m.segment_id, m.atom_id
"""


def walk_atoms(rows: list, segment_ids: list[str]) -> list[tuple[str, str]]:
    """Pure: the walk's (atom_id, label) in day order (no day last, then extraction order, then walk order), with the
    excluded moments dropped unless that leaves nothing (T9's rule)."""
    order = {s: i for i, s in enumerate(segment_ids)}
    rows = [dict(r) for r in rows]
    kept = [r for r in rows if not r.get("excluded")] or rows

    def key(r: dict) -> tuple:
        created = r.get("created_at")
        ts = created.timestamp() if hasattr(created, "timestamp") else float("inf")
        day = r.get("itinerary_day")
        return (day is None, day or 0, ts, order.get(r["segment_id"], len(order)))

    return [(r["atom_id"], r["text"]) for r in sorted(kept, key=key)]


async def blog_seed_words(conn, tenant_id, atom_id: str, tour_id: Optional[str],
                          route_segment_ids: Optional[list[str]]) -> int:
    """Words the T9 writer would get for a Blog pick seeded on `atom_id`: its seed text, plus the walk's moment seeds
    when the pick carries a walk (`route_segment_ids` on `tour_id`). 0 when the atom is not live for the tenant."""
    words = seed_words(await atom_seed_text(conn, atom_id, str(tenant_id)))
    if route_segment_ids and tour_id:
        rows = await conn.fetch(_WALK_ATOMS_SQL, list(route_segment_ids), str(tour_id), str(tenant_id))
        atoms = walk_atoms(rows, list(route_segment_ids))
        if atoms:
            words += sum(seed_words(block) for _aid, block in await route_moment_seeds(conn, atoms, str(tenant_id)))
    return words


__all__ = ["MIN_SEED_ENV", "WORD_RE", "blog_seed_words", "min_seed_words", "seed_words", "walk_atoms"]
