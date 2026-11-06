"""AA-741 (S271 review) — the focus of a Blog walk: which moment a Segment-grain Blog piece is about.

Since S271 a Blog Subject is a Segment that carries its Route as the walk (slate._segment_walk): every Bar-clearing
place on one Route is its own Subject, and each pick stores the same `route_segment_ids` (the whole Route). Without a
focus, T8 wrote its angles from the digest of the whole walk (every rich moment an ANCHOR CANDIDATE) and T9 wrote from
the same moments, so sibling Subjects of one Route gave near-identical articles, and a Subject could get an article
about other places. The focus is the Subject's own Segment: T8 leads every angle with it (route_digest FOCUS), T9 gives
it the title, the opening and the most depth (prompts walk_focus), the other moments are the way there and onwards.

One rule for T8 and T9, on the same data (`content_writing.service._fetch_route_segments`, day order, same-place
moments merged): a request whose Subject is a Segment walks its Route only when the Subject's own moment is on the
walk and is not thin (`THIN_FACTS`); otherwise it is a single-place piece (the request atom's seed, as a Segment pick
on any other Channel). A Route-grain Subject (Blog before S271) or a request with no Subject has no focus and keeps the
whole-walk behaviour. The Slate keeps thin Blog Subjects out in the first place (`seed_is_thin`, slate.propose_slate).
"""
from __future__ import annotations

from typing import Optional

from services.acp_contract.segment_matching import resolve_segment_aliases
from services.acp_shared.atom_seed import THIN_FACTS, atom_seed_parts, compose_seed, moment_fact_count

# A Segment Subject's own Segment (NULL for a Route Subject: subject_check). Tenant table: the tenant filter is here,
# and RLS (tenant_isolation) holds it on the tenant pool too.
_SUBJECT_SEGMENT_SQL = """
    SELECT segment_id FROM acp_shared.subject
    WHERE subject_id = $1::uuid AND tenant_id = $2::uuid AND segment_id IS NOT NULL
"""


def is_thin(block: str) -> bool:
    """A moment block gives the writer fewer than `THIN_FACTS` facts (the T8 digest's THIN MOMENTS rule)."""
    return moment_fact_count(block or "") < THIN_FACTS


async def subject_focus_segment(conn, tenant_id, subject_id) -> Optional[str]:
    """The live Segment id of a Segment Subject (an id that merged into another resolves to the live one, the id
    `trip_route_segment_ids` keeps on the walk); None for a Route Subject, a missing Subject or no Subject."""
    if not subject_id:
        return None
    segment_id = await conn.fetchval(_SUBJECT_SEGMENT_SQL, str(subject_id), str(tenant_id))
    if not segment_id:
        return None
    return (await resolve_segment_aliases(conn, [segment_id])).get(segment_id, segment_id)


def usable_focus(segments: list[tuple[str, str]] | None, focus_index: Optional[int]) -> Optional[int]:
    """The focus moment's index when the walk can be written around it (a walk of 2+ moments, the focus on it and not
    thin), else None: the caller then writes a single-place piece."""
    if not segments or len(segments) < 2 or focus_index is None or not 0 <= focus_index < len(segments):
        return None
    return None if is_thin(segments[focus_index][1]) else focus_index


def focus_block_index(groups: list[list[str]], focus_atom_id: Optional[str]) -> Optional[int]:
    """Index of the merged moment block that holds `focus_atom_id` (`groups`: the atom ids of each block, in block
    order, as atom_seed.route_moment_seeds reports them)."""
    if not focus_atom_id:
        return None
    return next((i for i, ids in enumerate(groups) if focus_atom_id in ids), None)


async def seed_is_thin(conn, atom_id: str, tenant_id) -> bool:
    """A single atom's seed gives the writer fewer than `THIN_FACTS` facts (card background + source sentences; the
    sibling lines are not facts, so they are not read). An atom that is not live for the tenant counts as thin."""
    p = await atom_seed_parts(conn, atom_id, str(tenant_id), siblings=False)
    if p is None:
        return True
    return is_thin(compose_seed(p["text"], p["evidence"], p["hook"], p["season"], [], place=p["place"],
                                country=p["country"], day=p["day"], card=p["card"]))


__all__ = ["focus_block_index", "is_thin", "seed_is_thin", "subject_focus_segment", "usable_focus"]
