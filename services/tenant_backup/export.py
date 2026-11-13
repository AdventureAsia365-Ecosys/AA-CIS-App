"""AA-1093 part 3 — write one tenant's CIS rows to the gold bucket (the `tenant_backup` job runs it).

Layout under exports/tenants/<tenant>/cis/<backup id>/ (catalogue.object_prefix; backup id = the job id):
  tables/<schema>.<table>.jsonl.gz   one `to_jsonb(row)` per line, gzip (mtime 0), rows the erasure predicate selects
  manifest.json                      format, tenant, backup id, schema version, per table: rows, bytes, gzip bytes,
                                     sha256 of the stored file, columns, primary key; skipped / excluded / unregistered
                                     / missing tables; restore order (parents first)
  SHA256SUMS                         "<sha256>  <path>" per table file and manifest.json; written last, so a backup
                                     without it is unfinished. Its own digest goes into the job result, which the
                                     restore drill checks first.

Every table is read in ONE repeatable-read, read-only transaction (one snapshot: a parent and its children agree).
Postgres writes the JSON (`to_jsonb`), so every type comes back through `jsonb_populate_recordset` in the drill.

Size cap (`max_bytes`, uncompressed JSON): tables are written smallest first; a table that would cross the cap is
dropped whole and listed under `skipped` with its row count (never cut in the middle), and the backup is then
`complete: false`. Same for any `unregistered` table (a tenant key the erasure inventory does not know).

The rows are tenant content (a backup, sealed in the bucket with SSE); the job result and the logs carry ids,
table names, counts, sizes and digests only.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from services.erasure import inventory as inv
from services.erasure import plan as pl
from services.tenant_backup import catalogue as cat
from services.tenant_backup.store import ObjectStore

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
STATEMENT_TIMEOUT = "300s"
FETCH_ROWS = 500

TENANT_SQL = "SELECT tenant_id::text AS tenant_id, kind, is_pilot FROM shared.tenants WHERE tenant_id = $1::uuid"
SCHEMA_VERSION_SQL = "SELECT max(version::int) AS v FROM shared.schema_versions WHERE version ~ '^[0-9]+$'"
COLUMNS_SQL = """
    SELECT a.attname::text AS name FROM pg_catalog.pg_attribute a
     WHERE a.attrelid = $1::text::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
"""
PK_SQL = """
    SELECT a.attname::text AS name
      FROM pg_catalog.pg_index i
      JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
     WHERE i.indrelid = $1::text::regclass AND i.indisprimary
     ORDER BY array_position(i.indkey::int2[], a.attnum)
"""


def max_bytes_from_env(env: Optional[dict] = None) -> int:
    raw = (env if env is not None else os.environ).get("TENANT_BACKUP_MAX_BYTES", "")
    try:
        return max(1, int(raw)) if str(raw).strip() else DEFAULT_MAX_BYTES
    except ValueError:
        return DEFAULT_MAX_BYTES


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sums_text(digests: dict[str, str]) -> str:
    """SHA256SUMS in the sha256sum format, sorted by path."""
    return "".join(f"{d}  {p}\n" for p, d in sorted(digests.items()))


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, sep, path = line.partition("  ")
        if not sep or len(digest) != 64 or not path:
            raise ValueError("SHA256SUMS line shape")
        out[path] = digest
    return out


class _CapReached(Exception):
    pass


async def _write_table(conn, table: cat.BackupTable, tenant_id: str, path: str, budget: int) -> tuple[int, int]:
    """(rows, uncompressed bytes) of one table written to `path` (gzip, mtime 0). Raises _CapReached past `budget`."""
    sql = f"SELECT to_jsonb(t)::text AS j FROM {pl.qname(table.table)} t WHERE {table.where}"  # noqa: S608
    rows = size = 0
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        async for rec in conn.cursor(sql, tenant_id, prefetch=FETCH_ROWS):
            line = (rec["j"] + "\n").encode("utf-8")
            size += len(line)
            if size > budget:
                raise _CapReached()
            gz.write(line)
            rows += 1
    return rows, size


async def run_backup(pool, store: ObjectStore, tenant_id: str, backup_id: str, *, max_bytes: int,
                     progress: Optional[Callable[..., Awaitable[None]]] = None,
                     registry: Optional[inv.Registry] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Export the tenant, upload, return the summary (ids, names, counts, sizes, digests). Raises BackupRefused."""
    tid = cat.check_tenant_id(tenant_id)
    prefix = cat.object_prefix(tid, backup_id)
    created_at = (now or datetime.now(timezone.utc)).isoformat()
    digests: dict[str, str] = {}
    tables_out: list[dict] = []
    skipped: list[dict] = []
    used = 0
    with tempfile.TemporaryDirectory(prefix="tenant-backup-") as work:
        async with pool.acquire() as conn:
            tenant = await conn.fetchrow(TENANT_SQL, tid)
            if tenant is None:
                raise cat.BackupRefused("no_tenant", "no shared.tenants row with this id")
            catalog = await pl.load_catalog(conn)
            bp = cat.backup_plan(pl.plan_tenant(registry or inv.Registry(), catalog))
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                schema_version = await conn.fetchval(SCHEMA_VERSION_SQL)
                counts = {t.table: int(await conn.fetchval(
                    f"SELECT count(*) FROM {pl.qname(t.table)} t WHERE {t.where}", tid))  # noqa: S608
                    for t in bp.tables}
                if progress:
                    await progress(phase="exporting", tables=len(bp.tables), rows=sum(counts.values()))
                # smallest first, so one large table cannot push the small ones past the cap
                for t in sorted(bp.tables, key=lambda x: (counts[x.table], x.table)):
                    rel = cat.table_file(t.table)
                    path = os.path.join(work, rel.replace("/", "_"))
                    try:
                        rows, size = await _write_table(conn, t, tid, path, max_bytes - used)
                    except _CapReached:
                        os.remove(path)
                        skipped.append({"table": t.table, "rows": counts[t.table], "reason": "size_cap"})
                        continue
                    used += size
                    digest = sha256_file(path)
                    gz_bytes = os.path.getsize(path)
                    await store.put_file(prefix + rel, path, "application/gzip")
                    os.remove(path)
                    digests[rel] = digest
                    columns = [r["name"] for r in await conn.fetch(COLUMNS_SQL, pl.qname(t.table))]
                    pk = [r["name"] for r in await conn.fetch(PK_SQL, pl.qname(t.table))]
                    tables_out.append({"table": t.table, "action": t.action, "file": rel, "rows": rows,
                                       "bytes": size, "gz_bytes": gz_bytes, "sha256": digest,
                                       "columns": columns, "pk": pk})
        tables_out.sort(key=lambda e: bp.restore_order.index(e["table"]))
        complete = not skipped and not bp.unregistered
        manifest = {
            "format": cat.FORMAT, "tenant_id": tid, "backup_id": backup_id, "created_at": created_at,
            "schema_version": schema_version, "max_bytes": max_bytes, "complete": complete,
            "tables": tables_out, "skipped": skipped, "excluded": bp.excluded,
            "unregistered": bp.unregistered, "missing": bp.missing,
            "restore_order": [e["table"] for e in tables_out],
        }
        mpath = os.path.join(work, "manifest.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, sort_keys=True, indent=1)
        digests["manifest.json"] = sha256_file(mpath)
        await store.put_file(prefix + "manifest.json", mpath, "application/json")
        spath = os.path.join(work, "SHA256SUMS")
        with open(spath, "w", encoding="ascii") as f:
            f.write(sums_text(digests))
        sums_digest = sha256_file(spath)
        await store.put_file(prefix + "SHA256SUMS", spath, "text/plain")
    return {
        "backup_id": backup_id, "tenant_id": tid, "prefix": prefix, "schema_version": schema_version,
        "tables": len(tables_out), "rows": sum(e["rows"] for e in tables_out),
        "bytes": sum(e["bytes"] for e in tables_out), "gz_bytes": sum(e["gz_bytes"] for e in tables_out),
        "max_bytes": max_bytes, "skipped": skipped, "excluded": len(bp.excluded),
        "unregistered": bp.unregistered, "missing": bp.missing, "complete": complete,
        "sha256sums_sha256": sums_digest, "manifest_sha256": digests["manifest.json"],
    }
