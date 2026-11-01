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

import dataclasses
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

from . import data_scope, route_evidence

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
                        resource_id: str, before: Any, after: Any, evidence: Optional[dict] = None) -> None:
    details = {"before": before, "after": after}
    if evidence:                      # AA-1026 D7: the eval run / break-glass reason of a route change
        details["evidence"] = evidence
    await conn.execute(
        _AUDIT_INSERT_SQL, actor, action, resource_type, str(resource_id),
        json.dumps(details, default=str),
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
    # AA-1002 (S5, migration 311) — the data scopes this stage's content may be sent through (data_scope.py). None =
    # no stage policy (column NULL / not migrated, no row): the gateway keeps its pre-S5 rules.
    allowed_scopes: Optional[tuple] = None
    # AA-1018 part 2 (migration 320) — the wall-time budget of the whole route in seconds (every model and deployment
    # it tries, fallbacks included). None = no deadline (every row today, column NULL / not migrated): no change.
    route_deadline_s: Optional[int] = None
    # AA-642 A1 (migration 326): the stage kill switch (llm_role_config.is_active; False = every call refused before any
    # provider attempt), the admin's reason, and the declared degrade mode (ADR 0009 decision 5). A stage with no row
    # (SAFE_DEFAULTS) is on.
    enabled: bool = True
    disabled_reason: Optional[str] = None
    degrade_mode: Optional[str] = None


# S235 (Nghiệp, 14/10/2026): Bedrock first; the OpenAI platform API is only the LAST-RESORT fallback for
# a Bedrock outage — never a primary or a shadow (tests/unit/test_s235_openai_last_resort.py). The judge
# default mirrors the live judge route after migration 224: GPT-5.6 Luna (Bedrock acc3) -> GPT-6 Luna
# (Bedrock acc3) -> GPT-6 Luna (OpenAI API). All three are non-Anthropic, so vendor separation holds.
# GPT-4.1 is retired (AA-714 kept it last so a judge could run with the DB unreachable: it was a legacy
# key that needed no catalog row). Now a cold process that cannot read the catalog skips every key
# here as "not in catalog" and the judge call raises.
_JUDGE_ROUTE_DEFAULT = ("gpt-6-luna", "gpt-6-luna-openai")
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
    # AA-763 (G4): T-series twins of the shared-graph stages — same routes as their A1 twin,
    # own keys so T cost / routing / shadow can be read and set apart from A1. See migration 219.
    "t2_judge":           StageConfig("t2_judge", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    "t2_brand_audit":     StageConfig("t2_brand_audit", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    "t2_flag_fix":        StageConfig("t2_flag_fix", "writer", "claude", "haiku", "acc3"),
    "t2_itinerary_nudge": StageConfig("t2_itinerary_nudge", "writer", "claude", "haiku", "acc3"),
    "t7_debate_judge":    StageConfig("t7_debate_judge", "judge", "openai", "gpt-5.6-luna", "acc3",
                                      fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
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
    # AA-772 place cards. S234 (migration 222, A/B n=30): draft on GPT-5.6 Luna, fact check on Claude Haiku
    # — both `validate` stages (ADR 0008), draft and check on different vendors. AA-784 (S252, migration 286,
    # web-sourced blind eval): draft on GPT-6 Luna (0/97 wrong kept facts vs 4.0% live), fallback GPT-5.6 Luna —
    # an OpenAI-vendor fallback, so a fallback draft is still checked by another vendor (222's Sonnet was not).
    "a3_place_card":      StageConfig("a3_place_card", "validate", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("gpt-5.6-luna",)),
    "a3_place_card_check": StageConfig("a3_place_card_check", "validate", "claude", "haiku", "acc3",
                                       fallback_model_ids=("sonnet",)),
    # S234 (migration 221): t9_repair — one cheap Claude stage for T9 sentence repair, SEO fit and
    # the admin sentence suggest (plan C2/C12); t10_claims — F9 v2 claim extraction, a validate
    # stage on GPT-6 Luna (ADR 0008 pattern), not wired into the gates until its replay passes.
    "t9_repair":          StageConfig("t9_repair", "writer", "claude", "haiku", "acc3",
                                      fallback_model_ids=("sonnet",)),
    "t10_claims":         StageConfig("t10_claims", "validate", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("gpt-5.6-luna",)),
    # AA-1000 (migration 309): the T10 tier-2 adjudicator (services/acp_content_writing/seed_align/adjudicate.py,
    # env T10_ADJ_MODE, default off). It scores the writer's sentences → `judge` (ADR 0008), non-Anthropic; on the
    # cheapest catalog model (GPT-6 Luna, acc3), GPT-5.6 Luna next, the OpenAI API only as the last resort (S235).
    "t10_adjudicate":     StageConfig("t10_adjudicate", "judge", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("gpt-5.6-luna", "gpt-6-luna-openai")),
    # AA-779 (S247, migration 264, ADR 0011): the optional LLM filter of the blog FAQ's real search questions
    # (flag T9_FAQ_LLM_FILTER, default off). A classification nobody scores → `validate` (ADR 0008), on the
    # Haiku the S247 trial measured; no model fallback (on failure the code keeps the deterministic list).
    "t9_faq_filter":      StageConfig("t9_faq_filter", "validate", "claude", "haiku", "acc3"),
    # AA-779 (S247, migration 266, ADR 0011): the optional check that each final blog FAQ answer answers its
    # question (same flag). Drop-only, never a hold → `validate` on the same Haiku; no model fallback (on failure
    # the FAQ is kept unchanged).
    "t9_faq_answer_fit":  StageConfig("t9_faq_answer_fit", "validate", "claude", "sonnet", "acc3"),
    # AA-753 (10/10/2026) — the n7_* stages (n7_draft/adapt/faq/repair/gap_research/judge) were the
    # removed N7 produce pipeline (services/acp_produce/gates.py, 0 live callers). Dropped from
    # SAFE_DEFAULTS here and deleted from shared.llm_role_config by migration 207.
    # AA-685 (migration 172) — call sites that bypassed the gateway before.
    "a0_column_map":      StageConfig("a0_column_map", "writer", "claude", "haiku", "acc3"),
    "f10_embed":          StageConfig("f10_embed", "embed", "cohere", "cohere-embed-v4", None),
    # AA-712 (migration 226) — the place pipeline moved from TripPlanner (tp_extract /
    # tp_component_embed) into CIS jobs; extraction of reference data → `validate` (ADR 0008). Embed:
    # Cohere v4 1536-d (= tp_search_embed). Day skeleton: Haiku 4.5 with a Sonnet fallback since migration 260
    # (AA-847, S246 place model trial); 226 seeded Sonnet with a 20% GPT-5.6 Luna shadow. Locate: back on Sonnet
    # with a Haiku fallback since migration 261 (S247 Mongolia build on Haiku: 84% of names left 'llm').
    "a3_place_days":      StageConfig("a3_place_days", "validate", "claude", "haiku", "acc3",
                                      fallback_model_ids=("sonnet",)),
    "a3_place_locate":    StageConfig("a3_place_locate", "validate", "claude", "sonnet", "acc3",
                                      fallback_model_ids=("haiku",)),
    "a3_place_embed":     StageConfig("a3_place_embed", "embed", "cohere", "cohere-embed-v4", None),
    # AA-847 Problem 2 (migration 257): the two-model check of the duplicate-place merge. Primary GPT-6 Luna (won
    # the S234 identity A/B, ~1/10 of Haiku), asked A/B and B/A; check Claude Haiku — another vendor. Both
    # `validate` (ADR 0008: nobody scores them). OpenAI platform only as the primary's last-resort fallback (S235).
    "a3_place_dedupe":    StageConfig("a3_place_dedupe", "validate", "openai", "gpt-6-luna", "acc3",
                                      fallback_model_ids=("gpt-6-luna-openai",)),
    "a3_place_dedupe_check": StageConfig("a3_place_dedupe_check", "validate", "claude", "haiku", "acc3"),
    # AA-835 (migration 278): the customer description / highlights of a Booking hotel or service, reviewed by a
    # human before Booking shows it. Customer-facing text → `writer`, Sonnet; no shadow. S251 (migration 282): the
    # structured-output call moves to the catalog key sonnet-5-5 (Converse: the schema is enforced) — the legacy
    # 'sonnet' (Messages API) stays last so the stage still drafts with the catalog unreadable (lesson S222 #617).
    "supply_content_writer": StageConfig("supply_content_writer", "writer", "claude", "sonnet-5-5", "acc3",
                                         fallback_model_ids=("sonnet",)),
    # AA-917 (migration 280, app ADR 0010 decision 5): the writing-memory rule drafter, a judge stage on the judge
    # route (writers stay Anthropic, rules are drafted by another vendor); WM-13 may reorder it after its check.
    "writing_memory_mine": StageConfig("writing_memory_mine", "judge", "openai", "gpt-5.6-luna", "acc3",
                                       fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    # AA-919 WM-19 (migration 291): the writing-memory eval judge of the R2 runner
    # (services/writing_memory/eval_judge.py), on the judge route (another vendor than the Anthropic writers it scores,
    # ADR 0010 decision 5); no shadow (cost-cuts K2: an eval stage is seeded with shadow_sample_pct = 0).
    "writing_memory_eval": StageConfig("writing_memory_eval", "judge", "openai", "gpt-5.6-luna", "acc3",
                                       fallback_model_ids=_JUDGE_ROUTE_DEFAULT),
    # AA-642 A2 (migration 335): the demand-research loop (services/acp_contract/segment_research.py, one keyword step
    # per call) and the batch proposer (segment_research_batch.py). Both were model_tier="haiku" requests with no
    # stage; the rows keep that very route (the legacy key: the same calls), writer (they phrase keywords), and the
    # declared degrade mode queue_retry (the job is re-queued, tech lead T-21's CIS mode).
    "segment_research":       StageConfig("segment_research", "writer", "claude", "haiku", "acc3",
                                          degrade_mode="queue_retry"),
    "segment_research_batch": StageConfig("segment_research_batch", "writer", "claude", "haiku", "acc3",
                                          degrade_mode="queue_retry"),
}

# AA-1002 (S5): migration 311's backfill for these rows — the stages whose route names a model reached only outside
# AWS (gpt-6-luna-openai, the S235 last resort) may use vendor_direct connections; every other stage is aws_only
# (tests/unit/test_aa1002_s5_data_scope.py derives this set from the routes above and the catalog seed rows).
_VENDOR_DIRECT_STAGES = frozenset({
    "s1_judge", "s1_brand_audit", "t2_judge", "t2_brand_audit", "t7_debate_judge", "t10_judge", "t10_adjudicate",
    "a3_place_dedupe", "writing_memory_mine", "writing_memory_eval",
})
# AA-642 A3 (migration 334, root ADR 0009 decision 5 "one deadline per route"): the route deadline of every stage, by
# role. The first attempt always runs; no later attempt starts once the deadline has passed. Each value is above the
# longest single attempt of the role (Bedrock read timeout 300 s; an acc2 embedding 30 s), so a timed-out first
# attempt still leaves room for a fallback, and caps a whole chain (at most errors.MAX_CHAIN_ATTEMPTS attempts) well
# below the 6 x 300 s it could take before.
ROUTE_DEADLINE_S = {"writer": 1200, "judge": 900, "validate": 600, "embed": 120}
for _stage, _cfg in list(SAFE_DEFAULTS.items()):
    SAFE_DEFAULTS[_stage] = dataclasses.replace(
        _cfg, allowed_scopes=data_scope.ALL_SCOPES if _stage in _VENDOR_DIRECT_STAGES else data_scope.AWS_ONLY_SCOPES,
        route_deadline_s=ROUTE_DEADLINE_S.get(_cfg.role))
del _stage, _cfg

# A stage with no row and no SAFE_DEFAULTS entry (an adhoc_* script, a connection test): no stage policy (None).
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
        allowed_scopes=data_scope.normalize(_scopes_of(row)),
        route_deadline_s=_deadline_of(row),
        enabled=_column(row, "is_active", True) is not False,
        disabled_reason=_column(row, "disabled_reason", None),
        degrade_mode=_column(row, "degrade_mode", None),
    )


def _column(row, name: str, default):
    """An optional column of a row (missing before its migration, or not selected): ``default``."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _deadline_of(row) -> Optional[int]:
    try:
        value = row["route_deadline_s"]
    except (KeyError, IndexError):
        return None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


_ROUTE_COLUMNS = "fallback_model_ids, shadow_model_id, shadow_sample_pct"

# AA-1002 (S5): migration 311 adds allowed_scopes. Until it is applied the column is missing: the reads retry without
# it (no stage policy = the pre-S5 behaviour) and try it again after _SCOPE_RETRY_SECONDS, so the code may ship
# before or after the migration and a process picks the column up without a restart.
_SCOPE_RETRY_SECONDS = 300.0
_scope_column_missing_at = 0.0


def _scopes_of(row) -> Optional[list]:
    try:
        return row["allowed_scopes"]
    except (KeyError, IndexError):
        return None


# AA-1018 part 2: migration 320 adds route_deadline_s, read the same way (missing column = no deadline, retried later).
_deadline_column_missing_at = 0.0
# AA-642 A1: migration 326 adds degrade_mode / disabled_reason, read the same way (missing = none declared, no reason).
_SWITCH_COLUMNS = ", degrade_mode, disabled_reason"
_switch_column_missing_at = 0.0


def _scope_select() -> str:
    """", allowed_scopes[, route_deadline_s[, degrade_mode, disabled_reason]]" for the optional columns believed to
    exist, else ""."""
    now = time.monotonic()
    if _scope_column_missing_at and now - _scope_column_missing_at < _SCOPE_RETRY_SECONDS:
        return ""
    if _deadline_column_missing_at and now - _deadline_column_missing_at < _SCOPE_RETRY_SECONDS:
        return ", allowed_scopes"
    if _switch_column_missing_at and now - _switch_column_missing_at < _SCOPE_RETRY_SECONDS:
        return ", allowed_scopes, route_deadline_s"
    return ", allowed_scopes, route_deadline_s" + _SWITCH_COLUMNS


async def _fetch_scoped(fetch, sql_for):
    """``fetch(sql_for(optional_cols))``; on a missing optional column (degrade_mode / disabled_reason before migration
    326, route_deadline_s before 320, allowed_scopes before 311), once more without it."""
    global _scope_column_missing_at, _deadline_column_missing_at, _switch_column_missing_at
    for _ in range(4):
        cols = _scope_select()
        try:
            return await fetch(sql_for(cols))
        except asyncpg.exceptions.UndefinedColumnError as e:
            if not cols:
                raise
            if "degrade_mode" in cols and ("degrade_mode" in str(e) or "disabled_reason" in str(e)):
                _switch_column_missing_at = time.monotonic()
                logger.warning("llm_role_config_switch_columns_missing",
                               hint="migration 326 not applied: no degrade modes, no switch reasons")
            elif "route_deadline_s" in cols and "route_deadline_s" in str(e):
                _deadline_column_missing_at = time.monotonic()
                logger.warning("llm_role_config_deadline_column_missing",
                               hint="migration 320 not applied: no route deadlines")
            else:
                _scope_column_missing_at = time.monotonic()
                logger.warning("llm_role_config_scope_column_missing",
                               hint="migration 311 not applied: no stage data scopes")
    return await fetch(sql_for(""))


async def _fetch_one(stage: str) -> Optional[StageConfig]:
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        row = await _fetch_scoped(
            lambda sql: conn.fetchrow(sql, stage),
            lambda cols: (f"SELECT stage, role, provider, model_id, account_route, is_active, {_ROUTE_COLUMNS}{cols} "
                          "FROM shared.llm_role_config WHERE stage = $1"))
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
                            hint="no row — using SAFE_DEFAULTS, check migration 137 seeded")
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
    """Admin UI read — every row, switched-off ones included (AA-642 A1: ``is_active`` is the stage kill switch), with
    ``degrade_mode`` / ``disabled_reason`` (None before migration 326)."""
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        rows = await _fetch_scoped(
            conn.fetch,
            lambda cols: ("SELECT stage, role, provider, model_id, account_route, is_active, updated_at, "
                          f"updated_by, {_ROUTE_COLUMNS}{cols} FROM shared.llm_role_config ORDER BY stage"))
        out = [dict(r) for r in rows]
        for r in out:
            r["fallback_model_ids"] = list(r["fallback_model_ids"] or [])
            scopes = data_scope.normalize(r.get("allowed_scopes"))   # AA-1002 (S5): None before migration 311
            r["allowed_scopes"] = list(scopes) if scopes is not None else None
            r.setdefault("degrade_mode", None)                          # AA-642 A1: None before migration 326
            r.setdefault("disabled_reason", None)
        return out
    finally:
        await conn.close()


async def set_stage_config(
    stage: str, model_id: str, account_route: Optional[str], updated_by: str,
    audit_actor: Optional[str] = None, evidence: Optional[dict] = None,
) -> dict:
    """Admin UI write — role/provider are NOT editable here (they're a property of the call site,
    not a choice; changing role/provider for a stage is a code change, not a config change).
    Raises ValueError if `stage` doesn't already exist (no upsert-a-brand-new-stage from the UI —
    every real stage is seeded by migration 137; a typo'd stage name should fail loud, not create
    a silently-dead config row nothing reads).

    AA-686: when `audit_actor` is given, the change is recorded in acp_shared.audit_log inside the
    same transaction as the UPDATE (action `llm_model_changed`), so a committed model change is
    always paired with its audit row.

    AA-1026 D7: ``evidence`` (route_evidence.require) goes into that audit row; a break-glass change also writes the
    admin bell in the same transaction.
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
                    evidence=evidence,
                )
                await route_evidence.record_break_glass(
                    conn, stage=stage, actor=audit_actor, evidence=evidence,
                    change={"model_id": model_id, "before": dict(before) if before else None})
        out = dict(row)
        out["fallback_model_ids"] = list(out["fallback_model_ids"] or [])
        return out
    finally:
        await conn.close()
        invalidate(stage)


async def set_stage_route(
    stage: str, *, fallback_model_ids: list[str], shadow_model_id: Optional[str],
    shadow_sample_pct: int, updated_by: str, audit_actor: str, evidence: Optional[dict] = None,
) -> dict:
    """AA-686 — set a stage's route (ordered fallback chain + optional shadow). Validation of the
    model keys (vendor rule, duplicate/primary-in-fallback, shadow == primary) belongs to the
    router, which has the catalog; this helper is the persistence + audit boundary. The UPDATE and
    the acp_shared.audit_log row (action `llm_route_changed`) share one transaction, and the stage
    cache is invalidated the same way set_stage_config does so the next LLM call sees the new route.
    AA-1026 D7: ``evidence`` goes into the audit row; a break-glass change writes the admin bell in the transaction.
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
                evidence=evidence,
            )
            await route_evidence.record_break_glass(
                conn, stage=stage, actor=audit_actor, evidence=evidence,
                change={"fallback_model_ids": list(fallback_model_ids), "before": before_d})
        out = dict(row)
        out["fallback_model_ids"] = list(out["fallback_model_ids"] or [])
        return out
    finally:
        await conn.close()
        invalidate(stage)


async def set_stage_enabled(stage: str, *, enabled: bool, reason: Optional[str], updated_by: str,
                            audit_actor: str) -> dict:
    """AA-642 A1 — the stage kill switch (``is_active``; migration 326 gives it that meaning). Switching off stores the
    admin's ``reason`` (``disabled_reason``); switching on clears it. The UPDATE and its acp_shared.audit_log row
    (action ``llm_stage_switched_off`` / ``llm_stage_switched_on``) share one transaction; this process's cache is
    invalidated at once, other processes (the worker) see it within ``_CACHE_TTL_SECONDS``. Raises ValueError for an
    unknown stage."""
    reason = None if enabled else ((reason or "").strip()[:500] or None)
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        async with conn.transaction():
            before = await conn.fetchrow(
                "SELECT is_active, disabled_reason FROM shared.llm_role_config WHERE stage = $1 FOR UPDATE", stage)
            if before is None:
                raise ValueError(f"unknown stage: {stage!r}")
            row = await conn.fetchrow(
                """
                UPDATE shared.llm_role_config
                SET is_active = $2, disabled_reason = $3, updated_at = now(), updated_by = $4
                WHERE stage = $1
                RETURNING stage, is_active, disabled_reason, degrade_mode, updated_at, updated_by
                """,
                stage, enabled, reason, updated_by,
            )
            await _insert_audit(
                conn, actor=audit_actor, action="llm_stage_switched_on" if enabled else "llm_stage_switched_off",
                resource_type="llm_role_config", resource_id=stage, before=dict(before),
                after={"is_active": enabled, "disabled_reason": reason},
            )
        return dict(row)
    finally:
        await conn.close()
        invalidate(stage)
