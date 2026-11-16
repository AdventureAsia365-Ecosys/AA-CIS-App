"""services/acp_contract/research_carry.py — AA-1122 item 4: a renamed or merged place keeps its demand research.

segment_matching.run_segment_matching can change the canonical place of a Segment (a new tour's atom moves the label,
"Hội An" -> "Hoi An") or merge Segments (an alias old id -> live id). Research is logged per canonical place
(acp_contract.segment_research_log) and its keyword map is stored per place (acp_contract.place_keyword), so the new
name would look never researched and be bought again. :func:`after_matching` runs inside segment matching's write
transaction and copies the research to the new name when the change is a proven same entity and the two names are the
same plain keyword (:func:`research_renames`). $0: no paid call.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import structlog

logger = structlog.get_logger()

# The log rows keep the newest researched_at per (market, tier); the map is copied only when the new name has none
# (never mixed into another map).
_CARRY_LOG_SQL = """
    INSERT INTO acp_contract.segment_research_log (canonical_place, market, tier, researched_at)
    SELECT $2, market, tier, researched_at FROM acp_contract.segment_research_log WHERE canonical_place = $1
    ON CONFLICT (canonical_place, market, tier) DO UPDATE
        SET researched_at = GREATEST(acp_contract.segment_research_log.researched_at, excluded.researched_at)
"""
_CARRY_MAP_SQL = """
    INSERT INTO acp_contract.place_keyword (subject_kind, subject, keyword, rank, country, activity, source,
                                            prompt_version, jev_zone, jev_mode, jev_probability, proposed_at)
    SELECT subject_kind, $2, keyword, rank, country, activity, source, prompt_version, jev_zone, jev_mode,
           jev_probability, proposed_at
      FROM acp_contract.place_keyword
     WHERE subject_kind = 'place' AND subject = $1
       AND NOT EXISTS (SELECT 1 FROM acp_contract.place_keyword k WHERE k.subject_kind = 'place' AND k.subject = $2)
    ON CONFLICT (subject_kind, subject, keyword) DO NOTHING
"""


def research_renames(existing_labels: Mapping[str, str], segment_rows: Mapping[str, tuple],
                     alias_rows: Iterable[tuple[str, str]], relabelled: Iterable[str]) -> list[tuple[str, str]]:
    """(old place, new place) pairs whose research should follow the new name. Pure.

    Only proven same-entity changes (critic of the S282 design: an unclaimed upsert can relabel a Segment to a
    DIFFERENT place, AA-857): a Segment this run may relabel (`relabelled`: it claimed it, or it has no member) whose
    stored place changes, and an alias merge (old id -> live id). And only when both names are the same plain keyword
    (segment_research_batch.clean_keyword: case, accents, punctuation): the copied map's keywords then still name the
    place, and the copied log keeps it fresh honestly. A real rename ("Tiger's Nest" -> "Taktsang Monastery") carries
    nothing: the new name is researched as a new place (its old keywords' volumes come free from the store; only the
    keyword proposal and new keywords are paid), because a fresh log over keywords that no longer name the place would
    hide its demand for a whole window."""
    from services.acp_contract.segment_research_batch import clean_keyword

    pairs: list[tuple[str, str]] = []
    for sid in relabelled:
        old, row = existing_labels.get(sid), segment_rows.get(sid)
        if old and row and row[1] and row[1] != old:
            pairs.append((old, row[1]))
    for was, target in alias_rows:
        old = existing_labels.get(was)
        new = segment_rows[target][1] if target in segment_rows else existing_labels.get(target)
        if old and new and old != new:
            pairs.append((old, new))
    out = []
    for old, new in dict.fromkeys(pairs):
        plain = clean_keyword(old)
        if plain and plain == clean_keyword(new):
            out.append((old, new))
    return out


async def carry_research(conn, renames: list[tuple[str, str]]) -> int:
    """Copy each old place's research log rows and (when the new name has none) its keyword map to the new name, on
    `conn` (the caller's transaction). Returns the pairs carried."""
    for old, new in renames:
        await conn.execute(_CARRY_LOG_SQL, old, new)
        await conn.execute(_CARRY_MAP_SQL, old, new)
    if renames:
        logger.info("segment_research_carried", pairs=len(renames), sample=renames[:5])
    return len(renames)


async def after_matching(conn, existing_rows, segment_rows: Mapping[str, tuple], alias_rows, claimed,
                         with_members) -> int:
    """segment matching's hook (its write transaction): the labels it read, the rows it wrote, its aliases; a Segment
    is relabelled when the run claimed it or it has no member (the upsert's own rule)."""
    labels = {r["segment_id"]: r["canonical_place"] for r in existing_rows}
    relabelled = {sid for sid in segment_rows if sid in claimed or sid not in with_members}
    return await carry_research(conn, research_renames(labels, segment_rows, alias_rows, relabelled))


__all__ = ["research_renames", "carry_research", "after_matching"]
