"""AA-919 (WM-17) — import an AA-729 golden-set fixture into shared.eval_golden_set / eval_golden_item and write the
baseline_import runs (shared.prompt_eval_runs + eval_item_result). $0: no model call (llm_guard), reads only the
fixture (and optional AA-729 score files) and the DB.

    python -m services.golden_set.importer --fixture s3:scripts/out/aa729/fixture-1.1.0.json            # DRY RUN
    python -m services.golden_set.importer --fixture s3:scripts/out/aa729/fixture-1.1.0.json --apply    # write
        [--baseline s3:scripts/out/aa729/<run>/score-score-only.json --baseline s3:…/score-regenerate.json]
        [--name aa729] [--holdout-pct 0.300] [--imported-by <who>] [--no-db] [--allow-downgrade]

--fixture / --baseline take a local path or `s3:<key>` in the CIS bronze bucket, whose name comes from the
environment (BRONZE_BUCKET, shared/aws_client/bronze.py) — never a bucket name or account in an argument or in the
stored source_uri. The fixture may also come from GOLDEN_FIXTURE.

DRY RUN (default) builds every row, prints the counts per writer / split / tier and, unless --no-db, checks the DB
read-only (READ ONLY transaction): the tenants exist and are not offboarded, the version is new or was imported from
the same bytes, a new version is newer than the active one (else refused unless --allow-downgrade). It writes
nothing. --apply does the same checks and writes in ONE transaction (advisory lock per set name): the set
(superseding the previous active version), its items, and one baseline_import run per writer that has none yet for
this set. Re-running is a no-op. The DB login is DATABASE_URL (cis_app in the container), ssl required
unless GOLDEN_DB_SSL=disable (local tests).

Output: one JSON document on stdout, counts and ids only (no text).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from decimal import Decimal
from typing import Any, Optional

from services.golden_set import derive, llm_guard

DEFAULT_NAME = "aa729"


def _read(ref: str) -> tuple[bytes, str]:
    """(bytes, source_uri) of a local path or s3:<key> in the bronze bucket."""
    if ref.startswith("s3://"):
        raise SystemExit("give the bronze key as s3:<key>; the bucket comes from the environment (BRONZE_BUCKET)")
    if ref.startswith("s3:"):
        import boto3

        from shared.aws_client.bronze import bronze_bucket

        key = ref[3:].lstrip("/")
        body = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).get_object(
            Bucket=bronze_bucket(), Key=key)["Body"].read()
        return body, f"s3:{key}"
    with open(ref, "rb") as f:
        return f.read(), f"file:{os.path.basename(ref)}"


async def _connect():
    import asyncpg

    from shared.secrets import get_database_url

    ssl: Any = None if os.environ.get("GOLDEN_DB_SSL", "require") == "disable" else "require"
    return await asyncpg.connect(get_database_url().strip(), ssl=ssl)


def prepare(fixture_ref: str, baseline_refs: list[str], *, name: str, pct: Decimal,
            imported_by: str) -> tuple[derive.GoldenSet, dict]:
    raw, uri = _read(fixture_ref)
    fixture = json.loads(raw)
    gs = derive.build(fixture, name=name, source_uri=uri, source_sha256=hashlib.sha256(raw).hexdigest(),
                      imported_by=imported_by, pct=pct)
    files = []
    for ref in baseline_refs:
        body, buri = _read(ref)
        files.append((buri, json.loads(body)))
    return gs, derive.baseline(gs, files)


def _baseline_report(b: dict) -> dict:
    return {w: {"results": len(v["results"]), "by_mode_verdict": v["run"]["details"]["results"],
                "skipped_post_edit": v["run"]["details"]["skipped_post_edit"], "source": v["run"]["details"]["source"]}
            for w, v in b.items()}


async def run(gs: derive.GoldenSet, baseline: dict, *, apply: bool, conn=None, allow_downgrade: bool = False) -> dict:
    from services.golden_set import store

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "set": {"name": gs.set_row["name"], "version": gs.set_row["version"], "source_uri": gs.set_row["source_uri"],
                "source_sha256": gs.set_row["source_sha256"], "seed": gs.set_row["seed"]},
        "counts": derive.count_report(gs),
        "baseline": _baseline_report(baseline),
        "tenants": gs.tenant_ids,
    }
    if conn is None:
        report["db"] = "not checked (--no-db)"
        return report
    if not apply:
        async with conn.transaction(readonly=True):
            report["db"] = await store.plan(conn, gs, allow_downgrade=allow_downgrade)
        report["ok"] = not report["db"]["problems"]
        return report
    async with conn.transaction():
        report["db"] = await store.apply(conn, gs, baseline, allow_downgrade=allow_downgrade)
    report["ok"] = True
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.golden_set.importer", description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", default=os.environ.get("GOLDEN_FIXTURE"),
                    help="local path or s3:<key> (bronze bucket from BRONZE_BUCKET)")
    ap.add_argument("--baseline", action="append", default=[], help="AA-729 score file, path or s3:<key> (repeat)")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--holdout-pct", default=str(derive.DEFAULT_HOLDOUT_PCT))
    ap.add_argument("--imported-by", default=os.environ.get("GOLDEN_IMPORTED_BY", "golden_import"))
    ap.add_argument("--apply", action="store_true", help="write (default: DRY RUN)")
    ap.add_argument("--no-db", action="store_true", help="DRY RUN without the DB checks")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="let a version older than the active one become active (default: refused)")
    a = ap.parse_args(argv)
    if not a.fixture:
        ap.error("--fixture (or GOLDEN_FIXTURE) is required")
    if a.apply and a.no_db:
        ap.error("--apply needs the DB")
    pct = Decimal(a.holdout_pct)
    if not Decimal(0) < pct < Decimal(1):
        ap.error("--holdout-pct must be between 0 and 1")

    llm_guard.install("golden_import")
    try:
        gs, baseline = prepare(a.fixture, a.baseline, name=a.name, pct=pct, imported_by=a.imported_by[:128])
    except derive.FixtureError as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        return 2

    async def _go() -> dict:
        if a.no_db:
            return await run(gs, baseline, apply=False, conn=None)
        conn = await _connect()
        try:
            return await run(gs, baseline, apply=a.apply, conn=conn, allow_downgrade=a.allow_downgrade)
        finally:
            await conn.close()

    from services.golden_set.store import ImportRefused

    try:
        report = asyncio.run(_go())
    except ImportRefused as e:
        print(json.dumps({"ok": False, "mode": "apply", "refused": str(e)}))
        return 2
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
