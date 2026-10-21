"""AA-915: the frozen AA-132 lessons loop (``shared.pipeline_lessons``) — the only reader of that table.

The AA-132 loop appended a "LESSONS FROM PREVIOUS BATCHES" block to the S1 writer's system prompt
from ``shared.pipeline_lessons`` and wrote new rows from flag_fix. It had no eval gate, no
``tenant_id`` and auto-activated every row (``is_active = true``). T2 (tenant rewrites) runs the same
S1 graph, so a working loop would let one tenant's failures steer the global S1 prompt through a
table that cannot tell tenants apart. It is superseded by app ADR 0010 (governed writing memory)
and retired in WM-34 (table drop).

Rules for this module, on purpose:

- Default OFF. Only ``PIPELINE_LESSONS_ENABLED`` in ``1/true/yes/on`` turns the loop back on, and
  the env var is read on every call so a task-definition change (or a script) takes effect without
  a reload. ON exists only as a byte-identical rollback of the old prompt.
- No per-tenant override (``tenant_flags``). Unlike ``S1_STRUCTURED_FACTS`` this flag must never be
  switched per tenant: a tenant switch would reopen the T2 → global-S1 path the freeze closes.
- With the flag OFF no SQL is sent at all, so ``pg_stat_user_tables`` for the table stays flat
  (the WM-01 live check reads idx_scan / seq_scan / n_tup_ins).
- Do NOT "fix" the stage mismatch: the writer side (``flag_fix_node.write_lessons_log``) writes
  ``stage = 'audit'`` while this reader asks for ``S1_rewrite`` / ``S1_editor``. Aligning them would
  revive the ungated loop. The replacement is ADR 0010, not a repair of this one.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping

import structlog

logger = structlog.get_logger()

PIPELINE_LESSONS_ENV = "PIPELINE_LESSONS_ENABLED"

# The batch path's query (the sync path also selected `pattern`, which it never used).
LESSONS_SQL = (
    "SELECT what_to_do, field FROM shared.pipeline_lessons "
    "WHERE stage IN ('S1_rewrite','S1_editor') AND is_active = true ORDER BY id"
)

_LESSONS_HEADER = (
    "\n\n## LESSONS FROM PREVIOUS BATCHES\n"
    "Apply these rules strictly — they come from real batch failures:\n"
)


def _flag_on(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def pipeline_lessons_enabled() -> bool:
    """Whether the frozen AA-132 loop may read or write ``shared.pipeline_lessons``. Default OFF;
    read from the env on every call; deliberately no tenant override (see module docstring)."""
    return _flag_on(os.environ.get(PIPELINE_LESSONS_ENV, ""))


def format_lessons_block(rows: Iterable[Mapping]) -> str:
    """The legacy S1 system-prompt suffix, byte-identical to what both readers built before AA-915.
    Empty string for no rows."""
    lines = [f"- {('[' + r['field'] + '] ') if r['field'] else ''}{r['what_to_do']}" for r in rows]
    if not lines:
        return ""
    return _LESSONS_HEADER + "\n".join(lines)


async def load_s1_lessons_block(conn) -> str:
    """The S1 lessons suffix for the writer system prompt, or ``""``.

    Flag OFF (default): returns ``""`` before any SQL is sent. Flag ON: fetches the active S1 rows
    and formats them; any error is logged and treated as no lessons (non-fatal, as before)."""
    if not pipeline_lessons_enabled():
        return ""
    try:
        return format_lessons_block(await conn.fetch(LESSONS_SQL))
    except Exception as e:  # noqa: BLE001 — lessons were always optional for the writer
        logger.warning("lessons_load_failed", error=str(e))
        return ""
