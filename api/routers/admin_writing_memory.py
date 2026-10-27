"""AA-917 WM-14 (API) — Quality › Writing memory: the Signals and Candidates of the governed writing memory (app ADR
0010). Admin only: signals and tenant rules hold tenant text.

  GET   /admin/writing-memory/summary                 counts (signals per writer / scope / fact status / action,
                                                      rules per status), the month's learning-loop spend vs the $25
                                                      cap, the mining trigger state, the last runs of the 3 job kinds
  GET   /admin/writing-memory/signals?writer=&scope=&tenant_id=&fact_status=&actor_type=&action=&limit=&offset=
                                                      the signals, newest first (texts cut to 600 characters)
  GET   /admin/writing-memory/signals/clusters        the clusters the miner would see now ($0, same code), with
                                                      their support, basis, open rule and two masked samples;
                                                      plus signals per writer per ISO week (8 weeks)
  GET   /admin/writing-memory/signals/{signal_id}     one signal, full text, and its fact-filter verdict recomputed
  PATCH /admin/writing-memory/signals/{signal_id}/fact {status: pass | fail}: the admin's confirmation of a grey
                                                      (or any) verdict; never `pass` for fact_not_in_source
  GET   /admin/writing-memory/candidates?status=&writer=&scope=&tenant_id=&limit=&offset=   the rules
  GET   /admin/writing-memory/candidates/{rule_id}    one rule with its provenance (the signals, with their source);
                                                      AA-918: `l0_activation` (null, or the code and reason the L0
                                                      path refuses it now) and `r0_fresh`
  PATCH /admin/writing-memory/candidates/{rule_id}    edit a candidate {rule_text, replacement, match_phrases}: the
                                                      lint runs again (a failing edit rejects it; an edit with tenant
                                                      text, a tenant brand or over the size rules is a 422 and
                                                      nothing is written)
  POST  /admin/writing-memory/candidates/{rule_id}/reject   {reason}
  POST  /admin/writing-memory/candidates/{rule_id}/r0       AA-918: queue the R0 replay of this rule ($0; the result
                                                      lands on the rule, `lint.r0`)
  POST  /admin/writing-memory/candidates/{rule_id}/activate AA-918: the L0 fast path — a lexicon rule of a T9 writer
                                                      with a fresh passing R0 goes active (the admin's one click;
                                                      the next T9 write gets it)
  POST  /admin/writing-memory/candidates/{rule_id}/rollback {reason}: AA-918, an active rule -> rejected (one click;
                                                      the next write no longer gets it)
  POST  /admin/writing-memory/run                     {kind: backfill | fact_filter | mine | r0_baseline, dry_run?}:
                                                      queue a job (mine = a manual run: the paid one, under its caps;
                                                      r0_baseline = the R0 sanity check alone, $0)

AA-919 (WM-18) — releases (services/writing_memory/releases.py; CIS 287). Ids, statuses and numbers only:
  GET   /admin/writing-memory/releases?writer=&status=&scope=&tenant_id=&limit=&offset=   the releases, each with
                                                      `evidence` {r0, eval, shadow}: the named run's kind / verdict
  GET   /admin/writing-memory/releases/active          the active release per writer and owner (the pointers the
                                                      writers resolve) and the memory switch per writer
  GET   /admin/writing-memory/releases/{release_id}   one release, its evidence runs (with `summary`: whitelisted
                                                      counts and hashes of the run details) and `activation` (null,
                                                      or the code and reason the API would refuse to activate it now)
  POST  /admin/writing-memory/releases                {writer, scope, tenant_id?, rule_ids?, train_kind?,
                                                      emergency_reason?, emergency_ref?, notes?}: a draft
  POST  /admin/writing-memory/releases/reverify-scan  flag the active releases whose golden set, model route or gate
                                                      taxonomy changed (they keep serving)
  POST  /admin/writing-memory/releases/{release_id}/evidence  {r0_run_id?, eval_run_id?, shadow_run_id?}
  POST  /admin/writing-memory/releases/{release_id}/approve   {approval_ref?}: the one human click (needs an admin id)
  POST  /admin/writing-memory/releases/{release_id}/activate  the pointer moves to it (the API checks the evidence,
                                                      the DB checks it again: 409 with a code, `db_refused` when only
                                                      the DB layer refused)
  POST  /admin/writing-memory/releases/{release_id}/rollback  {reason}: back to the previous release, no deploy
  POST  /admin/writing-memory/releases/{release_id}/reject    {reason}
  GET   /admin/writing-memory/writers                 the memory switch and the train close per writer
  PATCH /admin/writing-memory/writers/{writer}        {memory_enabled?, train_closed_month?} (on needs an active
                                                      platform release)
Every release write leaves an acp_shared.audit_log row (writing_memory.release_*), ids only.

Who may call (api/core/staff_authz.py POLICY): every read needs tenant.read and every write ai.admin (admin only).
Approval of a non-lexicon rule is not here: it reaches a writer only through a release (AA-919), one human click. A
lexicon rule may take the L0 path (ADR 0010 decision 6, D11): R0 + the activate click, no release.
Errors: 404 unknown id, 409 the row is not in a state for the action, 422 a bad body, 503 `unavailable` when the CIS
277 / 280 tables are missing (code deployed before the migration).

The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.writing_memory import config as cfg
from services.writing_memory import fact_filter as ff
from services.writing_memory import l0 as l0_mod
from services.writing_memory import lint as lint_mod
from services.writing_memory import mining
from services.writing_memory import rules as rules_mod
from services.writing_memory import signals as wm
from services.writing_memory.masking import excerpt
from services.writing_memory.replay import rule_sha
from services.writing_memory.reason_codes import FACT_NOT_IN_SOURCE

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/writing-memory", tags=["admin-writing-memory"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

PREVIEW_CHARS = 600
_JOB_KINDS = {"backfill": "writing_signal_backfill", "fact_filter": "writing_signal_fact_filter",
              "mine": "writing_memory_mine", "r0_baseline": "writing_memory_r0"}


def _pool(request: Request):
    return request.app.state.pool


def _uuid(value: str, what: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail=f"Unknown {what}")


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _cut(text: Optional[str], n: int = PREVIEW_CHARS) -> Optional[str]:
    return text if text is None or len(text) <= n else text[:n] + "…"


def _unavailable(e: Exception):
    logger.warning("aa917_writing_memory_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
    return HTTPException(status_code=503, detail={"detail": "The writing-memory tables are not applied yet "
                                                            "(CIS 277 / 280 / 287)", "code": "unavailable"})


# ── summary ─────────────────────────────────────────────────────────────────────────────────────────────────────────

_SUMMARY_SQL = """
    SELECT writer, scope, fact_status, action, actor_type, count(*) AS n
      FROM shared.writing_signal GROUP BY 1, 2, 3, 4, 5
"""
_RULES_SQL = "SELECT status, scope, count(*) AS n FROM shared.writing_rule GROUP BY 1, 2"
_LAST_JOBS_SQL = """
    SELECT DISTINCT ON (kind) kind, id::text AS job_id, status, created_at, finished_at, result
      FROM shared.job WHERE kind = ANY($1::text[]) ORDER BY kind, created_at DESC
"""


@router.get("/summary")
async def summary(request: Request):
    from services.writing_memory.budget import month_spend
    pool = _pool(request)
    try:
        async with pool.acquire() as conn:
            sig = [dict(r) for r in await conn.fetch(_SUMMARY_SQL)]
            rules = [dict(r) for r in await conn.fetch(_RULES_SQL)]
            spent = await month_spend(conn)
            refusal, gate = await mining.run_gate(conn, "auto")
            jobs = [dict(r) for r in await conn.fetch(_LAST_JOBS_SQL, sorted(set(_JOB_KINDS.values())))]
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    by: dict[str, Counter] = defaultdict(Counter)
    for r in sig:
        for dim in ("writer", "scope", "fact_status", "action", "actor_type"):
            by[dim][r[dim]] += int(r["n"])
    rule_counts: dict[str, Counter] = defaultdict(Counter)
    for r in rules:
        rule_counts[r["status"]][r["scope"]] += int(r["n"])
    return {
        "signals": {"total": sum(int(r["n"]) for r in sig), **{k: dict(v) for k, v in by.items()}},
        "rules": {status: dict(c) for status, c in rule_counts.items()},
        "budget": {"month_cap_usd": cfg.MONTH_CAP_USD, "month_spent_usd": round(spent, 4),
                   "run_cap_usd": cfg.RUN_CAP_USD, "max_calls_per_run": cfg.MINE_MAX_CALLS},
        "mine_trigger": {"auto_refused": refusal, **gate,
                         "thresholds": {"min_new_pass": cfg.MINE_MIN_NEW_PASS,
                                        "max_auto_runs_per_month": cfg.MINE_MAX_AUTO_RUNS_PER_MONTH,
                                        "max_runs_per_month": cfg.MINE_MAX_RUNS_PER_MONTH}},
        "last_jobs": {r["kind"]: {"job_id": r["job_id"], "status": r["status"], "created_at": _iso(r["created_at"]),
                                  "finished_at": _iso(r["finished_at"]), "result": r["result"]} for r in jobs},
    }


# ── signals ─────────────────────────────────────────────────────────────────────────────────────────────────────────

_SIGNAL_COLS = """
    signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, field, locator, before_text,
    after_text, gate, problem, reason_code, action, actor_type, actor_id, fact_status, exemplar_pref,
    audit_log_id::text AS audit_log_id, source_table, source_id, created_at
"""


def _signal_out(r: dict, full: bool = False) -> dict:
    out = {k: _iso(v) for k, v in r.items()}
    if not full:
        for k in ("before_text", "after_text", "problem"):
            out[k] = _cut(out.get(k))
    return out


@router.get("/signals")
async def list_signals(request: Request, writer: Optional[str] = None, scope: Optional[str] = None,
                       tenant_id: Optional[str] = None, fact_status: Optional[str] = None,
                       actor_type: Optional[str] = None, action: Optional[str] = None,
                       limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    where, args = ["true"], []
    for col, val in (("writer", writer), ("scope", scope), ("fact_status", fact_status), ("actor_type", actor_type),
                     ("action", action)):
        if val:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    if tenant_id:
        args.append(_uuid(tenant_id, "tenant"))
        where.append(f"tenant_id = ${len(args)}::uuid")
    w = " AND ".join(where)
    try:
        async with _pool(request).acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM shared.writing_signal WHERE {w}", *args)
            rows = await conn.fetch(f"SELECT {_SIGNAL_COLS} FROM shared.writing_signal WHERE {w} "
                                    f"ORDER BY created_at DESC, signal_id LIMIT {limit} OFFSET {offset}", *args)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"items": [_signal_out(dict(r)) for r in rows], "total": int(total or 0), "limit": limit, "offset": offset}


_WEEKS_SQL = """
    SELECT writer, to_char(date_trunc('week', created_at AT TIME ZONE 'utc'), 'IYYY-"W"IW') AS week, count(*) AS n
      FROM shared.writing_signal
     WHERE created_at >= now() - interval '8 weeks'
     GROUP BY 1, 2 ORDER BY 2, 1
"""


@router.get("/signals/clusters")
async def signal_clusters(request: Request):
    """The miner's view right now ($0): every cluster over threshold, its basis and support, whether it already has an
    open rule, and two masked samples (what the drafter would see)."""
    pool = _pool(request)
    try:
        async with pool.acquire() as conn:
            sigs = [dict(r) for r in await conn.fetch(mining._SIGNALS_SQL, cfg.SIGNAL_WINDOW_DAYS)]
            a1_pieces = await mining.a1_piece_count(conn)
            open_keys = await rules_mod.open_cluster_keys(conn)
            weeks = [dict(r) for r in await conn.fetch(_WEEKS_SQL)]
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    clusters = mining.build_clusters(sigs, a1_pieces_30d=a1_pieces)
    items = []
    for c in clusters:
        samples = [mining.example_of(s) for s in c.signals[:2]]
        items.append({"cluster_key": c.cluster_key, "writer": c.writer, "scope": c.scope, "tenant_id": c.tenant_id,
                      "label": c.label, "meaning": mining.label_meaning(c.label), "basis": c.basis,
                      "channels": c.channels(), "support": c.support(),
                      "has_open_rule": (c.writer, c.scope, c.tenant_id, c.cluster_key) in open_keys,
                      "draftable": len(c.signals) >= cfg.MIN_CLUSTER_FOR_DRAFT, "samples": samples,
                      "signal_ids": [s["signal_id"] for s in c.signals][:50]})
    return {"clusters": items, "pass_signals": len(sigs), "a1_pieces_30d": a1_pieces, "weeks": weeks,
            "thresholds": {"tenant_min_signals": cfg.TENANT_MIN_SIGNALS, "tenant_min_pieces": cfg.TENANT_MIN_PIECES,
                           "platform_min_tenants": cfg.PLATFORM_MIN_TENANTS,
                           "platform_min_reviewers": cfg.PLATFORM_MIN_REVIEWERS,
                           "a1_gate_repeat_share": cfg.A1_GATE_REPEAT_SHARE,
                           "min_cluster_for_draft": cfg.MIN_CLUSTER_FOR_DRAFT}}


@router.get("/signals/{signal_id}")
async def get_signal(signal_id: str, request: Request):
    sid = _uuid(signal_id, "signal")
    try:
        async with _pool(request).acquire() as conn:
            row = await conn.fetchrow(f"SELECT {_SIGNAL_COLS} FROM shared.writing_signal WHERE signal_id = $1::uuid",
                                      sid)
            if row is None:
                raise HTTPException(status_code=404, detail="Unknown signal")
            source = await ff.load_source(conn, row["source_table"], row["source_id"])
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    v = ff.classify(row["before_text"], row["after_text"], source, row["reason_code"])
    b, a = excerpt(row["before_text"], row["after_text"]) if row["after_text"] is not None else ("", None)
    return {**_signal_out(dict(row), full=True),
            "fact_check": {"status": v.status, "code": v.code, "reasons": v.reasons, "source_found": bool(source)},
            "masked_excerpt": {"before": b, "after": a}}


class FactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "fail"]


@router.patch("/signals/{signal_id}/fact")
async def set_signal_fact(signal_id: str, body: FactBody, request: Request,
                          x_admin_user_id: Optional[str] = Header(None)):
    """The admin's verdict on a signal's facts (a grey one waits for it). fact_not_in_source stays fail."""
    sid = _uuid(signal_id, "signal")
    try:
        async with _pool(request).acquire() as conn:
            reason = await conn.fetchrow("SELECT reason_code FROM shared.writing_signal WHERE signal_id = $1::uuid",
                                         sid)
            if reason is None:
                raise HTTPException(status_code=404, detail="Unknown signal")
            if reason["reason_code"] == FACT_NOT_IN_SOURCE and body.status == "pass":
                raise HTTPException(status_code=409, detail={"detail": "The admin marked this signal "
                                                                       "fact_not_in_source: it stays fail",
                                                             "code": "fact_not_in_source"})
            row = await conn.fetchrow("UPDATE shared.writing_signal SET fact_status = $2 WHERE signal_id = $1::uuid "
                                      "RETURNING signal_id::text AS signal_id, fact_status", sid, body.status)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    logger.info("aa917_signal_fact_set", signal_id=sid, status=body.status,
                actor=wm.admin_actor_id(x_admin_user_id))
    return dict(row)


# ── candidates ──────────────────────────────────────────────────────────────────────────────────────────────────────

@router.get("/candidates")
async def list_candidates(request: Request, status: Optional[str] = None, writer: Optional[str] = None,
                          scope: Optional[str] = None, tenant_id: Optional[str] = None,
                          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    if status and status not in rules_mod.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(rules_mod.STATUSES)}")
    if tenant_id:
        _uuid(tenant_id, "tenant")
    try:
        async with _pool(request).acquire() as conn:
            items, total = await rules_mod.list_rules(conn, status=status, writer=writer, scope=scope,
                                                      tenant_id=tenant_id, limit=limit, offset=offset)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"items": [rules_mod.serialise(r) for r in items], "total": total, "limit": limit, "offset": offset}


_PROVENANCE_SQL = f"SELECT {_SIGNAL_COLS} FROM shared.writing_signal WHERE signal_id = ANY($1::uuid[]) " \
                  "ORDER BY created_at DESC"


@router.get("/candidates/{rule_id}")
async def get_candidate(rule_id: str, request: Request):
    rid = _uuid(rule_id, "rule")
    try:
        async with _pool(request).acquire() as conn:
            rule = await rules_mod.get_rule(conn, rid)
            if rule is None:
                raise HTTPException(status_code=404, detail="Unknown rule")
            sigs = [dict(r) for r in await conn.fetch(_PROVENANCE_SQL, rule["signal_ids"] or [])]
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    out = rules_mod.serialise(rule)
    # AA-918 — what stands between this rule and the L0 path now (None = it may be activated), and whether its stored
    # R0 result is for its current text
    problem = l0_mod.activation_problem(out)
    r0 = out.get("lint", {}).get("r0") if isinstance(out.get("lint"), dict) else None
    return {**out, "provenance": [_signal_out(s) for s in sigs],
            "l0_activation": None if problem is None else {"code": problem[0], "detail": problem[1]},
            "r0_fresh": isinstance(r0, dict) and r0.get("rule_sha") == rule_sha(out)}


class CandidateEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_text: str = Field(..., min_length=1, max_length=1000)
    replacement: Optional[str] = Field(None, max_length=200)
    match_phrases: list[str] = Field(..., min_length=1, max_length=cfg.MAX_MATCH_PHRASES)


@router.patch("/candidates/{rule_id}")
async def edit_candidate(rule_id: str, body: CandidateEdit, request: Request,
                         x_admin_user_id: Optional[str] = Header(None)):
    """Edit a candidate's text; the lint runs again over its own cluster (every provenance signal is a PREDICT
    example) and, for a platform rule, every tenant's text and names."""
    rid = _uuid(rule_id, "rule")
    actor = f"admin:{wm.admin_actor_id(x_admin_user_id)}"
    phrases = [p.strip() for p in body.match_phrases if p.strip()]
    try:
        async with _pool(request).acquire() as conn:
            rule = await rules_mod.get_rule(conn, rid)
            if rule is None:
                raise HTTPException(status_code=404, detail="Unknown rule")
            if rule["status"] != "candidate":
                raise HTTPException(status_code=409, detail={"detail": f"A {rule['status']} rule cannot be edited",
                                                             "code": "not_candidate"})
            sigs = [dict(r) for r in await conn.fetch(_PROVENANCE_SQL, rule["signal_ids"] or [])]
            grams, names, missing = await mining._corpora(conn)
            cluster = mining.Cluster(rule["writer"], rule["scope"], rule["tenant_id"], rule["cluster_key"], "edit")
            corpus, banned = mining.lint_context(cluster, set().union(*grams.values()) if grams else set(), names)
            lint = lint_mod.run_lint(kind=rule["kind"], rule_text=body.rule_text.strip(),
                                     replacement=(body.replacement or "").strip() or None, match_phrases=phrases,
                                     scope=rule["scope"], held_out=sigs, corpus_ngrams=corpus, names=banned,
                                     corpus_unavailable=mining.corpus_gap(rule["scope"], missing))
            # An edit that leaks tenant text (or another tenant's brand) or breaks the store's size rules is refused
            # and nothing is written: the candidate keeps its text, and the stored lint never quotes tenant text.
            if lint["five_gram_overlap"].get("leak"):
                raise HTTPException(status_code=422, detail={
                    "detail": f"The edit {lint['five_gram_overlap']['detail']}; rewrite it in general words",
                    "code": "tenant_text"})
            if not lint["shape"]["ok"]:
                raise HTTPException(status_code=422, detail={"detail": f"The edit is too long: "
                                                                       f"{lint['shape']['detail']}", "code": "shape"})
            lint["edited_by"] = actor
            row = await rules_mod.edit_candidate(conn, rid, rule_text=body.rule_text.strip(),
                                                 replacement=(body.replacement or "").strip() or None,
                                                 match_phrases=phrases, lint=lint, actor=actor, kind=rule["kind"])
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except asyncpg.CheckViolationError as e:
        raise HTTPException(status_code=422, detail={"detail": f"The rule breaks a store rule: {str(e)[:200]}",
                                                     "code": "invalid"})
    if row is None:
        raise HTTPException(status_code=409, detail={"detail": "The rule changed meanwhile; reload", "code": "stale"})
    return rules_mod.serialise(row)


class RejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=500)


@router.post("/candidates/{rule_id}/reject")
async def reject_candidate(rule_id: str, body: RejectBody, request: Request,
                           x_admin_user_id: Optional[str] = Header(None)):
    rid = _uuid(rule_id, "rule")
    try:
        async with _pool(request).acquire() as conn:
            row = await rules_mod.reject(conn, rid, reason=body.reason.strip(),
                                         actor=f"admin:{wm.admin_actor_id(x_admin_user_id)}")
            if row is None:
                rule = await rules_mod.get_rule(conn, rid)
                if rule is None:
                    raise HTTPException(status_code=404, detail="Unknown rule")
                raise HTTPException(status_code=409, detail={"detail": f"A {rule['status']} rule cannot be rejected "
                                                                       "here", "code": "not_rejectable"})
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return rules_mod.serialise(row)


# ── R0 and the L0 fast path (AA-918) ────────────────────────────────────────────────────────────────────────────────

def _job_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")


@router.post("/candidates/{rule_id}/r0")
async def run_rule_r0(rule_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """Queue the R0 replay of one rule ($0, no model call); the result is kept on the rule (`lint.r0`)."""
    from services.jobs.writing_memory_job import R0
    from shared.jobs.registry import enqueue
    rid = _uuid(rule_id, "rule")
    actor = wm.admin_actor_id(x_admin_user_id)
    pool = _pool(request)
    try:
        async with pool.acquire() as conn:
            if await rules_mod.get_rule(conn, rid) is None:
                raise HTTPException(status_code=404, detail="Unknown rule")
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    job_id, created = await enqueue(pool, R0, {"rule_ids": [rid], "requested_by": actor},
                                    idempotency_key=f"{R0}:{rid}:{_job_stamp()}", created_by=f"admin:{actor}")
    return {"job_id": job_id, "kind": R0, "created": created}


@router.post("/candidates/{rule_id}/activate")
async def activate_rule(rule_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """The L0 fast path (WM-16): a lexicon rule of a T9 writer whose R0 on its current text passed goes active with
    the admin's id as approver. 409 with a code when the rule is not ready (not_lexicon, no_l0_reader,
    not_activatable, lint, phrase, replacement_adds, r0_missing, r0_stale, r0_failed, replacement_forbidden,
    token_cap); 403 no_actor when the caller has no admin id (an approval is a human's, never a job's)."""
    rid = _uuid(rule_id, "rule")
    actor = wm.admin_actor_id(x_admin_user_id)
    if actor == "unknown":
        raise HTTPException(status_code=403, detail={"detail": "Activating a rule needs a signed-in admin (no admin id "
                                                               "on the request)", "code": "no_actor"})
    try:
        async with _pool(request).acquire() as conn:
            try:
                # The check and the write are one transaction on a locked row: an edit (PATCH) landing between them
                # would otherwise send text active that R0 never replayed (review round, WM-16 "R0 on its current
                # text"). A concurrent edit waits for this commit, or this read waits for the edit and sees r0_stale.
                async with conn.transaction():
                    rule = await rules_mod.get_rule(conn, rid, for_update=True)
                    if rule is None:
                        raise HTTPException(status_code=404, detail="Unknown rule")
                    problem = l0_mod.activation_problem(rules_mod.serialise(rule))
                    if problem:
                        raise HTTPException(status_code=409, detail={"detail": problem[1], "code": problem[0]})
                    row = await rules_mod.activate_l0(conn, rid, approved_by=actor)
            except asyncpg.CheckViolationError as e:
                raise HTTPException(status_code=409, detail={
                    "detail": f"The writer's active rules would exceed their token cap: {str(e)[:200]}",
                    "code": "token_cap"})
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    if row is None:
        raise HTTPException(status_code=409, detail={"detail": "The rule changed meanwhile; reload", "code": "stale"})
    logger.info("aa918_l0_activated", rule_id=rid, writer=row["writer"], scope=row["scope"], actor=actor)
    return rules_mod.serialise(row)


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=400)


@router.post("/candidates/{rule_id}/rollback")
async def rollback_rule(rule_id: str, body: RollbackBody, request: Request,
                        x_admin_user_id: Optional[str] = Header(None)):
    """Roll an active rule back (one click): it becomes `rejected` with `rollback: <reason>`; the next write no longer
    gets it."""
    rid = _uuid(rule_id, "rule")
    actor = wm.admin_actor_id(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            row = await rules_mod.rollback(conn, rid, actor=f"admin:{actor}", reason=body.reason.strip())
            if row is None:
                rule = await rules_mod.get_rule(conn, rid)
                if rule is None:
                    raise HTTPException(status_code=404, detail="Unknown rule")
                raise HTTPException(status_code=409, detail={"detail": f"A {rule['status']} rule is not active",
                                                             "code": "not_active"})
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    logger.info("aa918_l0_rolled_back", rule_id=rid, actor=actor)
    return rules_mod.serialise(row)


# ── runs ────────────────────────────────────────────────────────────────────────────────────────────────────────────

class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["backfill", "fact_filter", "mine", "r0_baseline"]
    dry_run: bool = False


@router.post("/run")
async def run(body: RunBody, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """Queue one job. A manual mining run is refused up front when its trigger does not hold (409, the reason)."""
    from shared.jobs.registry import enqueue
    actor = wm.admin_actor_id(x_admin_user_id)
    kind = _JOB_KINDS[body.kind]
    payload: dict = {"requested_by": actor}
    pool = _pool(request)
    if body.kind == "backfill":
        payload["dry_run"] = body.dry_run
    elif body.kind == "r0_baseline":
        payload["baseline"] = True
    elif body.kind == "mine":
        payload["trigger"] = "manual"
        try:
            async with pool.acquire() as conn:
                refusal, facts = await mining.run_gate(conn, "manual")
        except asyncpg.UndefinedTableError as e:
            raise _unavailable(e)
        if refusal:
            raise HTTPException(status_code=409, detail={"detail": refusal, "code": "trigger", **facts})
    now = _job_stamp()
    job_id, created = await enqueue(pool, kind, payload, idempotency_key=f"{kind}:admin:{now}",
                                    created_by=f"admin:{actor}")
    return {"job_id": job_id, "kind": kind, "created": created}


# ── releases (AA-919 WM-18) ─────────────────────────────────────────────────────────────────────────────────────────

def _release_error(e) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"detail": e.detail, "code": e.code})


def _need_actor(raw: Optional[str]) -> str:
    actor = wm.admin_actor_id(raw)
    if actor == "unknown":
        raise HTTPException(status_code=403, detail={"detail": "This action needs a signed-in admin (no admin id "
                                                               "on the request)", "code": "no_actor"})
    return actor


@router.get("/releases")
async def list_releases(request: Request, writer: Optional[str] = None, status: Optional[str] = None,
                        scope: Optional[str] = None, tenant_id: Optional[str] = None,
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    from services.writing_memory import releases as rel
    if status and status not in rel.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(rel.STATUSES)}")
    if tenant_id:
        tenant_id = _uuid(tenant_id, "tenant")
    try:
        async with _pool(request).acquire() as conn:
            items, total = await rel.list_releases(conn, writer=writer, status=status, scope=scope,
                                                   tenant_id=tenant_id, limit=limit, offset=offset)
            verdicts = await rel.run_verdicts(conn, [r.get(f"{w}_run_id") for r in items
                                                     for w in ("r0", "eval", "shadow")])
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"items": [{**rel.serialise(r), "evidence": rel.evidence_of(r, verdicts)} for r in items],
            "total": total, "limit": limit, "offset": offset}


@router.get("/releases/active")
async def active_releases(request: Request):
    from services.writing_memory import releases as rel
    try:
        async with _pool(request).acquire() as conn:
            items = await rel.active_releases(conn)
            switches = await rel.writers(conn)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"items": [rel.serialise(r) for r in items], "writers": [rel.serialise(w) for w in switches]}


@router.get("/releases/{release_id}")
async def get_release(release_id: str, request: Request):
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    try:
        async with _pool(request).acquire() as conn:
            row = await rel.get_release(conn, rid)
            if row is None:
                raise HTTPException(status_code=404, detail="Unknown release")
            runs = await rel.runs_of(conn, row)
            problem = None
            if row["status"] == "approved":
                problem = rel.wiring_problem(row) or await rel.check(conn, row, first_activation=True)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {**rel.serialise(row),
            "runs": {k: ({**rel.serialise({kk: vv for kk, vv in v.items() if kk != "details"}),
                          "summary": rel.run_summary(v.get("details"))} if v else None)
                     for k, v in runs.items()},
            "activation": None if problem is None else {"code": problem[0], "detail": problem[1]}}


class ReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    writer: Literal["a1", "t2", "t9_blog", "t9_social"]
    scope: Literal["platform", "tenant"] = "platform"
    tenant_id: Optional[UUID] = None
    rule_ids: list[UUID] = Field(default_factory=list, max_length=40)
    train_kind: Optional[Literal["regular", "emergency"]] = None
    emergency_reason: Optional[Literal["hard_stop_1", "hard_stop_2", "hard_stop_3"]] = None
    emergency_ref: Optional[UUID] = None
    notes: Optional[str] = Field(None, max_length=1000)


@router.post("/releases")
async def create_release(body: ReleaseCreate, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    from services.writing_memory import releases as rel
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.create_draft(
                    conn, writer=body.writer, scope=body.scope,
                    tenant_id=str(body.tenant_id) if body.tenant_id else None,
                    rule_ids=[str(r) for r in body.rule_ids], train_kind=body.train_kind,
                    emergency_reason=body.emergency_reason,
                    emergency_ref=str(body.emergency_ref) if body.emergency_ref else None,
                    notes=body.notes, created_by=f"admin:{actor}")
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


@router.post("/releases/reverify-scan")
async def reverify_scan(request: Request, x_admin_user_id: Optional[str] = Header(None)):
    from services.writing_memory import releases as rel
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                flagged = await rel.reverify_scan(conn, actor=f"admin:{actor}")
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"flagged": flagged}


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    r0_run_id: Optional[UUID] = None
    eval_run_id: Optional[UUID] = None
    shadow_run_id: Optional[UUID] = None


@router.post("/releases/{release_id}/evidence")
async def release_evidence(release_id: str, body: EvidenceBody, request: Request,
                           x_admin_user_id: Optional[str] = Header(None)):
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.attach_evidence(
                    conn, rid, actor=f"admin:{actor}",
                    **{k: str(v) for k, v in body.model_dump().items() if v is not None})
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_ref: Optional[str] = Field(None, max_length=300)


@router.post("/releases/{release_id}/approve")
async def approve_release(release_id: str, request: Request, body: Optional[ApproveBody] = None,
                          x_admin_user_id: Optional[str] = Header(None)):
    """The one human click of ADR 0010 decision 1: the approver is the signed-in admin (403 no_actor otherwise)."""
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.approve(conn, rid, approved_by=actor,
                                        approval_ref=body.approval_ref if body else None)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


@router.post("/releases/{release_id}/activate")
async def activate_release(release_id: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    """Move the writer's active pointer to this approved release. The API checks the evidence on the locked row
    (409 with a code: not_approved, content_not_wired, r0_missing, r0_not_byte_identical, eval_not_passed,
    shadow_missing, rule_isolation, train_closed, ...); the DB checks it again (409 db_refused)."""
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.activate(conn, rid, actor=f"admin:{actor}")
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


class ReleaseReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=400)


@router.post("/releases/{release_id}/rollback")
async def rollback_release(release_id: str, body: ReleaseReasonBody, request: Request,
                           x_admin_user_id: Optional[str] = Header(None)):
    """Roll the active release back to the one it superseded (no deploy). Returns the release now active."""
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.rollback(conn, rid, actor=f"admin:{actor}", reason=body.reason.strip())
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


@router.post("/releases/{release_id}/reject")
async def reject_release(release_id: str, body: ReleaseReasonBody, request: Request,
                         x_admin_user_id: Optional[str] = Header(None)):
    from services.writing_memory import releases as rel
    rid = _uuid(release_id, "release")
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = await rel.reject(conn, rid, actor=f"admin:{actor}", reason=body.reason.strip())
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    return rel.serialise(row)


@router.get("/writers")
async def writer_switches(request: Request):
    from services.writing_memory import releases as rel
    try:
        async with _pool(request).acquire() as conn:
            rows = await rel.writers(conn)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    return {"items": [rel.serialise(r) for r in rows]}


class WriterPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_enabled: Optional[bool] = None
    train_closed_month: Optional[date] = None


@router.patch("/writers/{writer}")
async def patch_writer(writer: str, body: WriterPatch, request: Request,
                       x_admin_user_id: Optional[str] = Header(None)):
    """The per-writer memory switch (kill switch) and the monthly train close."""
    from services.writing_memory import releases as rel
    if writer not in rel.WRITERS:
        raise HTTPException(status_code=404, detail="Unknown writer")
    if body.memory_enabled is None and body.train_closed_month is None:
        raise HTTPException(status_code=422, detail="give memory_enabled and / or train_closed_month")
    actor = f"admin:{_need_actor(x_admin_user_id)}"
    try:
        async with _pool(request).acquire() as conn:
            async with conn.transaction():
                row = None
                if body.train_closed_month is not None:
                    row = await rel.close_month(conn, writer, body.train_closed_month, actor=actor)
                if body.memory_enabled is not None:
                    row = await rel.set_memory_enabled(conn, writer, body.memory_enabled, actor=actor)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rel.ReleaseError as e:
        raise _release_error(e)
    logger.info("aa919_writer_switch", writer=writer, memory_enabled=body.memory_enabled,
                train_closed_month=str(body.train_closed_month) if body.train_closed_month else None)
    return rel.serialise(row)
