"""AA-825 — count and delete expired rows of one retention part (data standards C5.4c).

Deletes run in batches (<= BATCH_SIZE_MAX rows, one transaction each, `statement_timeout` 30 s),
selected by keyset on the indexed (retention column, id) pair, never by `id` alone (random uuids),
with a short pause between batches. For an archived part each batch is written to S3 first; the
DELETE runs in the same transaction as the row lock and must remove exactly the archived rows, or
the transaction rolls back. Never VACUUM FULL: autovacuum reclaims the space (migration 233 tunes
it for these ledgers).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import structlog

from services.retention.policy import Part, TableSpec

logger = structlog.get_logger()

BATCH_SIZE_MAX = 5_000
BATCH_STATEMENT_TIMEOUT = "30s"
OBJECT_KEYS_KEPT = 100           # keys listed in the job result per part (the count is always exact)


class CountMismatch(Exception):
    """The DELETE did not remove exactly the rows that were locked (and archived)."""


@dataclass
class PartResult:
    part: str
    window_days: int
    archive: bool
    cutoff: str
    candidates: int = 0
    oldest: Optional[str] = None
    newest: Optional[str] = None
    deleted: int = 0
    archived_rows: int = 0
    batches: int = 0
    objects: int = 0
    object_keys: list[str] = field(default_factory=list)
    cursor: Optional[dict] = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def cutoff(conn, window_days: int) -> datetime:
    """UTC midnight `window_days` days before today (UTC): every expired day is a whole day."""
    return await conn.fetchval(
        "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => $1)) AT TIME ZONE 'UTC'",
        window_days)


_FK_REFS_SQL = """
    SELECT format('%I.%I', n.nspname, cl.relname) AS ref_table, a.attname AS ref_col
      FROM pg_constraint c
      JOIN pg_class cl ON cl.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = cl.relnamespace
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
     WHERE c.contype = 'f' AND c.confrelid = $1::regclass AND array_length(c.conkey, 1) = 1
     ORDER BY 1, 2
"""


async def fk_refs(conn, table: str) -> list[tuple[str, str]]:
    """Single-column foreign keys pointing at `table` (rows they reference are never deleted)."""
    return [(r["ref_table"], r["ref_col"]) for r in await conn.fetch(_FK_REFS_SQL, table)]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def predicate(spec: TableSpec, part: Part, refs: list[tuple[str, str]]) -> str:
    """WHERE clause on alias `t` with $1 = cutoff."""
    clauses = [f"t.{spec.ts_col} < $1"]
    if part.where:
        clauses.append(f"({part.where})")
    for i, (ref_table, ref_col) in enumerate(refs):
        clauses.append(f"NOT EXISTS (SELECT 1 FROM {ref_table} r{i} WHERE r{i}.{_quote_ident(ref_col)} = t.id)")
    return " AND ".join(clauses)


def count_sql(spec: TableSpec, part: Part, refs) -> str:
    return (f"SELECT count(*)::bigint AS n, min(t.{spec.ts_col}) AS oldest, max(t.{spec.ts_col}) AS newest "
            f"FROM {spec.table} t WHERE {predicate(spec, part, refs)}")


def batch_sql(spec: TableSpec, part: Part, refs, *, after: bool) -> str:
    """$1 cutoff, $2 limit, [$3 last ts, $4 last id]. Locks the rows it returns."""
    doc = ", to_jsonb(t)::text AS doc" if part.archive else ""
    keyset = f" AND (t.{spec.ts_col}, t.id) > ($3, $4::{spec.id_type})" if after else ""
    return (f"SELECT t.{spec.ts_col} AS ts, t.id AS id{doc} FROM {spec.table} t "
            f"WHERE {predicate(spec, part, refs)}{keyset} "
            f"ORDER BY t.{spec.ts_col}, t.id LIMIT $2 FOR UPDATE OF t")


def delete_sql(spec: TableSpec) -> str:
    return f"DELETE FROM {spec.table} WHERE id = ANY($1::{spec.id_type}[])"


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def same_month_prefix(rows: list) -> list:
    """The leading rows that share the first row's UTC month (one archive object = one month)."""
    if not rows:
        return rows
    first = rows[0]["ts"].astimezone(timezone.utc)
    key = (first.year, first.month)
    out = []
    for r in rows:
        ts = r["ts"].astimezone(timezone.utc)
        if (ts.year, ts.month) != key:
            break
        out.append(r)
    return out


async def count_part(conn, spec: TableSpec, part: Part, refs, cut: datetime) -> PartResult:
    row = await conn.fetchrow(count_sql(spec, part, refs), cut)
    return PartResult(part=part.name, window_days=part.window_days, archive=part.archive, cutoff=_iso(cut),
                      candidates=int(row["n"]), oldest=_iso(row["oldest"]), newest=_iso(row["newest"]))


ProgressFn = Callable[[PartResult], Awaitable[None]]


async def delete_part(conn, spec: TableSpec, part: Part, refs, cut: datetime, res: PartResult, *,
                      limit_rows: int, batch_size: int = BATCH_SIZE_MAX, archiver=None,
                      cursor: Optional[tuple] = None, pause_s: float = 0.2,
                      on_batch: Optional[ProgressFn] = None) -> PartResult:
    """Deletes up to `limit_rows` candidates of one part, oldest first, into `res`.

    `cursor` = (last ts, last id) of an earlier attempt of the same job, to resume after it."""
    if part.archive and archiver is None:
        raise ValueError(f"{spec.kind}/{part.name} archives before delete but no archiver was given")
    batch_size = max(1, min(int(batch_size), BATCH_SIZE_MAX))
    first_sql = batch_sql(spec, part, refs, after=False)
    next_sql = batch_sql(spec, part, refs, after=True)
    del_sql = delete_sql(spec)
    while res.deleted < limit_rows:
        n = min(batch_size, limit_rows - res.deleted)
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = '{BATCH_STATEMENT_TIMEOUT}'")
            if cursor is None:
                rows = await conn.fetch(first_sql, cut, n)
            else:
                rows = await conn.fetch(next_sql, cut, n, cursor[0], cursor[1])
            if not rows:
                break
            if part.archive:
                rows = same_month_prefix(rows)
                key = await archiver.put(spec.archive_name, part.name, res.batches + 1, rows[0]["ts"],
                                         [r["doc"] for r in rows])
            status = await conn.execute(del_sql, [r["id"] for r in rows])
            deleted = int(status.split()[-1])
            if deleted != len(rows):
                raise CountMismatch(f"{spec.table}: locked {len(rows)} rows, DELETE removed {deleted}")
        # committed
        res.batches += 1
        res.deleted += deleted
        if part.archive:
            res.archived_rows += deleted
            res.objects += 1
            if len(res.object_keys) < OBJECT_KEYS_KEPT:
                res.object_keys.append(key)
        cursor = (rows[-1]["ts"], rows[-1]["id"])
        res.cursor = {"ts": _iso(cursor[0]), "id": str(cursor[1])}
        if on_batch is not None:
            await on_batch(res)
        if pause_s:
            await asyncio.sleep(pause_s)
    logger.info("retention_part_done", kind=spec.kind, part=part.name, deleted=res.deleted,
                batches=res.batches, objects=res.objects)
    return res
