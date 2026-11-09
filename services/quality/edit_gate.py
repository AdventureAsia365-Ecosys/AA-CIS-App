"""AA-1052 W6 (root ADR 0020 d12, roadmap "UI edits on gated stages require an eval run id or a break-glass reason
and are frozen during waves") — the runtime edit gate of the writing path's admin surfaces ($0, no model call).

The PR-time change control (``change_control.py``: the lock file and the route-migration header lint) covers what
reaches the writers through code and migrations. This module covers what an admin changes at runtime:

=====================================  ====================================================================
surface                                gated when
=====================================  ====================================================================
PATCH /admin/llm-config/{stage}        the stage is gated and its primary model changes
PATCH /admin/llm-config/{stage}/route  the stage is gated and its fallback chain changes (a shadow-only
                                       change needs no evidence but is frozen during a wave)
POST /admin/ai-gateway/route-control/  a gated stage's chain or its first serving model@connection
  save | presets/{p} | versions/{v}/    changes (provider order = the catalog deployments' priorities, the
  revert                               order mode, a preset, a revert)
PATCH /admin/llm-catalog/{model_key}   the model is on a gated stage's route (primary or fallback)
PUT / DELETE /admin/ai-gateway/        the stage is gated (or ``*``, the defaults of every stage) and the
  route-policy/{stage}                 hold switch (``hold_fallback_writes``, what a writer stores) changes;
                                       an alert-threshold-only edit changes no output and passes
=====================================  ====================================================================

A gated stage: one of ``change_control.GATED_STAGE_PREFIXES`` (the same prefixes as the lock and the migration lint)
or a writer / judge stage (AA-1026 D7, ``route_evidence.EVIDENCE_ROLES``). ``shared.stage_model_params`` has no
runtime write path (migrations only, covered by the lint).

A gated edit:
  * is refused with 409 while a rerun wave is open (``shared.rerun_wave.status = 'open'``, the check the deploy gate
    and ``/admin/waves`` use): a wave compares tours written under one config, a mid-wave change splits it;
  * otherwise needs ``eval_run_id`` (a finished, valid, passing eval run, ``route_evidence.eval_run_problem``) or a
    ``break_glass_reason`` (at least 20 characters), else 422; the evidence goes into the edit's audit row
    (``acp_shared.audit_log.details.evidence``) and a break glass raises the admin bell in the same transaction.

Not gated: the stage kill switch (PATCH /llm-config/{stage}/switch: the incident lever, it stops calls rather than
changing what answers them), a provider connection on / off (the gateway's own failure handling), validate / embed
stages outside the gated prefixes.
"""
from __future__ import annotations

from typing import Iterable, Optional

from shared.llm_client import route_evidence

DEFAULT_STAGE = "*"   # the route-policy row that is the defaults of every stage

OPEN_WAVES_SQL = ("SELECT id::text AS id, name FROM shared.rerun_wave WHERE status = 'open' "
                  "ORDER BY created_at, id LIMIT 20")


class EditFrozen(RuntimeError):
    """A gated edit while a rerun wave is open (the router answers 409 with this text). Built by ``frozen_error``."""


def frozen_error(surface: str, stages: Iterable[str], waves: list[dict]) -> EditFrozen:
    """The refusal of one gated edit during the open ``waves`` (pure)."""
    stages = sorted(set(stages))
    names = ", ".join(f"{w.get('name') or '?'} ({w.get('id')})" for w in waves[:5])
    err = EditFrozen(
        f"{surface} is frozen while a rerun wave is open ({names}): a change of a gated stage "
        f"({', '.join(stages[:8])}) mid-wave would split the wave's tours across two configs. Close or finish the "
        f"wave first (the stage kill switch stays available).")
    err.stages, err.waves = stages, waves
    return err


def is_gated_stage(stage: Optional[str], role: Optional[str] = None) -> bool:
    """A stage the gate covers: a gated prefix (``*`` counts: it is the defaults of every stage) or a writer / judge
    role."""
    if stage == DEFAULT_STAGE:
        return True
    return route_evidence.needs_evidence(role, stage)


def gated(stages: Iterable[tuple[str, Optional[str]]]) -> list[str]:
    """The gated stages of (stage, role) pairs, sorted (pure)."""
    return sorted({s for s, r in stages if is_gated_stage(s, r)})


async def read_open_waves(pool=None) -> list[dict]:
    """The open rerun waves (id, name). ``pool`` (a pool or a connection) when the caller holds one, else a short own
    connection (the PATCH routes of admin_llm_ops write through their own connections too). Raises on a read error:
    the gate then fails closed (503)."""
    if pool is not None and hasattr(pool, "fetch"):
        return [dict(r) for r in await pool.fetch(OPEN_WAVES_SQL)]
    if pool is not None:                                  # a pool-like object that only lends connections
        async with pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(OPEN_WAVES_SQL)]
    import asyncpg

    from shared.secrets import get_database_url
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        return [dict(r) for r in await conn.fetch(OPEN_WAVES_SQL)]
    finally:
        await conn.close()


async def refuse_if_frozen(surface: str, stages: Iterable[str], *, pool=None) -> None:
    """Raise EditFrozen when ``stages`` names a gated stage (already filtered by the caller) and a wave is open."""
    stages = sorted(set(stages))
    if not stages:
        return
    waves = await read_open_waves(pool)
    if waves:
        raise frozen_error(surface, stages, waves)


async def require_evidence(stage: str, role: Optional[str], *, eval_run_id: Optional[str],
                           break_glass_reason: Optional[str], what: Optional[str] = None,
                           read_run=None) -> Optional[dict]:
    """The evidence of one gated edit as stored in its audit row (None when the stage is not gated). Raises
    route_evidence.EvidenceRefused (422) when it has none usable."""
    if not is_gated_stage(stage, role):
        return None
    return await route_evidence.require(role, stage=stage, required=True, what=what, eval_run_id=eval_run_id,
                                        break_glass_reason=break_glass_reason, read_run=read_run)


__all__ = ["DEFAULT_STAGE", "EditFrozen", "OPEN_WAVES_SQL", "frozen_error", "gated", "is_gated_stage",
           "read_open_waves", "refuse_if_frozen", "require_evidence"]
