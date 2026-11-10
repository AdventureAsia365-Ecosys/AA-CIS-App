"""AA-1048 — shared.eval_claim_label (CIS 388) reads and writes, and the 'golden_claim' items of the disagreement queue
(shared.writing_memory_disagreement, CIS 314 + 388).

Writes are DRY RUN unless `apply=True`. A label set version is append-only: panel rows are imported once per
(set_id, set_version) (a relabel is a new version); tech-lead rows are added to the version as the adjudication goes
on, at most one per claim (the DB key). Nothing here is on a live path: the CLI (scripts/aa1048/cli.py) calls it as
cis_app.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Optional

COLUMNS = ("set_id", "set_version", "writer", "scope", "tenant_id", "sample_id", "piece_id", "tour_id", "version_id",
           "content_sha256", "field", "span_start", "span_end", "claim_text", "claim_hash", "claim_class", "label",
           "labeller_kind", "labeller", "lens", "reason", "split", "stratum", "adjudication", "policy_sha")
_UUID_COLS = ("tenant_id", "piece_id", "tour_id", "version_id")
_INSERT_SQL = (
    f"INSERT INTO shared.eval_claim_label ({', '.join(COLUMNS)}) VALUES ("
    + ", ".join(f"${i + 1}" + ("::uuid" if c in _UUID_COLS else "") for i, c in enumerate(COLUMNS))
    + ") ON CONFLICT ON CONSTRAINT eval_claim_label_judgement_key DO NOTHING RETURNING label_id::text AS label_id")
_SELECT_COLS = ", ".join(f"{c}::text AS {c}" if c in _UUID_COLS else c for c in COLUMNS)
_READ_SQL = (f"SELECT label_id::text AS label_id, created_at, {_SELECT_COLS} "
             "FROM shared.eval_claim_label WHERE set_id = $1 AND set_version = $2 "
             "ORDER BY claim_hash, labeller_kind, lens NULLS LAST")
QUEUE_SOURCE = "golden_claim"
# resolution of a golden_claim queue item -> the tech-lead label, given the panel's common label
_FLIP = {"violation": "ok", "ok": "violation"}


class StoreRefused(ValueError):
    """The write would break the append-only rule of a label set version: nothing is written."""


async def panel_rows_exist(conn, set_id: str, set_version: str) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) AS n FROM shared.eval_claim_label WHERE set_id = $1 AND set_version = $2 "
        "AND labeller_kind = 'agent_panel'", set_id, set_version) or 0)


async def insert_rows(conn, rows: list[dict], *, apply: bool) -> dict:
    """Insert the rows of `panel.consolidate` (DRY RUN: count them). Panel rows into a version that already has panel
    rows are refused; a tech-lead row whose claim already has one is skipped (counted)."""
    sets = {(r["set_id"], r["set_version"]) for r in rows}
    panel = [r for r in rows if r["labeller_kind"] == "agent_panel"]
    for set_id, version in sorted(sets):
        if panel and await panel_rows_exist(conn, set_id, version):
            raise StoreRefused(f"{set_id}@{version} already has panel rows: import a relabel as a new set version")
    summary = {"rows": len(rows), "panel": len(panel), "tech_lead": len(rows) - len(panel), "inserted": 0,
               "skipped": 0, "dry_run": not apply, "sets": sorted(f"{s}@{v}" for s, v in sets)}
    if not apply:
        return summary
    async with conn.transaction():
        for r in rows:
            got = await conn.fetchval(_INSERT_SQL, *[r.get(c) for c in COLUMNS])
            if got:
                summary["inserted"] += 1
            else:
                summary["skipped"] += 1
    return summary


async def read_rows(conn, set_id: str, set_version: str) -> list[dict]:
    return [dict(r) for r in await conn.fetch(_READ_SQL, set_id, set_version)]


async def existing_judgements(conn, set_id: str, set_version: str) -> dict[tuple, tuple[str, str]]:
    """(claim_hash, labeller_kind, lens) -> (claim_class, label) of the rows a version already holds (the import's
    idempotency check, services/ground_truth/importer.py). Labels and keys only: no text is read."""
    rows = await conn.fetch(
        "SELECT claim_hash, labeller_kind, lens, claim_class, label FROM shared.eval_claim_label "
        "WHERE set_id = $1 AND set_version = $2", set_id, set_version)
    return {(r["claim_hash"], r["labeller_kind"], r["lens"]): (r["claim_class"], r["label"]) for r in rows}


def week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


# The adjudication order (S273 tech-lead decision "t9-heldout-first"): held-out claims first (the only side a gate
# decision reads), then the T9 surface (D7, the claim instrument's first surface), then one object at a time so the
# tech lead reads the claims of one piece / version together.
WRITER_ORDER = ("t9_blog", "t9_social", "t2", "a1")
SPLITS = ("held_out", "train")


def _queue_order(item: dict) -> tuple:
    w = item.get("writer")
    return (item.get("split") != "held_out", WRITER_ORDER.index(w) if w in WRITER_ORDER else len(WRITER_ORDER),
            item.get("content_sha256") or "", item.get("span_start") or 0, item.get("claim_hash") or "")


def queue_candidates(rows: list[dict], *, split: Optional[str] = None,
                     writers: Optional[Iterable[str]] = None) -> list[dict]:
    """One queue item per disputed or audit-drawn claim: source_ref = its first panel row (lens order), machine_verdict
    = the panel's common label ('split' when the lenses have no majority), human_label = the panel state
    ('disputed' | 'audit'), the tech lead's work. Claims that already have a tech-lead row are left out.

    `split` / `writers` keep the claims of that split / those writers only (S273: held-out T9 claims first). The list
    is in adjudication order (WRITER_ORDER above): held-out first, T9 first, then by object and position."""
    from collections import Counter, defaultdict

    if split is not None and split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, not {split!r}")
    keep = None if writers is None else set(writers)
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_hash[r["claim_hash"]].append(r)
    out = []
    for h, rs in sorted(by_hash.items()):
        if any(r["labeller_kind"] == "tech_lead" for r in rs):
            continue
        panel = sorted((r for r in rs if r["labeller_kind"] == "agent_panel"), key=lambda r: r["lens"] or "")
        if not panel or panel[0]["adjudication"] not in ("disputed", "audit"):
            continue
        first = panel[0]
        if split is not None and first.get("split") != split:
            continue
        if keep is not None and first["writer"] not in keep:
            continue
        top = Counter(r["label"] for r in panel).most_common()
        machine = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "split"
        out.append({"source_ref": first["label_id"], "writer": first["writer"], "scope": first["scope"],
                    "tenant_id": first["tenant_id"], "machine_verdict": machine,
                    "human_label": first["adjudication"], "split": first.get("split"), "claim_hash": h,
                    "content_sha256": first.get("content_sha256"), "span_start": first.get("span_start")})
    return sorted(out, key=_queue_order)


_QUEUED_SQL = ("SELECT source_ref::text AS source_ref, status FROM shared.writing_memory_disagreement "
               "WHERE source = $1 AND source_ref = ANY($2::uuid[])")
_QUEUE_INSERT_SQL = (
    "INSERT INTO shared.writing_memory_disagreement (source, source_ref, writer, scope, tenant_id, "
    "machine_verdict, human_label, week_start) VALUES ($1, $2::uuid, $3, $4, $5::uuid, $6, $7, $8) "
    "ON CONFLICT (source, source_ref) DO NOTHING RETURNING item_id::text AS item_id")


async def enqueue(conn, rows: list[dict], *, today: date, apply: bool, split: Optional[str] = None,
                  writers: Optional[Iterable[str]] = None, max_open: Optional[int] = None) -> dict:
    """Add the golden_claim items of a label set version to the disagreement queue (DRY RUN: count the new ones).
    An item is never added twice (unique source + source_ref). The queue's weekly cap (QUEUE_MAX of the KPI job)
    does not apply: these are the W2 adjudication work list.

    Batches (S273, decision "t9-heldout-first"): `split` / `writers` narrow the candidates and `max_open` caps the
    open items of this set version in the queue after the call (the batch the tech lead works through in one sitting).
    Items are added in adjudication order (queue_candidates); a later call tops the queue up to `max_open` again
    with the next items once the tech lead has resolved some. `remaining` = candidates still outside the queue."""
    if max_open is not None and max_open < 1:
        raise ValueError("max_open must be >= 1")
    items = queue_candidates(rows, split=split, writers=writers)
    refs = sorted({r["label_id"] for r in rows if r["labeller_kind"] == "agent_panel" and r.get("label_id")})
    queued = {r["source_ref"]: r["status"] for r in await conn.fetch(_QUEUED_SQL, QUEUE_SOURCE, refs)} if refs else {}
    open_before = sum(1 for s in queued.values() if s == "open")
    room = None if max_open is None else max(0, max_open - open_before)
    week = week_start(today)
    added = already = 0
    for it in items:
        if it["source_ref"] in queued:
            already += 1
            continue
        if room is not None and added >= room:
            continue
        if not apply:
            added += 1
            continue
        got = await conn.fetchval(_QUEUE_INSERT_SQL, QUEUE_SOURCE, it["source_ref"], it["writer"], it["scope"],
                                  it["tenant_id"], it["machine_verdict"], it["human_label"], week)
        if got:
            added += 1
        else:                       # queued by a concurrent call since the read above
            already += 1
    remaining = len(items) - already - added
    out = {"candidates": len(items), "added": added, "week_start": week.isoformat(), "dry_run": not apply}
    if split is not None or writers is not None or max_open is not None:
        out.update(already_queued=already, open_before=open_before, open_after=open_before + added,
                   remaining=remaining, batches_left=(math.ceil(remaining / max_open) if max_open else None),
                   filter={"split": split, "writers": sorted(writers) if writers is not None else None,
                           "max_open": max_open})
    return out


def resolution_label(machine_verdict: str, resolution: str) -> str:
    """judge_right: the panel's common label stands; human_right: it flips (violation <-> ok); unclear, or a panel
    with no majority / an unsure majority: unsure (the claim stays without a usable label)."""
    if resolution == "judge_right" and machine_verdict in _FLIP:
        return machine_verdict
    if resolution == "human_right" and machine_verdict in _FLIP:
        return _FLIP[machine_verdict]
    return "unsure"


async def relabelled_claims(conn, set_id: str, set_version: str) -> set[str]:
    """The claims of a label set version with a tech-lead correction (shared.eval_claim_relabel, CIS 394; an empty set
    before it)."""
    import asyncpg

    try:
        async with conn.transaction():
            return {r["claim_hash"] for r in await conn.fetch(
                "SELECT DISTINCT claim_hash FROM shared.eval_claim_relabel WHERE set_id = $1 AND set_version = $2",
                set_id, set_version)}
    except asyncpg.UndefinedTableError:
        return set()


async def pull_resolutions(conn, set_id: str, set_version: str, *, apply: bool,
                           policy_sha: Optional[str] = None) -> dict:
    """Turn the resolved golden_claim queue items of a label set version into tech-lead rows (DRY RUN: list them).
    A claim that already has a tech-lead row or a correction is skipped: its decision stands (AA-1063: a stored row
    written after a correction would silently become the final label)."""
    resolved = await conn.fetch(
        "SELECT d.source_ref::text AS label_id, d.machine_verdict, d.resolution, d.resolved_by "
        "FROM shared.writing_memory_disagreement d JOIN shared.eval_claim_label l ON l.label_id = d.source_ref "
        "WHERE d.source = $1 AND d.status = 'resolved' AND l.set_id = $2 AND l.set_version = $3",
        QUEUE_SOURCE, set_id, set_version)
    rows = {r["label_id"]: r for r in await read_rows(conn, set_id, set_version)}
    lead_claims = {r["claim_hash"] for r in rows.values() if r["labeller_kind"] == "tech_lead"}
    lead_claims |= await relabelled_claims(conn, set_id, set_version)
    new_rows = []
    for d in resolved:
        panel = rows.get(d["label_id"])
        if panel is None or panel["claim_hash"] in lead_claims:
            continue
        row = {c: panel.get(c) for c in COLUMNS}
        row.update(labeller_kind="tech_lead", labeller=str(d["resolved_by"] or "tech_lead")[:128], lens=None,
                   label=resolution_label(d["machine_verdict"], d["resolution"]), adjudication="final",
                   reason=f"queue: {d['resolution']}", policy_sha=policy_sha or panel.get("policy_sha"))
        new_rows.append(row)
    summary = await insert_rows(conn, new_rows, apply=apply) if new_rows else {
        "rows": 0, "inserted": 0, "skipped": 0, "dry_run": not apply}
    summary["resolved_items"] = len(resolved)
    return summary


__all__ = ["COLUMNS", "QUEUE_SOURCE", "SPLITS", "StoreRefused", "WRITER_ORDER", "enqueue", "existing_judgements",
           "insert_rows", "panel_rows_exist", "pull_resolutions", "queue_candidates", "read_rows", "relabelled_claims",
           "resolution_label",
           "week_start"]
