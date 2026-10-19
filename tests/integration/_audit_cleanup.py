"""AA-884 (CIS 248): teardown helper for tests that write acp_shared.audit_log rows.

Migration 248 makes the table append-only: BEFORE UPDATE / DELETE / TRUNCATE triggers raise 42501, so the
plain `DELETE FROM acp_shared.audit_log …` the tests used to clean up after themselves now fails. Test-only
way round it: one transaction with `SET LOCAL session_replication_role = replica`, which skips ordinary
(not ENABLE ALWAYS) triggers. Setting it needs a superuser — the CI postgres service user and the local
`cistest` role both are (tests/integration/conftest.py). Never use this pattern in application code.

Deleting chained rows leaves a gap in chain_seq (the next row's prev_hash names a row that is gone). That is
exactly what the verifier reports as tampering, so a test that checks the chain verifies from its own first
row (`from_seq`), never the whole table of a shared test DB.
"""
from __future__ import annotations

from typing import Any


async def _delete(conn: Any, where_sql: str, args: tuple) -> int:
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        status = await conn.execute(f"DELETE FROM acp_shared.audit_log WHERE {where_sql}", *args)
    return int(status.split()[-1])


async def delete_audit_rows(db: Any, where_sql: str, *args: Any) -> int:
    """DELETE FROM acp_shared.audit_log WHERE <where_sql> with the append-only triggers skipped.

    `db` is an asyncpg Pool or Connection; `where_sql` uses $1… placeholders bound to `args`. Returns the
    number of rows deleted."""
    if hasattr(db, "transaction"):
        return await _delete(db, where_sql, args)
    async with db.acquire() as conn:
        return await _delete(conn, where_sql, args)
