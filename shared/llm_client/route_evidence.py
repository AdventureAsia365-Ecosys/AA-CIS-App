"""AA-1026 D7 (root ADR 0009 decision 12, tech lead T-19, S263) — a route change needs evidence.

A change of the models a writer or judge stage is served by (the primary model, or the fallback chain) must name the
eval run that backs it (``eval_run_id``: a row of ``shared.prompt_eval_runs``) or, when no eval can run (an outage,
LLM access blocked), a break-glass reason. The admin PATCH routes (``api/routers/admin_llm_ops.py``) refuse a change
with neither (422). The evidence is written into the change's ``acp_shared.audit_log`` row (``details.evidence``,
same transaction as the UPDATE); a break-glass change also raises an admin bell (``shared.notifications``, event
``platform.llm_route.break_glass``) in that transaction.

Not covered: ``validate`` and ``embed`` stages (CIS ADR 0008 decision 2: a validate stage may use any enabled model),
a shadow-only change (the shadow is how evidence is gathered; it never serves a reply), an account-route-only change
and the stage kill switch (an operation, not a model change).

An eval run counts only when it measured the change and passed:
- a writing-memory run (``pipeline = 'writing_memory'``): finished, ``verdict = 'pass'`` and
  ``stop_reason = 'completed'`` (a ``baseline`` / ``informative`` verdict, or a run stopped by ``error`` /
  ``budget_cap`` / ``futility``, shows nothing about the change);
- a legacy regression row (``s1_old`` / ``s1_from_atom``, written once at the end, ``verdict`` NULL): not INVALID
  (``details.invalid``: a hard detector failed open, or a tour was judged by a model of its writer's family) and no
  regression detected (``_detect_regression`` forces ``regression_detected = false`` on an INVALID run, so the flag
  alone is not enough).
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import structlog

logger = structlog.get_logger()

EVIDENCE_ROLES = ("writer", "judge")
BREAK_GLASS_MIN_CHARS = 20
BREAK_GLASS_MAX_CHARS = 500
BREAK_GLASS_EVENT = "platform.llm_route.break_glass"
_FAILED_VERDICTS = ("fail", "invalid")      # a legacy regression row (verdict NULL today) with one of these
_PASS_VERDICT = "pass"                      # the only writing-memory verdict that backs a change
_COMPLETED = "completed"                    # ... and only when the run was not stopped early
# The platform sentinel tenant (shared.notifications.tenant_id is NOT NULL; same UUID as the other platform bells).
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_EVAL_RUN_SQL = """
    SELECT id::text AS id, pipeline, run_kind, writer, verdict, stop_reason, regression_detected,
           (details -> 'invalid') = 'true'::jsonb AS details_invalid, finished_at, created_at
      FROM shared.prompt_eval_runs
     WHERE id = $1::uuid
"""

_BELL_SQL = """
    INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, target_roles)
    VALUES ($1::uuid, 'system', $2, 'llm_role_config', $3, $4::jsonb, ARRAY['admin'])
"""


class EvidenceRefused(ValueError):
    """The change carries no usable evidence (the router answers 422 with this text)."""


def needs_evidence(role: Optional[str]) -> bool:
    return role in EVIDENCE_ROLES


def eval_run_problem(run_id: str, row: Optional[dict]) -> Optional[str]:
    """Why the eval run cannot back a route change, or None when it can (pure)."""
    if row is None:
        return f"eval run {run_id} does not exist"
    if row.get("details_invalid"):
        return (f"eval run {run_id} is INVALID (a hard detector failed open or the judge shared the writer's "
                f"family): it measured nothing")
    if row.get("pipeline") == "writing_memory":
        if row.get("finished_at") is None:
            return f"eval run {run_id} has not finished"
        if row.get("verdict") != _PASS_VERDICT:
            return f"eval run {run_id} ended with verdict {row.get('verdict')}, not {_PASS_VERDICT}"
        if row.get("stop_reason") != _COMPLETED:
            return f"eval run {run_id} stopped with {row.get('stop_reason')}, not {_COMPLETED}"
    elif row.get("verdict") in _FAILED_VERDICTS:
        return f"eval run {run_id} ended with verdict {row['verdict']}"
    if row.get("regression_detected"):
        return f"eval run {run_id} detected a regression"
    return None


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise EvidenceRefused(f"eval_run_id {str(value)[:80]!r} is not a run id (a UUID of an eval run)")


async def read_eval_run(run_id: str) -> Optional[dict]:
    """The eval run row, or None. Its own short connection (the same login as role_config's writes)."""
    import asyncpg

    from shared.secrets import get_database_url

    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        row = await conn.fetchrow(_EVAL_RUN_SQL, run_id)
        return dict(row) if row is not None else None
    finally:
        await conn.close()


async def require(role: Optional[str], *, eval_run_id: Optional[str], break_glass_reason: Optional[str],
                  read_run=None) -> Optional[dict]:
    """The evidence of one change as stored in its audit row, or None when the stage needs none (a validate / embed
    stage). Raises EvidenceRefused when the change needs evidence and has none usable. An eval run id, when given,
    must be valid even next to a break-glass reason (a wrong id is never waved through)."""
    if not needs_evidence(role):
        return None
    run_id = (eval_run_id or "").strip()
    reason = " ".join((break_glass_reason or "").split())
    evidence: dict = {}
    if run_id:
        run_id = _uuid(run_id)
        row = await (read_run or read_eval_run)(run_id)
        problem = eval_run_problem(run_id, row)
        if problem:
            raise EvidenceRefused(f"{problem}: a {role} route change needs a finished, valid eval run that passed")
        evidence["eval_run_id"] = run_id
        evidence["eval_run"] = {k: row.get(k) for k in ("pipeline", "run_kind", "writer", "verdict")}
    if reason:
        if len(reason) < BREAK_GLASS_MIN_CHARS:
            raise EvidenceRefused(f"break_glass_reason must say why in at least {BREAK_GLASS_MIN_CHARS} characters")
        evidence["break_glass_reason"] = reason[:BREAK_GLASS_MAX_CHARS]
    if not evidence:
        raise EvidenceRefused(
            f"a {role} route change needs evidence: eval_run_id (a finished eval run of the change) or, when no eval "
            f"can run, break_glass_reason (at least {BREAK_GLASS_MIN_CHARS} characters; it raises an admin alert)")
    return evidence


async def record_break_glass(conn, *, stage: str, actor: str, evidence: Optional[dict], change: dict) -> bool:
    """Inside the change's transaction: the admin bell of a break-glass change (no-op without a reason). True when a
    bell was written."""
    if not evidence or not evidence.get("break_glass_reason"):
        return False
    payload = {
        "message": f"Route of stage {stage} changed without an eval run (break glass) by {actor}: "
                   f"{evidence['break_glass_reason']}",
        "stage": stage, "actor": actor, "reason": evidence["break_glass_reason"], "change": change,
        "eval_run_id": evidence.get("eval_run_id"),
    }
    await conn.execute(_BELL_SQL, _PLATFORM_TENANT_ID, BREAK_GLASS_EVENT, stage, json.dumps(payload, default=str))
    logger.warning("llm_route_break_glass", stage=stage, actor=actor, reason=evidence["break_glass_reason"][:200])
    return True


__all__ = ["BREAK_GLASS_EVENT", "BREAK_GLASS_MIN_CHARS", "EVIDENCE_ROLES", "EvidenceRefused", "eval_run_problem",
           "needs_evidence", "read_eval_run", "record_break_glass", "require"]
