"""AA-1063 — the label set browser's list of every claim label set version (moved out of review.py in S282).

`list_sets` reads every listed version's rows and corrections with one query each (S282, Sentry AA-CIS-API-1A: it
used to make two queries per version, an N+1). The per-set result is `review.set_summary` plus the version's queue
counts, exactly as before. `review` is imported inside the functions: review.py re-exports `list_sets`, so a
module-level import here would be circular.
"""
from __future__ import annotations

from typing import Iterable

from services.ground_truth import store


async def read_relabels_many_soft(conn, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """review.read_relabels_soft of several (set_id, set_version) in one query; each version's corrections keep their
    order (created_at, relabel_id). Before CIS 394 every version maps to []."""
    import asyncpg

    from services.ground_truth import review as rv

    keys = sorted(set(keys))
    out: dict[tuple[str, str], list[dict]] = {k: [] for k in keys}
    if not keys:
        return out
    a, b = (list(x) for x in zip(*keys))
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                f"{rv._RELABELS_SQL} WHERE (set_id, set_version) IN (SELECT * FROM unnest($1::text[], $2::text[])) "
                "ORDER BY created_at, relabel_id", a, b)
    except asyncpg.UndefinedTableError:
        return out
    for r in rows:
        out[(r["set_id"], r["set_version"])].append(dict(r))
    return out


async def list_sets(conn) -> list[dict]:
    """Every label set version with its counts (set_summary) and its queue items per state."""
    from services.ground_truth import review as rv

    sets = [(r["set_id"], r["set_version"]) for r in await conn.fetch(
        "SELECT DISTINCT set_id, set_version FROM shared.eval_claim_label ORDER BY set_id, set_version LIMIT 200")]
    queue = {(r["set_id"], r["set_version"], r["status"]): int(r["n"]) for r in await conn.fetch(
        "SELECT l.set_id, l.set_version, d.status, count(*) AS n FROM shared.writing_memory_disagreement d "
        "JOIN shared.eval_claim_label l ON l.label_id = d.source_ref WHERE d.source = 'golden_claim' "
        "GROUP BY 1, 2, 3")}
    rows_by = await store.read_rows_many(conn, sets)
    relabels_by = await read_relabels_many_soft(conn, sets)
    out = []
    for set_id, version in sets:
        rows = rows_by.get((set_id, version), [])
        relabels = relabels_by.get((set_id, version), [])
        out.append({"set_id": set_id, "set_version": version, **rv.set_summary(rows, relabels),
                    "queue": {s: queue.get((set_id, version, s), 0) for s in ("open", "resolved")}})
    return out


__all__ = ["list_sets", "read_relabels_many_soft"]
