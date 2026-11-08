"""AA-1048 — shared.eval_claim_label (CIS 388) reads and writes, and the 'golden_claim' items of the disagreement queue
(shared.writing_memory_disagreement, CIS 314 + 388).

Writes are DRY RUN unless `apply=True`. A label set version is append-only: panel rows are imported once per
(set_id, set_version) (a relabel is a new version); tech-lead rows are added to the version as the adjudication goes
on, at most one per claim (the DB key). Nothing here is on a live path: the CLI (scripts/aa1048/cli.py) calls it as
cis_app.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

COLUMNS = ("set_id", "set_version", "writer", "scope", "tenant_id", "sample_id", "piece_id", "tour_id", "version_id",
           "content_sha256", "field", "span_start", "span_end", "claim_text", "claim_hash", "claim_class", "label",
           "labeller_kind", "labeller", "lens", "reason", "split", "stratum", "adjudication", "policy_sha")
_UUID_COLS = ("tenant_id", "piece_id", "tour_id", "version_id")
_INSERT_SQL = (
    f"INSERT INTO shared.eval_claim_label ({', '.join(COLUMNS)}) VALUES ("
    + ", ".join(f"${i + 1}" + ("::uuid" if c in _UUID_COLS else "") for i, c in enumerate(COLUMNS))
    + ") ON CONFLICT ON CONSTRAINT eval_claim_label_judgement_key DO NOTHING RETURNING label_id::text AS label_id")
_SELECT_COLS = ", ".join(f"{c}::text AS {c}" if c in _UUID_COLS else c for c in COLUMNS)
_READ_SQL = (f"SELECT label_id::text AS label_id, {_SELECT_COLS} "
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


def queue_candidates(rows: list[dict]) -> list[dict]:
    """One queue item per disputed or audit-drawn claim: source_ref = its first panel row (lens order), machine_verdict
    = the panel's common label ('split' when the lenses have no majority), human_label = the panel state
    ('disputed' | 'audit'), the tech lead's work. Claims that already have a tech-lead row are left out."""
    from collections import Counter, defaultdict

    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_hash[r["claim_hash"]].append(r)
    out = []
    for _h, rs in sorted(by_hash.items()):
        if any(r["labeller_kind"] == "tech_lead" for r in rs):
            continue
        panel = sorted((r for r in rs if r["labeller_kind"] == "agent_panel"), key=lambda r: r["lens"] or "")
        if not panel or panel[0]["adjudication"] not in ("disputed", "audit"):
            continue
        top = Counter(r["label"] for r in panel).most_common()
        machine = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "split"
        out.append({"source_ref": panel[0]["label_id"], "writer": panel[0]["writer"], "scope": panel[0]["scope"],
                    "tenant_id": panel[0]["tenant_id"], "machine_verdict": machine,
                    "human_label": panel[0]["adjudication"]})
    return out


async def enqueue(conn, rows: list[dict], *, today: date, apply: bool) -> dict:
    """Add the golden_claim items of a label set version to the disagreement queue (DRY RUN: count the new ones).
    An item is never added twice (unique source + source_ref). The queue's weekly cap (QUEUE_MAX of the KPI job)
    does not apply: these are the W2 adjudication work list."""
    items = queue_candidates(rows)
    week = week_start(today)
    added = 0
    for it in items:
        if not apply:
            exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM shared.writing_memory_disagreement WHERE "
                                         "source = $1 AND source_ref = $2::uuid) AS ok", QUEUE_SOURCE,
                                         it["source_ref"])
            added += 0 if exists else 1
            continue
        got = await conn.fetchval(
            "INSERT INTO shared.writing_memory_disagreement (source, source_ref, writer, scope, tenant_id, "
            "machine_verdict, human_label, week_start) VALUES ($1, $2::uuid, $3, $4, $5::uuid, $6, $7, $8) "
            "ON CONFLICT (source, source_ref) DO NOTHING RETURNING item_id::text AS item_id",
            QUEUE_SOURCE, it["source_ref"], it["writer"], it["scope"], it["tenant_id"], it["machine_verdict"],
            it["human_label"], week)
        added += 1 if got else 0
    return {"candidates": len(items), "added": added, "week_start": week.isoformat(), "dry_run": not apply}


def resolution_label(machine_verdict: str, resolution: str) -> str:
    """judge_right: the panel's common label stands; human_right: it flips (violation <-> ok); unclear, or a panel
    with no majority / an unsure majority: unsure (the claim stays without a usable label)."""
    if resolution == "judge_right" and machine_verdict in _FLIP:
        return machine_verdict
    if resolution == "human_right" and machine_verdict in _FLIP:
        return _FLIP[machine_verdict]
    return "unsure"


async def pull_resolutions(conn, set_id: str, set_version: str, *, apply: bool,
                           policy_sha: Optional[str] = None) -> dict:
    """Turn the resolved golden_claim queue items of a label set version into tech-lead rows (DRY RUN: list them)."""
    resolved = await conn.fetch(
        "SELECT d.source_ref::text AS label_id, d.machine_verdict, d.resolution, d.resolved_by "
        "FROM shared.writing_memory_disagreement d JOIN shared.eval_claim_label l ON l.label_id = d.source_ref "
        "WHERE d.source = $1 AND d.status = 'resolved' AND l.set_id = $2 AND l.set_version = $3",
        QUEUE_SOURCE, set_id, set_version)
    rows = {r["label_id"]: r for r in await read_rows(conn, set_id, set_version)}
    lead_claims = {r["claim_hash"] for r in rows.values() if r["labeller_kind"] == "tech_lead"}
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


__all__ = ["COLUMNS", "QUEUE_SOURCE", "StoreRefused", "enqueue", "existing_judgements", "insert_rows",
           "panel_rows_exist", "pull_resolutions", "queue_candidates", "read_rows", "resolution_label", "week_start"]
