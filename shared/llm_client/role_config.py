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

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 20.0

# AA-686 — admin LLM-ops changes are audited in acp_shared.audit_log in the SAME transaction as
# the config update (a lost audit row is a silent gap, the same reasoning as
# services/acp_shared/audit_log.py). actor_type is left NULL on purpose: the audit_actor_type
# enum (hitl_reviewer / tenant_admin / tenant_reviewer, migration 021) has no admin value, and
# every pre-existing acp_shared.audit_log admin write (admin.py agency.onboard / agency.offboard)
# already leaves it NULL rather than force-fit a reviewer value.
_AUDIT_INSERT_SQL = """
    INSERT INTO acp_shared.audit_log
        (actor, action, resource_type, resource_id, details)
    VALUES ($1, $2, $3, $4, $5::jsonb)
"""


async def _insert_audit(conn, *, actor: str, action: str, resource_type: str,
                        resource_id: str, before: Any, after: Any) -> None:
    await conn.execute(
        _AUDIT_INSERT_SQL, actor, action, resource_type, str(resource_id),
        json.dumps({"before": before, "after": after}, default=str),
    )


@dataclass(frozen=True)
class StageConfig:
    stage: str
    role: str
    provider: str
    model_id: str
    account_route: Optional[str]
    # AA-659 (migration 170) — route: models tried after model_id, plus an optional shadow.
    fallback_model_ids: tuple = ()
    shadow_model_id: Optional[str] = None
    shadow_sample_pct: int = 0


# AA-714 (S222, Nghiệp): every path prefers Bedrock. The judge defaults mirror the live judge route
# (migration 171: GPT-5.6 Luna -> GPT-6 Luna on Bedrock acc3 -> GPT-6 Luna on the OpenAI API) and end
# with GPT-4.1 — a LEGACY key that needs no catalog row. If the DB (and so the catalog) cannot be read,
# the Luna keys are skipped as "not in catalog" and GPT-4.1 still judges; with a readable catalog the
# call always goes to Bedrock first. (Before AA-714 the defaults were GPT-4.1 alone, OpenAI direct.)
_JUDGE_ROUTE_DEFAULT = ("gpt-6-luna", "gpt-6-luna-openai", "gpt-4.1")
# Matches shared.llm_role_config's own seed data (migration 137) exactly — see that file's
# header for why each value is what it is. This is the fallback when the DB is unreachable OR a
# stage has no row yet (e.g. a new call site shipped before its migration/seed caught up).
SAFE_DEFAULTS: dict[str, StageConfig] = {
    "s1_generate":        StageConfig("s1_generate", "writer", "claude", "haiku", "acc3"),
    # AA-620: dedicated tenant (T2) writer stage — seeded Haiku (identical to s1_generate today),
    # kept separate so admin can move ONLY the T2 rewrite to Sonnet via Settings > LLM Models
    # without touching the A1 admin batch write. See migration 156.
    "t2_generate":        StageConfig("t2_generate", "writer", "claude", "haiku", "acc3"),
    "s1_judge":           StageConfig("s1_judge", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    "s1_brand_audit":     StageConfig("s1_brand_audit", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    "s1_flag_fix":        StageConfig("s1_flag_fix", "writer", "claude", "haiku", "acc3"),
    "s1_itinerary_nudge": StageConfig("s1_itinerary_nudge", "writer", "claude", "haiku", "acc3"),
    # AA-748: extract per-day source facts for the S1 writer (behind S1_STRUCTURED_FACTS). Cheap
    # structured-extraction task, seeded Haiku like the other S1 writer stages. See migration 210.
    # AA-748 / ADR 0008: extraction, not prose — a validate stage, so it may run on GPT-6 Luna
    # (10x cheaper than Haiku); Haiku stays as the fallback.
    "s1_source_facts":    StageConfig("s1_source_facts", "validate", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("haiku",)),
    "s1_atom_writer":     StageConfig("s1_atom_writer", "writer", "claude", "sonnet", "acc3"),
    "t8_angle_gen":       StageConfig("t8_angle_gen", "writer", "claude", "sonnet", "acc3"),
    "t9_write":           StageConfig("t9_write", "writer", "claude", "sonnet", "acc3"),
    "t10_judge":          StageConfig("t10_judge", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    # AA-619: Sonnet->Haiku — A/B proved equal atom quality, ~4x cheaper (see migration 155).
    # AA-757 / ADR 0008: Haiku->GPT-6 Luna as a validate stage (extraction the judge never scores);
    # S224 offline A/B on 477 days: 0 errors, no invented detail, ~6x cheaper. Haiku is the fallback.
    # AA-757 (S224) — stage key renamed t5_atomize -> a3_atomize (atomize is A3 platform only;
    # the old key was a pre-AA-526 name). Historical llm_call_log rows keep `t5_atomize`; any
    # UI/report grouping by stage treats both as the same stage (label "A3 atomize").
    "a3_atomize":         StageConfig("a3_atomize", "validate", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("haiku",)),
    # AA-753 (10/10/2026) — the n7_* stages (n7_draft/adapt/faq/repair/gap_research/judge) were the
    # removed N7 produce pipeline (services/acp_produce/gates.py, 0 live callers). Dropped from
    # SAFE_DEFAULTS here and deleted from shared.llm_role_config by migration 207.
    # AA-685 (migration 172) — call sites that bypassed the gateway before.
    "a0_column_map":      StageConfig("a0_column_map", "writer", "claude", "haiku", "acc3"),
    "f10_embed":          StageConfig("f10_embed", "embed", "cohere", "cohere-embed-v4", None),
}

_GENERIC_FALLBACK = StageConfig("unknown", "writer", "claude", "haiku", "acc3")

# stage -> (StageConfig, fetched_at_monotonic)
_cache: dict[str, tuple[StageConfig, float]] = {}


def _row_to_config(row) -> StageConfig:
    return StageConfig(
        stage=row["stage"], role=row["role"], provider=row["provider"],
        model_id=row["model_id"], account_route=row["account_route"],
        fallback_model_ids=tuple(row["fallback_model_ids"] or ()),
        shadow_model_id=row["shadow_model_id"],
        shadow_sample_pct=row["shadow_sample_pct"] or 0,
    )


_ROUTE_COLUMNS = "fallback_model_ids, shadow_model_id, shadow_sample_pct"


async def _fetch_one(stage: str) -> Optional[StageConfig]:
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        row = await conn.fetchrow(
            f"SELECT stage, role, provider, model_id, account_route, {_ROUTE_COLUMNS} "
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
            "SELECT stage, role, provider, model_id, account_route, is_active, updated_at, "
            f"updated_by, {_ROUTE_COLUMNS} FROM shared.llm_role_config ORDER BY stage",
        )
        out = [dict(r) for r in rows]
        for r in out:
            r["fallback_model_ids"] = list(r["fallback_model_ids"] or [])
        return out
    finally:
        await conn.close()


async def set_stage_config(
    stage: str, model_id: str, account_route: Optional[str], updated_by: str,
    audit_actor: Optional[str] = None,
) -> dict:
    """Admin UI write — role/provider are NOT editable here (they're a property of the call site,
    not a choice; changing role/provider for a stage is a code change, not a config change).
    Raises ValueError if `stage` doesn't already exist (no upsert-a-brand-new-stage from the UI —
    every real stage is seeded by migration 137; a typo'd stage name should fail loud, not create
    a silently-dead config row nothing reads).

    AA-686: when `audit_actor` is given, the change is recorded in acp_shared.audit_log inside the
    same transaction as the UPDATE (action `llm_model_changed`), so a committed model change is
    always paired with its audit row.
    """
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT model_id, account_route FROM shared.llm_role_config WHERE stage = $1",
                stage,
            )
            row = await conn.fetchrow(
                """
                UPDATE shared.llm_role_config
                SET model_id = $2, account_route = $3, updated_at = now(), updated_by = $4
                WHERE stage = $1
                RETURNING stage, role, provider, model_id, account_route, is_active, updated_at,
                          updated_by, fallback_model_ids, shadow_model_id, shadow_sample_pct
                """,
                stage, model_id, account_route, updated_by,
            )
            if row is None:
                raise ValueError(f"unknown stage: {stage!r}")
            if audit_actor is not None:
                await _insert_audit(
                    conn, actor=audit_actor, action="llm_model_changed",
                    resource_type="llm_role_config", resource_id=stage,
                    before=dict(before) if before else None,
                    after={"model_id": model_id, "account_route": account_route},
                )
        out = dict(row)
        out["fallback_model_ids"] = list(out["fallback_model_ids"] or [])
        return out
    finally:
        await conn.close()
        invalidate(stage)


async def set_stage_route(
    stage: str, *, fallback_model_ids: list[str], shadow_model_id: Optional[str],
    shadow_sample_pct: int, updated_by: str, audit_actor: str,
) -> dict:
    """AA-686 — set a stage's route (ordered fallback chain + optional shadow). Validation of the
    model keys (vendor rule, duplicate/primary-in-fallback, shadow == primary) belongs to the
    router, which has the catalog; this helper is the persistence + audit boundary. The UPDATE and
    the acp_shared.audit_log row (action `llm_route_changed`) share one transaction, and the stage
    cache is invalidated the same way set_stage_config does so the next LLM call sees the new route.
    """
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        async with conn.transaction():
            before = await conn.fetchrow(
                f"SELECT model_id, {_ROUTE_COLUMNS} FROM shared.llm_role_config WHERE stage = $1",
                stage,
            )
            row = await conn.fetchrow(
                """
                UPDATE shared.llm_role_config
                SET fallback_model_ids = $2, shadow_model_id = $3, shadow_sample_pct = $4,
                    updated_at = now(), updated_by = $5
                WHERE stage = $1
                RETURNING stage, role, provider, model_id, account_route, is_active, updated_at,
                          updated_by, fallback_model_ids, shadow_model_id, shadow_sample_pct
                """,
                stage, list(fallback_model_ids), shadow_model_id, shadow_sample_pct, updated_by,
            )
            if row is None:
                raise ValueError(f"unknown stage: {stage!r}")
            before_d = dict(before) if before else None
            if before_d and before_d.get("fallback_model_ids") is not None:
                before_d["fallback_model_ids"] = list(before_d["fallback_model_ids"])
            await _insert_audit(
                conn, actor=audit_actor, action="llm_route_changed",
                resource_type="llm_role_config", resource_id=stage,
                before=before_d,
                after={"fallback_model_ids": list(fallback_model_ids),
                       "shadow_model_id": shadow_model_id, "shadow_sample_pct": shadow_sample_pct},
            )
        out = dict(row)
        out["fallback_model_ids"] = list(out["fallback_model_ids"] or [])
        return out
    finally:
        await conn.close()
        invalidate(stage)
