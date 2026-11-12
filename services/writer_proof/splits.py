"""AA-1051 — the frozen DEV / CAL / GATE side of each golden item (CIS 399, shared.eval_golden_split).

The S271 audit (Writing Lab redesign, "golden v2") gives the golden set three disjoint uses: DEV (rubric and policy
work), CAL (judge calibration) and GATE (sealed: release and W5 proof decisions only). A sample's side is fixed by a
hash of its sample id under a named RULE, so a sample keeps its side in every golden version (the sample id is the
identity across versions; R2 pairs controls on it) and adding samples never moves an existing one.

    bucket = int(sha256("aa1051-eval:<sample_id>")[:8], 16) / 2**32, floored to 6 decimals   (0 <= bucket < 1)
    cal    bucket <  0.30
    gate   0.30 <= bucket < 0.60
    dev    bucket >= 0.60

Per sample, not per tour (the AA-1048 claim split is per tour): the T9 writers have about 20 blogs on 3 tours, so a
per-tour GATE would hold 0 or 1 tour. The sides of the item-level eval do not share labelled sentences with the claim
calibration (R1 reads claim labels, split by tour), and the cost of a tour on two sides is a shared source, not shared
text.

A stored row is the truth: readers (R2 `PgStore.items`, the proof runner) read the stored side, never recompute it,
so a later rule (v2) cannot move an item of an existing set version. The importer writes the rows of a new version;
`write_missing` (CLI below, DRY RUN first) fills an existing set.

    python -m services.writer_proof.splits [--set aa729] [--version 1.3.0]                  # DRY RUN: counts only
    python -m services.writer_proof.splits --apply [--assigned-by <who>]                     # writes the missing rows

DB login DATABASE_URL (cis_app in the container), ssl required unless GOLDEN_DB_SSL=disable (local tests). Output:
one JSON document, ids and counts only. $0: no model call.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter
from typing import Any, Iterable, Optional

RULE = "aa1051-split-v1"
SEED = "aa1051-eval"
SIDES = ("dev", "cal", "gate")
# (side, upper bound of its bucket range), in bucket order: cal 30%, gate 30%, dev 40%
BOUNDS = (("cal", 0.30), ("gate", 0.60), ("dev", 1.0))
DEFAULT_SET = "aa729"

TABLE_SQL = "SELECT to_regclass('shared.eval_golden_split') IS NOT NULL AS ok"
SET_SQL = """
    SELECT set_id::text AS set_id, name, version, status FROM shared.eval_golden_set
     WHERE name = $1 AND ($2::text IS NULL AND status = 'active' OR version = $2)
"""
ITEMS_SQL = """
    SELECT g.sample_id, g.writer, g.trap, s.eval_split, s.rule, s.bucket::float AS bucket
      FROM shared.eval_golden_item g
      LEFT JOIN shared.eval_golden_split s ON s.set_id = g.set_id AND s.sample_id = g.sample_id
     WHERE g.set_id = $1::uuid
     ORDER BY g.sample_id
"""
INSERT_SQL = """
    INSERT INTO shared.eval_golden_split (set_id, sample_id, eval_split, rule, bucket, assigned_by)
    SELECT $1::uuid, x.sample_id, x.eval_split, $5, x.bucket, $6
      FROM unnest($2::text[], $3::text[], $4::numeric[]) AS x(sample_id, eval_split, bucket)
    ON CONFLICT (set_id, sample_id) DO NOTHING
"""


class SplitRefused(RuntimeError):
    """The DB state forbids the backfill (no such set, the table is missing)."""


def bucket_of(sample_id: str) -> float:
    """The sample's position in the hash space, floored to the 6 decimals the column keeps (0 <= bucket < 1)."""
    raw = int(hashlib.sha256(f"{SEED}:{sample_id}".encode("utf-8")).hexdigest()[:8], 16) / 2 ** 32
    return math.floor(raw * 1_000_000) / 1_000_000


def side_of(bucket: float) -> str:
    for side, upper in BOUNDS:
        if bucket < upper:
            return side
    raise ValueError(f"bucket {bucket} is outside [0, 1)")


def split_of(sample_id: str) -> str:
    return side_of(bucket_of(sample_id))


def assign(sample_ids: Iterable[str]) -> list[dict]:
    """[{sample_id, eval_split, bucket}] of the rule, one per distinct sample id, in id order. Pure."""
    return [{"sample_id": s, "eval_split": split_of(s), "bucket": bucket_of(s)} for s in sorted(set(sample_ids))]


async def table_exists(conn) -> bool:
    return bool(await conn.fetchval(TABLE_SQL))


def summarise(rows: list[dict]) -> dict:
    """Counts per writer and side of the stored rows, the items without a row (and the side the rule would give
    them), and stored rows whose rule is this RULE but whose side differs from it (should be 0). Pure."""
    stored: dict[str, Counter] = {}
    proposed: dict[str, Counter] = {}
    drift = []
    for r in rows:
        w = str(r["writer"])
        if r.get("eval_split"):
            stored.setdefault(w, Counter())[r["eval_split"]] += 1
            if r.get("rule") == RULE and split_of(r["sample_id"]) != r["eval_split"]:
                drift.append(r["sample_id"])
        else:
            proposed.setdefault(w, Counter())[split_of(r["sample_id"])] += 1
    return {"rule": RULE, "stored": {w: dict(sorted(c.items())) for w, c in sorted(stored.items())},
            "missing": sum(sum(c.values()) for c in proposed.values()),
            "proposed_for_missing": {w: dict(sorted(c.items())) for w, c in sorted(proposed.items())},
            "rule_drift": drift[:20]}


async def write_missing(conn, set_id: str, *, assigned_by: str) -> dict:
    """Insert the rule's side for every item of the set that has none, in the caller's transaction. Idempotent (ON
    CONFLICT DO NOTHING: a stored side is never changed). Returns {"inserted": n, "summary": ...}."""
    rows = [dict(r) for r in await conn.fetch(ITEMS_SQL, set_id)]
    todo = assign(r["sample_id"] for r in rows if not r.get("eval_split"))
    if todo:
        await conn.execute(INSERT_SQL, set_id, [t["sample_id"] for t in todo], [t["eval_split"] for t in todo],
                           [t["bucket"] for t in todo], RULE, assigned_by[:128])
    after = [dict(r) for r in await conn.fetch(ITEMS_SQL, set_id)]
    return {"inserted": len(todo), "summary": summarise(after)}


async def run(conn, *, name: str, version: Optional[str], apply: bool, assigned_by: str) -> dict:
    if not await table_exists(conn):
        raise SplitRefused("shared.eval_golden_split does not exist (CIS 399 not applied)")
    gs = await conn.fetchrow(SET_SQL, name, version)
    if gs is None:
        raise SplitRefused(f"no golden set {name}@{version or 'active'}")
    out: dict[str, Any] = {"mode": "apply" if apply else "dry_run", "set": dict(gs)}
    if not apply:
        async with conn.transaction(readonly=True):
            out["summary"] = summarise([dict(r) for r in await conn.fetch(ITEMS_SQL, gs["set_id"])])
        return out
    async with conn.transaction():
        out.update(await write_missing(conn, gs["set_id"], assigned_by=assigned_by))
    return out


async def _connect():
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("GOLDEN_DB_SSL", "require") == "disable" else "require"
    return await asyncpg.connect(get_database_url().strip(), ssl=ssl)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.splits", description=__doc__.split("\n")[0])
    ap.add_argument("--set", default=DEFAULT_SET, help="golden set name (default aa729)")
    ap.add_argument("--version", help="a set version (default: the active one)")
    ap.add_argument("--apply", action="store_true", help="write the missing rows (default: DRY RUN)")
    ap.add_argument("--assigned-by", default="split_backfill:cli")
    a = ap.parse_args(argv)

    async def _go() -> dict:
        conn = await _connect()
        try:
            return await run(conn, name=a.set, version=a.version, apply=a.apply, assigned_by=a.assigned_by)
        finally:
            await conn.close()

    try:
        report = asyncio.run(_go())
    except SplitRefused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2
    print(json.dumps({"ok": True, **report}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
