"""AA-1093 part 3 — the one-tenant restore drill of CIS (gap M27; the CRM's is AA-Partner app/portability/restore_drill;
runbook: root docs/ops/runbooks, tenant restore drill).

RDS point-in-time recovery restores the whole database of all four apps, never one tenant. A tenant is recovered from
its own backup (job `tenant_backup`, services/tenant_backup/export.py); this drill proves a backup restores. In the
CIS API container (it has the cis_app DSN and reads the gold bucket), through ECS exec on aa-cis-dev-api:

    python -m services.tenant_backup.restore_drill                         # the pilot tenant, its newest backup
    python -m services.tenant_backup.restore_drill --tenant <uuid>         # another tenant (its newest backup)
    python -m services.tenant_backup.restore_drill --tenant <uuid> --backup <job id>

Steps:
1. the backup: a succeeded `tenant_backup` job of the tenant (the newest, unless --backup names one);
2. SHA256SUMS: its SHA-256 must equal the job result's `sha256sums_sha256`; manifest.json and every table file must
   match their line (and the manifest's own `sha256`); the manifest must name this tenant and backup;
3. restore into a throwaway schema: per table a temporary table (the session's own `pg_temp`, ON COMMIT DROP) made
   from the LIVE table's columns and types (`CREATE TEMP TABLE ... AS SELECT * FROM <live> WITH NO DATA`: types, no
   constraints), loaded from the JSONL with `jsonb_populate_recordset`, so a value that no longer parses as the live
   type fails the drill. A table no longer live is restored as text columns;
4. compare per table: the manifest's count, the restored count, the live count now (the erasure predicate for this
   tenant), how many restored primary keys exist live and how many restored rows are identical to the live row
   (`to_jsonb` equal).

The drill transaction is ROLLED BACK at the end, whatever happens: its only statements are reads of the live tables
and writes to its own temporary tables, and nothing of it is committed. Then one acp_shared.audit_log row
`tenant.restore_drill` (ids and counts only) is written in its own statement. Output: one JSON object of ids, table
names, counts and digests (never a row value). Pass (`ok`): every digest matches and restored = manifest for every
table; `complete`: ok and the backup itself was complete (no table skipped by the size cap, none unregistered).
Exit 0 = complete, 3 = restored but the backup is incomplete, 1 = failed, 2 = not configured.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
import tempfile
import uuid
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.erasure import inventory as inv
from services.erasure import plan as pl
from services.tenant_backup import catalogue as cat
from services.tenant_backup.export import parse_sums, sha256_file
from services.tenant_backup.store import ObjectMissing, ObjectStore

ACTOR = "job:cis_restore_drill"
AUDIT_ACTION = AdminTenantAuditAction.RESTORE_DRILL
KIND = "tenant_backup"
STATEMENT_TIMEOUT = "300s"
BATCH_ROWS = 500
BATCH_BYTES = 8 * 1024 * 1024

TENANT_SQL = "SELECT tenant_id::text AS tenant_id FROM shared.tenants WHERE tenant_id = $1::uuid"
PILOT_SQL = "SELECT tenant_id::text AS tenant_id FROM shared.tenants WHERE is_pilot LIMIT 1"
LATEST_SQL = """
    SELECT id::text AS id, status, payload, result, finished_at FROM shared.job
     WHERE kind = 'tenant_backup' AND status = 'succeeded' AND payload ->> 'tenant_id' = $1::text
     ORDER BY finished_at DESC, id DESC LIMIT 1
"""
ONE_SQL = "SELECT id::text AS id, status, payload, result, finished_at FROM shared.job WHERE id = $1::uuid AND kind = $2"
LIVE_SQL = "SELECT to_regclass($1::text) IS NOT NULL AS ok"


class DrillError(Exception):
    """A drill that cannot go on: the code says why."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _json(v: Any) -> dict:
    if isinstance(v, str):
        return json.loads(v)
    return dict(v or {})


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _backup_job(conn, tenant_id: str, backup_id: Optional[str]) -> dict:
    if backup_id is None:
        row = await conn.fetchrow(LATEST_SQL, tenant_id)
        if row is None:
            raise DrillError("no_backup")
    else:
        row = await conn.fetchrow(ONE_SQL, backup_id, KIND)
        if row is None:
            raise DrillError("no_backup")
    job = {"id": row["id"], "status": row["status"], "payload": _json(row["payload"]),
           "result": _json(row["result"]), "finished_at": row["finished_at"]}
    if job["payload"].get("tenant_id") != tenant_id:
        raise DrillError("backup_of_another_tenant")
    if job["status"] != "succeeded" or not job["result"].get("sha256sums_sha256"):
        raise DrillError("backup_not_ready")
    return job


async def _fetch(store: ObjectStore, key: str, path: str) -> None:
    try:
        await store.get_file(key, path)
    except ObjectMissing:
        raise DrillError("object_missing") from None


def _batches(path: str):
    """Lists of JSON lines from a .jsonl.gz file, at most BATCH_ROWS lines / BATCH_BYTES each."""
    batch: list[str] = []
    size = 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            batch.append(line)
            size += len(line)
            if len(batch) >= BATCH_ROWS or size >= BATCH_BYTES:
                yield batch
                batch, size = [], 0
    if batch:
        yield batch


async def restore_table(conn, n: int, entry: dict, path: str, where: Optional[str], tenant_id: str) -> dict:
    """Load one table of the backup into pg_temp.rd_<n> and count it against the manifest and the live table."""
    table = str(entry["table"])
    temp = f"rd_{n}"
    live = bool(await conn.fetchval(LIVE_SQL, pl.qname(table)))
    if live:
        await conn.execute(f"CREATE TEMP TABLE {temp} ON COMMIT DROP AS "  # noqa: S608
                           f"SELECT * FROM {pl.qname(table)} WITH NO DATA")
    else:
        cols = entry.get("columns") or []
        spec = ", ".join(f"{ident(c)} text" for c in cols) or "drill_empty boolean"
        await conn.execute(f"CREATE TEMP TABLE {temp} ({spec}) ON COMMIT DROP")
    for batch in _batches(path):
        await conn.execute(
            f"INSERT INTO pg_temp.{temp} SELECT * FROM jsonb_populate_recordset(NULL::pg_temp.{temp}, $1::jsonb)",
            "[" + ",".join(batch) + "]")
    restored = int(await conn.fetchval(f"SELECT count(*) FROM pg_temp.{temp}"))  # noqa: S608
    live_rows = pk_matched = identical = None
    if live and where is not None:
        live_rows = int(await conn.fetchval(f"SELECT count(*) FROM {pl.qname(table)} t WHERE {where}",  # noqa: S608
                                            tenant_id))
    pk = [str(c) for c in entry.get("pk") or []]
    if live and pk:
        on = " AND ".join(f"l.{ident(c)} = d.{ident(c)}" for c in pk)
        pk_matched = int(await conn.fetchval(
            f"SELECT count(*) FROM pg_temp.{temp} d WHERE EXISTS "  # noqa: S608
            f"(SELECT 1 FROM {pl.qname(table)} l WHERE {on})"))
        identical = int(await conn.fetchval(
            f"SELECT count(*) FROM pg_temp.{temp} d WHERE EXISTS "  # noqa: S608
            f"(SELECT 1 FROM {pl.qname(table)} l WHERE {on} AND to_jsonb(l) = to_jsonb(d))"))
    manifest_rows = int(entry.get("rows", -1))
    return {"table": table, "manifest_rows": manifest_rows, "restored_rows": restored, "live_rows": live_rows,
            "live_pk_matched": pk_matched, "live_identical": identical, "live_table": live,
            "ok": restored == manifest_rows}


async def drill(pool, store: ObjectStore, tenant_id: str, backup_id: Optional[str] = None,
                registry: Optional[inv.Registry] = None) -> dict[str, Any]:
    """Run the drill for one tenant; the report (ids, table names, counts, digests). Raises DrillError."""
    try:
        tid = cat.check_tenant_id(tenant_id)
    except cat.BackupRefused as e:
        raise DrillError(e.code) from None
    if backup_id is not None:
        try:
            backup_id = str(uuid.UUID(str(backup_id)))
        except ValueError:
            raise DrillError("bad_backup_id") from None
    async with pool.acquire() as conn:
        if await conn.fetchrow(TENANT_SQL, tid) is None:
            raise DrillError("no_tenant")
        job = await _backup_job(conn, tid, backup_id)
        bid = job["id"]
        prefix = cat.object_prefix(tid, bid)
        with tempfile.TemporaryDirectory(prefix="restore-drill-") as work:
            spath = os.path.join(work, "SHA256SUMS")
            await _fetch(store, prefix + "SHA256SUMS", spath)
            if sha256_file(spath) != job["result"]["sha256sums_sha256"]:
                raise DrillError("sums_digest_mismatch")
            try:
                with open(spath, encoding="ascii") as f:
                    sums = parse_sums(f.read())
            except (ValueError, UnicodeDecodeError):
                raise DrillError("sums_shape") from None
            mpath = os.path.join(work, "manifest.json")
            await _fetch(store, prefix + "manifest.json", mpath)
            if sha256_file(mpath) != sums.get("manifest.json"):
                raise DrillError("checksum_mismatch")
            checked = 1
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
            if manifest.get("tenant_id") != tid or manifest.get("backup_id") != bid \
                    or manifest.get("format") != cat.FORMAT or not isinstance(manifest.get("tables"), list):
                raise DrillError("manifest_mismatch")
            catalog = await pl.load_catalog(conn)
            plan = pl.plan_tenant(registry or inv.Registry(), catalog)
            where = {tb: pl.predicate(plan.nodes, tb, "t") for tb in plan.nodes}
            results: list[dict] = []
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                for n, entry in enumerate(manifest["tables"]):
                    rel = str(entry.get("file") or "")
                    if rel not in sums or sums[rel] != entry.get("sha256"):
                        raise DrillError("checksum_missing_file")
                    path = os.path.join(work, f"t{n}.jsonl.gz")
                    await _fetch(store, prefix + rel, path)
                    if sha256_file(path) != sums[rel]:
                        raise DrillError("checksum_mismatch")
                    checked += 1
                    results.append(await restore_table(conn, n, entry, path, where.get(str(entry["table"])), tid))
                    os.remove(path)
            finally:
                await tx.rollback()           # the drill commits nothing: temp tables and all go with it
        ok = all(r["ok"] for r in results)
        complete = ok and bool(manifest.get("complete"))
        report = {
            "tenant_id": tid, "backup_id": bid,
            "backup_finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
            "schema_version_backup": manifest.get("schema_version"),
            "checksums_verified": checked,
            "tables": results, "table_count": len(results),
            "manifest_rows": sum(r["manifest_rows"] for r in results),
            "restored_rows": sum(r["restored_rows"] for r in results),
            "mismatched_tables": [r["table"] for r in results if not r["ok"]],
            "live_differs": [r["table"] for r in results if r["live_rows"] is not None
                             and r["live_rows"] != r["restored_rows"]],
            "skipped": manifest.get("skipped") or [], "unregistered": manifest.get("unregistered") or [],
            "excluded": manifest.get("excluded") or [],
            "ok": ok, "complete": complete,
        }
        reason = "mismatch" if not ok else ("ok" if complete else "incomplete_backup")
        await write_audit_log(conn, tenant_id=tid, actor=ACTOR, action=AUDIT_ACTION, resource_type="tenant_backup",
                              resource_id=bid, details={
                                  "backup_id": bid, "reason": reason, "ok": ok, "complete": complete,
                                  "tables": len(results), "manifest_rows": report["manifest_rows"],
                                  "restored_rows": report["restored_rows"], "checksums_verified": checked,
                                  "mismatched_tables": report["mismatched_tables"],
                                  "live_differs": report["live_differs"]})
    return report


def exit_code(report: dict) -> int:
    if not report.get("ok"):
        return 1
    return 0 if report.get("complete") else 3


def failure(exc: BaseException) -> dict:
    """A failed drill's report line: the code of a known refusal; otherwise only the class and SQLSTATE, never the
    message (a database error text can quote a row value)."""
    if isinstance(exc, DrillError):
        return {"ok": False, "error": exc.code}
    out: dict[str, Any] = {"ok": False, "error": "unexpected", "error_class": type(exc).__name__}
    state = getattr(exc, "sqlstate", None)
    if isinstance(state, str) and state:
        out["sqlstate"] = state
    return out


async def _main(argv: list[str]) -> int:  # pragma: no cover — the container entry point; drill() is tested
    import asyncpg

    from services.tenant_backup.store import S3Store

    parser = argparse.ArgumentParser(prog="python -m services.tenant_backup.restore_drill",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", default=None, help="the tenant id (default: the pilot tenant)")
    parser.add_argument("--backup", default=None, help="a tenant_backup job id (default: the newest succeeded)")
    args = parser.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"ok": False, "error": "not_configured"}))
        return 2
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, ssl=os.environ.get("DB_SSL", "require"), min_size=1, max_size=2,
                                         timeout=10, command_timeout=600,
                                         server_settings={"application_name": "cis-restore-drill"})
        tenant = args.tenant
        if tenant is None:
            tenant = await pool.fetchval(PILOT_SQL)
            if tenant is None:
                raise DrillError("no_pilot_tenant")
        report = await drill(pool, S3Store(), tenant, args.backup)
    except Exception as exc:  # noqa: BLE001 — every error is reported by code, class and SQLSTATE only
        print(json.dumps(failure(exc)))
        return 1
    finally:
        if pool is not None:
            await pool.close()
    print(json.dumps(report, sort_keys=True, default=str))
    return exit_code(report)


def cli(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
