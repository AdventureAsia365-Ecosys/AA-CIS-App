"""AA-518 Việc C — per-stage, admin-only LLM model config (shared.llm_role_config).

Read path used by every one of the 16 real call sites (see docs/implementation-notes/AA-518.md)
instead of a hardcoded model/account literal. Short in-process cache (`_CACHE_TTL_SECONDS`) so a
hot call site doesn't hit Postgres on every single LLM call — `invalidate()` is called by the
admin PATCH endpoint right after a successful write so a model change takes effect on this SAME
process's very next LLM call, not after the TTL expires. Cross-process invalidation (Redis
pub/sub, SNS, ...) is deliberately NOT built — this app runs desired_count=1 (see CLAUDE.md), so
there is only ever one process to invalidate; add a broadcast mechanism if that ever changes.

Never raises. On any DB error (or the table not being reachable at all) every function falls back
to SAFE_DEFAULTS, which is hand-kept in sync with what the code did before this task shipped —
a bad DB read must never be the reason a writer/judge call fails.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 20.0


@dataclass(frozen=True)
class StageConfig:
    stage: str
    role: str
    provider: str
    model_id: str
    account_route: Optional[str]


# Matches shared.llm_role_config's own seed data (migration 137) exactly — see that file's
# header for why each value is what it is. This is the fallback when the DB is unreachable OR a
# stage has no row yet (e.g. a new call site shipped before its migration/seed caught up).
SAFE_DEFAULTS: dict[str, StageConfig] = {
    "s1_generate":        StageConfig("s1_generate", "writer", "claude", "haiku", "acc3"),
    # AA-620: dedicated tenant (T2) writer stage — seeded Haiku (identical to s1_generate today),
    # kept separate so admin can move ONLY the T2 rewrite to Sonnet via Settings > LLM Models
    # without touching the A1 admin batch write. See migration 156.
    "t2_generate":        StageConfig("t2_generate", "writer", "claude", "haiku", "acc3"),
    "s1_judge":           StageConfig("s1_judge", "judge", "openai", "gpt-4.1", None),
    "s1_brand_audit":     StageConfig("s1_brand_audit", "judge", "openai", "gpt-4.1", None),
    "s1_flag_fix":        StageConfig("s1_flag_fix", "writer", "claude", "haiku", "acc3"),
    "s1_itinerary_nudge": StageConfig("s1_itinerary_nudge", "writer", "claude", "haiku", "acc3"),
    "s1_atom_writer":     StageConfig("s1_atom_writer", "writer", "claude", "sonnet", "acc3"),
    "t8_angle_gen":       StageConfig("t8_angle_gen", "writer", "claude", "sonnet", "acc3"),
    "t9_write":           StageConfig("t9_write", "writer", "claude", "sonnet", "acc3"),
    "t10_judge":          StageConfig("t10_judge", "judge", "openai", "gpt-4.1", None),
    # AA-619: Sonnet->Haiku — A/B proved equal atom quality, ~4x cheaper (see migration 155).
    "t5_atomize":         StageConfig("t5_atomize", "writer", "claude", "haiku", "acc3"),
    "n7_draft":           StageConfig("n7_draft", "writer", "claude", "sonnet", "acc3"),
    "n7_adapt":           StageConfig("n7_adapt", "writer", "claude", "sonnet", "acc3"),
    "n7_faq":             StageConfig("n7_faq", "writer", "claude", "sonnet", "acc3"),
    "n7_repair":          StageConfig("n7_repair", "writer", "claude", "sonnet", "acc3"),
    "n7_gap_research":    StageConfig("n7_gap_research", "validate", "claude", "haiku", "acc3"),
    "n7_judge":           StageConfig("n7_judge", "judge", "openai", "gpt-4.1", None),
}

_GENERIC_FALLBACK = StageConfig("unknown", "writer", "claude", "haiku", "acc3")

# stage -> (StageConfig, fetched_at_monotonic)
_cache: dict[str, tuple[StageConfig, float]] = {}


def _row_to_config(row) -> StageConfig:
    return StageConfig(
        stage=row["stage"], role=row["role"], provider=row["provider"],
        model_id=row["model_id"], account_route=row["account_route"],
    )


async def _fetch_one(stage: str) -> Optional[StageConfig]:
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        row = await conn.fetchrow(
            "SELECT stage, role, provider, model_id, account_route "
            "FROM shared.llm_role_config WHERE stage = $1 AND is_active",
            stage,
        )
        return _row_to_config(row) if row else None
    finally:
        await conn.close()


async def get_stage_config(stage: str) -> StageConfig:
    """Async path — cache-first, DB on a cold/stale cache, SAFE_DEFAULTS (or the stale cache
    entry, if there is one) on any failure."""
    cached = _cache.get(stage)
    now = time.monotonic()
    if cached and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        cfg = await _fetch_one(stage)
        if cfg is None:
            cfg = SAFE_DEFAULTS.get(stage, _GENERIC_FALLBACK)
            logger.warning("llm_role_config_stage_missing", stage=stage,
                            hint="no active row — using SAFE_DEFAULTS, check migration 137 seeded")
        _cache[stage] = (cfg, now)
        return cfg
    except Exception as e:
        logger.warning("llm_role_config_read_failed", stage=stage, error=str(e))
        if cached:
            return cached[0]  # stale-but-real beats a hardcoded default when the DB hiccups
        return SAFE_DEFAULTS.get(stage, _GENERIC_FALLBACK)


def get_stage_config_sync(stage: str) -> StageConfig:
    """Sync wrapper for the many call sites that are plain `def`, not `async def`. Cache-hit
    path never touches asyncio at all — the common case (20s TTL).

    Most callers (AA-416's "wrap at the async/sync boundary" convention — S1's graph nodes, all
    5 N7 writer/judge functions, T9's write/T10-gate functions) already run inside
    `asyncio.to_thread()` from their async caller, so a fresh `asyncio.run()` is safe: no event
    loop is already running on that worker thread.

    One real, pre-existing exception found while wiring this up: `s1_from_atom.py::
    _call_claude_satellite()` is a plain `def` called DIRECTLY from its async caller, without
    `to_thread()` — a genuine gap in that file (it already blocks the event loop with a
    synchronous boto3 call, self-documented, not fixed by this task). `asyncio.run()` would
    raise "cannot be called from a running event loop" there. Detected via
    `asyncio.get_running_loop()` and handled by running the fetch in a throwaway worker thread
    (its own fresh loop) instead of either crashing or silently degrading to SAFE_DEFAULTS on
    every single call from that one call site."""
    cached = _cache.get(stage)
    if cached and time.monotonic() - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    import asyncio
    try:
        has_running_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            has_running_loop = False
        if has_running_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(lambda: asyncio.run(get_stage_config(stage))).result(timeout=5)
        return asyncio.run(get_stage_config(stage))
    except Exception as e:
        logger.warning("llm_role_config_sync_read_failed", stage=stage, error=str(e))
        return cached[0] if cached else SAFE_DEFAULTS.get(stage, _GENERIC_FALLBACK)


def invalidate(stage: Optional[str] = None) -> None:
    """Called by PATCH /admin/llm-config/{stage} right after a successful write. `stage=None`
    clears everything (used by tests / a full reseed)."""
    if stage is None:
        _cache.clear()
    else:
        _cache.pop(stage, None)


async def list_stage_configs() -> list[StageConfig]:
    """Admin UI read — every row regardless of is_active isn't needed here (inactive rows would
    only exist if a future UI adds soft-delete; migration 137 seeds everything active), so this
    intentionally mirrors get_stage_config()'s own `AND is_active` filter for consistency."""
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        rows = await conn.fetch(
            "SELECT stage, role, provider, model_id, account_route, is_active, updated_at, updated_by "
            "FROM shared.llm_role_config ORDER BY stage",
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def set_stage_config(
    stage: str, model_id: str, account_route: Optional[str], updated_by: str,
) -> dict:
    """Admin UI write — role/provider are NOT editable here (they're a property of the call site,
    not a choice; changing role/provider for a stage is a code change, not a config change).
    Raises ValueError if `stage` doesn't already exist (no upsert-a-brand-new-stage from the UI —
    every real stage is seeded by migration 137; a typo'd stage name should fail loud, not create
    a silently-dead config row nothing reads)."""
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        row = await conn.fetchrow(
            """
            UPDATE shared.llm_role_config
            SET model_id = $2, account_route = $3, updated_at = now(), updated_by = $4
            WHERE stage = $1
            RETURNING stage, role, provider, model_id, account_route, is_active, updated_at, updated_by
            """,
            stage, model_id, account_route, updated_by,
        )
        if row is None:
            raise ValueError(f"unknown stage: {stage!r}")
        return dict(row)
    finally:
        await conn.close()
        invalidate(stage)
