"""AA-735 nac 3 — declared stage registry for the platform recompute (ADR 0003 layer B + C).

The platform-wide Segment / Score / Route recompute used to live as two hand-written chains in
`services/export/handler.py` (`recompute_segment_score_route`, tour scope; and
`recompute_rankings_and_routes`, platform scope) plus a third inline copy of segment matching in
`_run_a3_atomize_background`. ADR 0003 layer B asks for a stage registry instead: each stage is a
small module with a pure `run(ctx, scope)` that *declares* the tables it reads, the tables it
writes and how it writes them (layer C), and one orchestrator runs a named list of stages.

This is a STRUCTURAL refactor — same SQL, same order, same progress events, same result shape.
The four stages wrap the existing functions UNCHANGED (`run_segment_matching`,
`precompute_question_landings`, `run_atom_ranking`, `run_route_detection`); none of their SQL is
rewritten here. The write strategies below are *declared from* the real SQL each function runs,
they do not change it. A stage that writes several tables with different strategies declares one
`write_strategy` per table (`table_strategies`), with `write_strategy` naming the one readers
actually depend on:
  - `segment` — mixed, declared per table (`run_segment_matching`'s own SQL):
      * `atom_segment` — UPSERT only (`INSERT ... ON CONFLICT (segment_id) DO UPDATE`); never
        DELETEd, by design (migration 129: `atom_segment_alias.segment_id_old` FKs into it, so an
        id that "gave way" has to keep existing as a row).
      * `atom_segment_member` — DELETE+INSERT for the re-point path: when a bridging merge makes
        one Segment give way, its members are re-pointed to the surviving id as INSERT-SELECT
        (`_REPOINT_INSERT_SQL`, ON CONFLICT DO NOTHING) then DELETE of the losing id's rows
        (`_REPOINT_DELETE_SQL`, segment_matching.py ~line 505); the triggering tour's own new
        members are INSERTed ON CONFLICT DO NOTHING on top.
      * `atom_segment_alias` — UPSERT (`INSERT ... ON CONFLICT (segment_id_old) DO UPDATE`), plus
        a chain-resolve UPDATE and a self-pointing DELETE within the same transaction.
  - `landing` — UPSERT across every table it touches (`precompute_question_landings`):
      * `atom_segment` — UPDATEs the cached `questions_count`/`questions_computed_at` columns
        (migration 163) for the Segments it (re)lands.
      * `atom_matches` — `INSERT ... ON CONFLICT (query, atom_id) DO UPDATE` (atom_ranking.py
        ~line 532, via `land_questions_for_segment`).
      * `atom_embedding` / `question_embedding` — the embedding caches
        (`ensure_atom_embeddings_batch` / `embed_questions_cached`, both ON CONFLICT DO NOTHING).
  - `score` — VERSIONED-SWAP: `atom_ranking` supersedes the market's old current rows and inserts
    the new ones in one transaction, then drops the already-invisible superseded rows (AA-734 /
    nac 2 — no reader dip).
  - `route` — mixed, declared per table (`run_route_detection`):
      * `route` — VERSIONED-SWAP (supersede + insert, AA-532): the strategy readers depend on
        (`superseded_at IS NULL`).
      * `hub` — UPSERT (an UPDATE of `updated_at` on reuse, an INSERT on a new family; never
        superseded or rebuilt — implementation notes Decision 10).

  - `segment_catchup` (AA-1059) — the `segment` stage for the active Masters with matchable atoms and no Segment
    (`services/recompute/segment_catchup.py`), at most SEGMENT_CATCHUP_MAX tours per pass; same tables and strategies
    as `segment` (it calls the same function), plus the Segments it touched handed to `landing`.
  - `segment_gc` (AA-988) — DELETE of zero-reference orphan Segments and of their member rows on dead atoms
    (`services/acp_contract/segment_gc.py`), in `report` mode by default (env SEGMENT_GC_MODE), under the
    segment-write lock the `segment` stage holds shared.

  - `retarget` (AA-1057) — UPDATE in place of `acp_shared.subject.route_id` (a picked / used Route Subject on a
    superseded Route) and `acp_shared.angle_gate_request.atom_id` (a request on a replaced atom, or a Segment
    Subject's request whose seed tour retired), guarded on the old value (`services/acp_shared/retarget.py`; env
    RECOMPUTE_RETARGET_MODE, default `apply` since the audited one-off ran).

The two named lists mirror the two old chains, plus the AA-1059 catch-up at the start of the platform pass, the
AA-1057 retarget after `route` and the AA-988 clean-up at the end of the platform pass:
  - `TOUR_STAGES`     = segment -> landing -> score -> route -> retarget (one tour's publish / atom edit).
  - `PLATFORM_STAGES` = segment_catchup -> landing -> score -> route -> retarget -> segment_gc (AA-1059: first the
    segment stage for the active Masters it missed, then platform score+route, then GC once ranking and routes are
    current).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger()

VALID_WRITE_STRATEGIES = ("upsert", "versioned_swap", "delete_insert")


@dataclass(frozen=True)
class RecomputeScope:
    """What a recompute run is scoped to.

    `tour_id` — the triggering tour (needed by `segment`; the `landing` stage lands PAA questions
    only for this tour's current Segments). None for a from-scratch platform pass.
    `segment_ids` — for a platform-scoped run, the Segments whose PAA landing to recompute (None =
    every Segment, the backfill case). Carried straight into `precompute_question_landings`.
    `log_reason` / `log_tour_id` — threaded into the same log lines the old chains emitted.
    """

    tour_id: Optional[str] = None
    segment_ids: Optional[set[str]] = None
    log_reason: str = ""
    log_tour_id: Optional[str] = None


@dataclass
class RecomputeContext:
    """Shared state the orchestrator hands to every stage.

    `pool` — the asyncpg pool the stages run on (opened by the caller, e.g. `open_job_pool`).
    `progress` — optional sync callback for the Jobs page (same shape the old chains emitted).
    `data` — scratch dict where an earlier stage leaves outputs a later one needs; today only
    `question_counts` (landing -> score, AA-610: landing is market-independent, computed once).
    """

    pool: object
    progress: Optional[Callable[[dict], None]] = None
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Stage:
    """A declared recompute stage (ADR 0003 layer B + C).

    `reads` / `writes` — the tables the wrapped function touches (declared, for the registry).
    `write_strategy` — the strategy readers depend on, one of VALID_WRITE_STRATEGIES.
    `table_strategies` — the strategy PER written table (every value in VALID_WRITE_STRATEGIES),
    for a stage that writes several tables differently (segment, route). It always covers exactly
    `writes`; `write_strategy` is the one of these the stage is named by.
    `run` — `async run(ctx, scope) -> dict`, wrapping the existing function unchanged.
    """

    name: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    write_strategy: str
    run: Callable[[RecomputeContext, RecomputeScope], Awaitable[dict]]
    table_strategies: dict[str, str] = field(default_factory=dict)


# ── stage bodies (thin wrappers over the existing functions — no SQL rewritten) ─────────────

# The tour's own current Segments, exactly as `recompute_segment_score_route` inlined it before
# this refactor (AA-610 scope fix: land PAA questions only for this tour's Segments, read every
# other Segment's count from cache). Kept verbatim so `landing` behaves identically on a tour run.
_THIS_TOUR_SEGMENTS_SQL = """
    SELECT DISTINCT asm.segment_id
    FROM acp_contract.atom_segment_member asm
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = asm.atom_id
    WHERE ta.tour_id = $1::uuid AND NOT ta.deleted AND NOT ta.is_empty_marker
"""


async def _run_segment(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    if not scope.tour_id:
        raise ValueError("segment stage needs scope.tour_id")
    from services.acp_contract.segment_matching import run_segment_matching
    segment_result = await run_segment_matching(scope.tour_id, ctx.pool)
    logger.info("segment_matching_done", tour_id=scope.log_tour_id or scope.tour_id,
                result=segment_result)
    return segment_result


async def _run_landing(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.acp_contract.atom_ranking import precompute_question_landings
    # Tour scope: land only this tour's current Segments (the exact query the old chain inlined).
    # Platform scope: use the Segment set the caller passed (None = every Segment, backfill).
    if scope.tour_id:
        async with ctx.pool.acquire() as conn:
            rows = await conn.fetch(_THIS_TOUR_SEGMENTS_SQL, scope.tour_id)
        segment_ids: Optional[set[str]] = {r["segment_id"] for r in rows}
    else:
        segment_ids = scope.segment_ids
        # AA-1059 — the Segments `segment_catchup` touched this pass are landed too (brand-new ones would be anyway:
        # questions_count IS NULL; an existing one a caught-up atom joined has a new atom to land questions on).
        extra = ctx.data.get("catchup_segment_ids")
        if segment_ids is not None and extra:
            segment_ids = set(segment_ids) | set(extra)
    # AA-688: `progress` reports the embedding pre-pass to the Jobs page. The platform chain never
    # passed one (ctx.progress is None there), so landing stays progress-silent on that path.
    question_counts = await precompute_question_landings(ctx.pool, segment_ids, ctx.progress)
    # Hand the landing counts to `score` — AA-610: computed once, market-independent.
    ctx.data["question_counts"] = question_counts
    return {"segments_landed": None if segment_ids is None else len(segment_ids)}


async def _run_score(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.acp_contract.atom_ranking import run_atom_ranking
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
    question_counts = ctx.data.get("question_counts", {})
    ranking_results = {}
    markets = list(DFS_LOCATION_MAP)
    for i, market_code in enumerate(markets, start=1):
        ranking_results[market_code] = await run_atom_ranking(market_code, ctx.pool, question_counts)
        # AA-687: emit ranking_markets steps so the Jobs page does not sit on the landing step
        # for the ~80 s ranking + route detection take.
        if ctx.progress:
            ctx.progress({"step": "ranking_markets", "done": i, "total": len(markets)})
    # AA-735 — the old tour chain (`recompute_segment_score_route`) logged `ranking_done`; the old
    # platform chain (`recompute_rankings_and_routes`) logged ONLY its own
    # `recompute_rankings_and_routes_done`. Keep the per-tour event on the tour path only (scope
    # has a tour_id) so the platform path stays log-identical to base.
    if scope.tour_id:
        logger.info("ranking_done", tour_id=scope.log_tour_id or scope.tour_id,
                    result=ranking_results)
    return ranking_results


async def _run_route(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.acp_contract.route_detection import run_route_detection
    if ctx.progress:
        ctx.progress({"step": "route_detection", "done": 0, "total": 1})
    route_result = await run_route_detection(ctx.pool)
    if ctx.progress:
        ctx.progress({"step": "route_detection", "done": 1, "total": 1})
    # AA-735 — per-tour `route_detection_done` only on the tour path (see `_run_score`); the
    # platform chain never logged it.
    if scope.tour_id:
        logger.info("route_detection_done", tour_id=scope.log_tour_id or scope.tour_id,
                    result=route_result)
    return route_result


async def _run_segment_catchup(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.recompute.segment_catchup import run_segment_catchup
    if ctx.progress:
        ctx.progress({"step": "segment_catchup", "done": 0, "total": 1})
    result = await run_segment_catchup(ctx.pool)
    ctx.data["catchup_segment_ids"] = set(result.pop("segment_ids", None) or [])
    if ctx.progress:
        ctx.progress({"step": "segment_catchup", "done": 1, "total": 1})
    return result


async def _run_retarget(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.acp_shared.retarget import run_retarget
    return await run_retarget(ctx.pool)


async def _run_segment_gc(ctx: RecomputeContext, scope: RecomputeScope) -> dict:
    from services.acp_contract.segment_gc import run_segment_gc
    if ctx.progress:
        ctx.progress({"step": "segment_gc", "done": 0, "total": 1})
    result = await run_segment_gc(ctx.pool)
    if ctx.progress:
        ctx.progress({"step": "segment_gc", "done": 1, "total": 1})
    return result


# ── registry ────────────────────────────────────────────────────────────────────────────────

_STAGES: dict[str, Stage] = {
    "segment": Stage(
        name="segment",
        reads=("acp_contract.v_active_tour_atoms", "acp_contract.tour_atoms",
               "acp_contract.atom_segment", "acp_contract.atom_segment_member",
               "silver_aa_internal.raw_tours"),
        writes=("acp_contract.atom_segment", "acp_contract.atom_segment_member",
                "acp_contract.atom_segment_alias"),
        # Mixed per table (AA-735): atom_segment never deletes (migration 129 FK), member
        # re-points as insert+delete on a bridging merge, alias upserts + chain-resolves.
        table_strategies={
            "acp_contract.atom_segment": "upsert",
            "acp_contract.atom_segment_member": "delete_insert",
            "acp_contract.atom_segment_alias": "upsert",
        },
        write_strategy="upsert",  # the dominant / id-stable strategy readers depend on
        run=_run_segment,
    ),
    "segment_catchup": Stage(
        name="segment_catchup",
        reads=("acp_contract.v_active_tour_atoms", "acp_contract.tour_atoms",
               "acp_contract.atom_segment", "acp_contract.atom_segment_member",
               "silver_aa_internal.raw_tours"),
        writes=("acp_contract.atom_segment", "acp_contract.atom_segment_member",
                "acp_contract.atom_segment_alias"),
        # AA-1059 — runs `run_segment_matching` per caught-up tour: the same writes as the `segment` stage.
        table_strategies={
            "acp_contract.atom_segment": "upsert",
            "acp_contract.atom_segment_member": "delete_insert",
            "acp_contract.atom_segment_alias": "upsert",
        },
        write_strategy="upsert",
        run=_run_segment_catchup,
    ),
    "landing": Stage(
        name="landing",
        reads=("acp_contract.atom_segment", "acp_contract.atom_segment_member",
               "acp_contract.v_active_tour_atoms", "acp_contract.tour_atoms",
               "acp_contract.search_demand", "silver_aa_internal.raw_tours"),
        writes=("acp_contract.atom_segment", "acp_contract.atom_matches",
                "acp_contract.atom_embedding", "acp_contract.question_embedding"),
        # Every table UPSERTed: questions_count cache (mig 163), atom_matches ON CONFLICT DO
        # UPDATE, and the two embedding caches ON CONFLICT DO NOTHING.
        table_strategies={
            "acp_contract.atom_segment": "upsert",
            "acp_contract.atom_matches": "upsert",
            "acp_contract.atom_embedding": "upsert",
            "acp_contract.question_embedding": "upsert",
        },
        write_strategy="upsert",
        run=_run_landing,
    ),
    "score": Stage(
        name="score",
        reads=("acp_contract.atom_segment", "acp_contract.atom_segment_member",
               "acp_contract.search_demand", "acp_contract.v_active_tour_atoms"),
        writes=("acp_contract.atom_ranking",),
        table_strategies={"acp_contract.atom_ranking": "versioned_swap"},
        write_strategy="versioned_swap",  # AA-734 / nac 2 — supersede + insert, no reader dip
        run=_run_score,
    ),
    "route": Stage(
        name="route",
        reads=("acp_contract.atom_ranking", "acp_contract.atom_segment",
               "acp_contract.atom_segment_member", "acp_contract.v_active_tour_atoms",
               "acp_contract.hub", "acp_contract.route"),
        writes=("acp_contract.route", "acp_contract.hub"),
        # Mixed per table (AA-735): route is versioned (supersede + insert, AA-532), hub is a
        # persistent upsert (UPDATE updated_at on reuse / INSERT on a new family, never superseded).
        table_strategies={
            "acp_contract.route": "versioned_swap",
            "acp_contract.hub": "upsert",
        },
        write_strategy="versioned_swap",  # route — AA-532; the strategy readers depend on
        run=_run_route,
    ),
    "retarget": Stage(
        name="retarget",
        reads=("acp_shared.subject", "acp_contract.route", "acp_shared.angle_gate_request",
               "acp_contract.tour_atoms", "acp_contract.v_active_tour_atoms", "acp_contract.atom_segment",
               "acp_contract.atom_segment_member", "acp_contract.atom_segment_alias",
               "acp_shared.atom_place_card", "silver_aa_internal.raw_tours", "gold_aa_internal.published_tours",
               "gold_aa_internal.v_tour_liveness", "gold_aa_internal.tenant_tour_versions"),
        writes=("acp_shared.subject", "acp_shared.angle_gate_request"),
        # AA-1057 GAP 2: UPDATE in place of the id a Subject / request points at (route_id, atom_id), guarded on
        # the old value; never an insert or delete (report mode: none). Declared "upsert", the in-place strategy.
        table_strategies={
            "acp_shared.subject": "upsert",
            "acp_shared.angle_gate_request": "upsert",
        },
        write_strategy="upsert",
        run=_run_retarget,
    ),
    "segment_gc": Stage(
        name="segment_gc",
        reads=("acp_contract.atom_segment", "acp_contract.atom_segment_member", "acp_contract.tour_atoms",
               "acp_contract.v_active_tour_atoms", "acp_contract.atom_ranking", "acp_contract.atom_segment_alias",
               "acp_contract.route", "acp_shared.subject", "acp_shared.debate_brand_fit_cache",
               "acp_shared.angle_gate_request"),
        writes=("acp_contract.atom_segment", "acp_contract.atom_segment_member"),
        # AA-988 — deletes only (report mode: none): orphan Segments and their member rows on dead atoms.
        table_strategies={
            "acp_contract.atom_segment": "delete_insert",
            "acp_contract.atom_segment_member": "delete_insert",
        },
        write_strategy="delete_insert",
        run=_run_segment_gc,
    ),
}


def get_stage(name: str) -> Stage:
    stage = _STAGES.get(name)
    if stage is None:
        raise KeyError(f"unknown recompute stage {name!r}; known: {sorted(_STAGES)}")
    return stage


def all_stages() -> dict[str, Stage]:
    """A copy of the registry (tests assert over the declared metadata)."""
    return dict(_STAGES)


# Named stage lists — these mirror the two hand-written chains they replace, exactly.
# AA-1057: `retarget` follows `route` on both (the new Route / atom ids exist by then).
# AA-1059: `segment_catchup` opens the platform pass (its Segments feed `landing`).
TOUR_STAGES = ["segment", "landing", "score", "route", "retarget"]
PLATFORM_STAGES = ["segment_catchup", "landing", "score", "route", "retarget", "segment_gc"]


async def run_stages(names: list[str], scope: RecomputeScope, *, pool,
                     progress: Optional[Callable[[dict], None]] = None,
                     should_stop: Optional[Callable[[], bool]] = None,
                     on_stage: Optional[Callable[[str], None]] = None) -> dict:
    """Run `names` in order on one shared `ctx`, return `{stage_name: result}`.

    Validates every name against the registry first (an unknown name is a programming error, not
    a runtime one — fail before touching the DB). `question_counts` and anything else a stage
    leaves in `ctx.data` is visible to the stages that follow it.

    AA-1056 — `should_stop` (the `recompute` job's cancel event) is asked before every stage and raises
    RecomputeStopped, so a job released by a deploy stops at the next stage boundary instead of running on beside the
    attempt that re-claims it; `on_stage(name)` is told which stage starts (the job names it in a stage failure).
    Both default to None: every other caller is unchanged.
    """
    from .errors import RecomputeStopped

    stages = [get_stage(n) for n in names]  # raises KeyError on an unknown name, before running
    ctx = RecomputeContext(pool=pool, progress=progress)
    results: dict = {}
    for stage in stages:
        if should_stop is not None and should_stop():
            logger.info("recompute_stopped", before=stage.name, tour_id=scope.log_tour_id or scope.tour_id)
            raise RecomputeStopped(f"before stage {stage.name} (job cancelled)")
        if on_stage is not None:
            on_stage(stage.name)
        results[stage.name] = await stage.run(ctx, scope)
    return results
