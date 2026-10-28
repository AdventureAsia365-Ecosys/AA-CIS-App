"""AA-919 WM-18 — writing-memory releases (app ADR 0010): drafts, evidence, approval, activation, rollback, the memory
switch, the monthly train close and the reverify scan, over shared.writing_memory_release / writing_memory_writer
(CIS 287).

The double check of ADR 0010 decision 1: `evidence_problem()` is the API's half — the same rules as the CHECK
`writing_memory_release_evidence_check` and the trigger `writing_memory_release_guard` (CIS 287), checked on the
locked row inside the write transaction, answered as a readable 409. The DB refuses the same activation even when the
API is bypassed or wrong (`DBRefused`, 409 `db_refused`: proof that the second layer fired).

Policy (not a DB rule, `wiring_problem`): `activate` refuses a class the writer does not render with 409
`content_not_wired` (WIRED_CLASSES). AA-920 wired `rules` and `repair_hint_only` for A1 (WM-23) and T9 (WM-24) through
the one builder of `insertion.py`, and `rules` for T2 (WM-25: the version records its stamp in `generation_meta`,
CIS 297, hidden from every tenant read by `_tenant_projection`). T2 does not take `repair_hint_only`: a hint-only
release changes no T2 writing slot and its repair passes (the shared flag-fix node, T3) have no test for one; a T2
repair hint goes live inside a `rules` release. `lexicon_only` is not rendered by any writer: its evidence is an R0
replay only (no R2 / shadow), which is not enough for a prompt change; a lexicon rule reaches T9 through L0 (`l0.py`)
or inside a `rules` release.

Rollback moves the active pointer back to the release the current one superseded (`previous_release_id`), with no
deploy: the current release becomes `retired` with `rollback: <reason>` and can never be re-activated (a fix is a new
release); the previous one goes from `retired` back to `active` on its own evidence (re-checked, never its rules'
current status: a rollback must not be blocked). Release 0 has no previous: 409 `no_previous` (the memory switch is
the answer there).

Every state change writes one `acp_shared.audit_log` row (ids, statuses, numbers; never text) in the same
transaction, last (the audit chain trigger holds a transaction lock, QRY-4).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

import structlog

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
STATUSES = ("draft", "evaluated", "approved", "active", "retired", "rejected")
CONTENT_CLASSES = ("empty", "lexicon_only", "repair_hint_only", "rules")
TRAIN_KINDS = ("baseline", "regular", "emergency", "lexicon")
EMERGENCY_REASONS = ("hard_stop_1", "hard_stop_2", "hard_stop_3")
REVERIFY_REASONS = ("golden_set", "model_route", "gate_taxonomy", "prompt_version_mismatch")
WRITER_STAGES = {"a1": "s1_generate", "t2": "t2_generate", "t9_blog": "t9_write", "t9_social": "t9_write"}
JUDGE_STAGE = "writing_memory_eval"          # WM-19's judge stage (fingerprinted when it exists)
GOLDEN_SET = "aa729"
MIN_R0_INPUTS = 20                           # WM-20: >= 20 stored inputs per writer
MAX_RULES = 40
# What each writer renders (AA-920: A1 = WM-23, T9 = WM-24, T2 = WM-25; see the module docstring).
WIRED_CLASSES = {"a1": frozenset({"empty", "rules", "repair_hint_only"}), "t2": frozenset({"empty", "rules"}),
                 "t9_blog": frozenset({"empty", "rules", "repair_hint_only"}),
                 "t9_social": frozenset({"empty", "rules", "repair_hint_only"})}
TRANSITIONS = {("draft", "evaluated"), ("draft", "rejected"), ("evaluated", "approved"), ("evaluated", "rejected"),
               ("evaluated", "draft"), ("approved", "active"), ("approved", "rejected"), ("active", "retired"),
               ("retired", "active")}
ROLLBACK_PREFIX = "rollback:"
ZERO_TENANT = "00000000-0000-0000-0000-000000000000"
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"   # the audit_log tenant of a platform release

_COLUMNS = """
    release_id::text AS release_id, writer, scope, tenant_id::text AS tenant_id, release_no, train_kind, content_class,
    rule_ids::text[] AS rule_ids, items_sha256, status, r0_run_id::text AS r0_run_id, eval_run_id::text AS eval_run_id,
    shadow_run_id::text AS shadow_run_id, previous_release_id::text AS previous_release_id, train_month,
    emergency_reason, emergency_ref::text AS emergency_ref, reverify, reverify_reasons, reverify_at,
    reverify_run_id::text AS reverify_run_id, reverify_approved_by, reverify_approved_at, fingerprint, approved_by,
    approved_at, approval_ref, activated_at, retired_at, retired_reason, rejected_by, rejected_reason, notes,
    created_by, created_at, updated_at
"""
_RUN_COLUMNS = ("id::text AS id, pipeline, writer, run_kind, verdict, stop_reason, finished_at, details, "
                "release_id::text AS release_id, tour_count, cost_usd")


class ReleaseError(Exception):
    """A refusal the API maps to an HTTP status with a code."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class DBRefused(ReleaseError):
    """The DB (CHECK or trigger) refused a change the API mirror let through: the second layer fired (409
    db_refused)."""


# ── pure rules (the API half of the double check) ──────────────────────────────────────────────────────────────────

def items_sha256(rules: list[dict]) -> str:
    """sha256 of the sorted (rule_id, rule_text, replacement) of a release; release 0 hashes an empty list."""
    items = sorted([str(r["rule_id"]), r.get("rule_text") or "", r.get("replacement")] for r in rules)
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def content_class_of(kinds: set[str]) -> str:
    if not kinds:
        return "empty"
    if kinds == {"lexicon"}:
        return "lexicon_only"
    if kinds == {"repair_hint"}:
        return "repair_hint_only"
    return "rules"


def train_kind_for(content_class: str, requested: Optional[str]) -> str:
    """The train kind a class implies (empty = baseline, lexicon_only = lexicon); otherwise the requested one
    (regular unless emergency)."""
    if content_class == "empty":
        return "baseline"
    if content_class == "lexicon_only":
        return "lexicon"
    return "emergency" if requested == "emergency" else "regular"


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _details(run: dict) -> dict:
    d = run.get("details")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d if isinstance(d, dict) else {}


def _run_problem(run: Optional[dict], writer: str, what: str, ok_kinds: dict[str, str]) -> Optional[tuple[str, str]]:
    if run is None:
        return f"{what}_missing", f"no {what} run"
    if run.get("pipeline") != "writing_memory" or run.get("writer") != writer:
        return f"{what}_wrong_writer", f"the {what} run is not a writing_memory run of writer {writer}"
    if run.get("finished_at") is None:
        return f"{what}_unfinished", f"the {what} run has not finished"
    kind, verdict = run.get("run_kind"), run.get("verdict")
    if ok_kinds.get(kind) != verdict:
        want = " or ".join(f"{k} {v}" for k, v in ok_kinds.items())
        return f"{what}_not_passed", f"the {what} run is {kind} {verdict}; it must be {want}"
    return None


def release_link_problem(run: Optional[dict], release: dict, what: str) -> Optional[tuple[str, str]]:
    """An eval (r2) or shadow run is evidence only for the release it ran (CIS 292 mirrors this): its release_id is
    the release's; an r2 run is not a seeded-fault operator check and not a code-train run (`candidate: current`). The
    baseline import (release 0's eval, no release) is exempt; r0 replays are per writer, not per release."""
    if run is None or what not in ("eval", "shadow"):
        return None
    if what == "eval" and run.get("run_kind") == "baseline_import":
        return None
    rid, mine = run.get("release_id"), release.get("release_id")
    if not rid or str(rid) != str(mine):
        whose = "no release (a code-train or operator run)" if not rid else "another release"
        return f"{what}_wrong_release", f"the {what} run evaluated {whose}; attach a {what} run of this release"
    if what == "eval" and run.get("run_kind") == "r2":
        d = _details(run)
        if d.get("seeded_fault"):
            return "eval_seeded_fault", "a seeded-fault run is an operator check of the stop, never evidence"
        if d.get("candidate", "release") != "release":
            return "eval_wrong_release", "the r2 run wrote today's writers (a code-train run), not this release"
    return None


def transition_problem(old: str, new: str, retired_reason: Optional[str] = None) -> Optional[tuple[str, str]]:
    if (old, new) not in TRANSITIONS:
        return "bad_transition", f"{old} -> {new} is not a release transition"
    if old == "retired" and new == "active" and (retired_reason or "").startswith(ROLLBACK_PREFIX):
        return "rolled_back", "a rolled-back release is never re-activated; draft a new release"
    return None


def evidence_problem(release: dict, runs: dict[str, Optional[dict]], rules: list[dict], *,
                     first_activation: bool = True, closed_month: Optional[date] = None,
                     today: Optional[date] = None) -> Optional[tuple[str, str]]:
    """Why `release` may not become active now, or None. Mirrors CIS 287 (CHECK + trigger): `runs` maps r0 / eval /
    shadow to the prompt_eval_runs row the release names (None when it names none or the row is missing); `rules`
    are the writing_rule rows of its rule_ids (as found)."""
    writer, cls = release["writer"], release["content_class"]
    old = release.get("status")
    if first_activation and old != "approved":
        return "not_approved", f"a {old} release is not approved"
    if not first_activation:
        p = transition_problem(old, "active", release.get("retired_reason"))
        if p:
            return p
    if not (release.get("approved_by") and release.get("approved_at")):
        return "not_approved", "the release has no approver"
    # the CHECK's presence rule, then the trigger's run rules
    need = {"empty": ("r0", "eval"), "lexicon_only": ("r0",), "repair_hint_only": ("eval", "shadow"),
            "rules": ("eval", "shadow")}[cls]
    for what in need:
        if not release.get(f"{what}_run_id"):
            return f"{what}_missing", f"a {cls} release needs its {what} run (activate requires eval, WM-18)"
    if "r0" in need:
        p = _run_problem(runs.get("r0"), writer, "r0", {"r0_replay": "pass"})
        if p:
            return p
        if cls == "empty":
            d = _details(runs["r0"])
            diffs, inputs = _num(d.get("byte_diffs")), _num(d.get("inputs"))
            if diffs != 0 or (inputs or 0) < MIN_R0_INPUTS:
                return "r0_not_byte_identical", (f"an empty release needs an R0 replay with 0 byte diffs over >= "
                                                 f"{MIN_R0_INPUTS} inputs (got {diffs} diffs, {inputs} inputs)")
    if "eval" in need:
        ok = {"r2": "pass", "baseline_import": "baseline"} if cls == "empty" else {"r2": "pass"}
        p = (_run_problem(runs.get("eval"), writer, "eval", ok)
             or release_link_problem(runs.get("eval"), release, "eval"))
        if p:
            return p
    if "shadow" in need:
        p = (_run_problem(runs.get("shadow"), writer, "shadow", {"shadow": "pass"})
             or release_link_problem(runs.get("shadow"), release, "shadow"))
        if p:
            return p
    ids = set(release.get("rule_ids") or [])
    found = {str(r["rule_id"]): r for r in rules}
    if ids:
        missing = sorted(ids - set(found))
        if missing:
            return "rule_missing", f"rules {missing[:3]} do not exist"
        for r in found.values():
            if (r["writer"] != writer or r["scope"] != release["scope"]
                    or (str(r["tenant_id"]) if r.get("tenant_id") else None) != release.get("tenant_id")):
                return "rule_isolation", (f"rule {r['rule_id']} is not a {writer} rule of this owner (a tenant "
                                          "release holds only its tenant's rules)")
        kinds = {r["kind"] for r in found.values()}
        if content_class_of(kinds) != cls:
            return "class_mismatch", f"a {cls} release cannot hold rules of kinds {sorted(kinds)}"
        if first_activation:
            unready = [r["rule_id"] for r in found.values() if r["status"] not in ("approved", "active")]
            if unready:
                return "rules_not_approved", f"{len(unready)} rules are not approved or active"
    if release.get("train_kind") == "regular" and first_activation:
        month = release.get("train_month") or (today or datetime.now(timezone.utc).date()).replace(day=1)
        if closed_month is not None and closed_month >= month:
            return "train_closed", f"the {month:%Y-%m} train of {writer} is closed"
    return None


def wiring_problem(release: dict) -> Optional[tuple[str, str]]:
    """Only a class the writer renders may go active (WIRED_CLASSES; T2 takes no repair-hint-only release,
    lexicon_only goes live for no writer)."""
    wired = WIRED_CLASSES.get(release["writer"], frozenset({"empty"}))
    if release["content_class"] not in wired:
        why = ("a lexicon-only release has R0 evidence only; use the L0 path or a rules release"
               if release["content_class"] == "lexicon_only"
               else "a T2 repair hint goes live inside a rules release" if release["writer"] == "t2"
               else "not wired")
        return "content_not_wired", f"{release['writer']} does not render {release['content_class']} releases ({why})"
    return None


def is_human(actor_id: Optional[str]) -> bool:
    """An approval is a human's: a shared.admin_users id (a UUID), or agent:<session> only through the release-0 CLI
    (empty class, with the human decision in approval_ref; ADR 0010 note)."""
    if not actor_id:
        return False
    try:
        UUID(str(actor_id))
        return True
    except ValueError:
        return False


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def serialise(row: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k in ("fingerprint", "details") and isinstance(v, str):
            v = json.loads(v)
        out[k] = v
    return out


async def get_release(conn, release_id: str, *, for_update: bool = False) -> Optional[dict]:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM shared.writing_memory_release WHERE release_id = $1::uuid"
                              + (" FOR UPDATE" if for_update else ""), str(release_id))
    return dict(row) if row else None


async def list_releases(conn, *, writer: Optional[str] = None, status: Optional[str] = None,
                        scope: Optional[str] = None, tenant_id: Optional[str] = None, limit: int = 50,
                        offset: int = 0) -> tuple[list[dict], int]:
    where, args = ["true"], []
    for col, val in (("writer", writer), ("status", status), ("scope", scope)):
        if val:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    if tenant_id:
        args.append(str(tenant_id))
        where.append(f"tenant_id = ${len(args)}::uuid")       # explicit owner filter (ADR 0010 decision 10)
    w = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM shared.writing_memory_release WHERE {w}", *args)
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM shared.writing_memory_release WHERE {w} "
                            f"ORDER BY writer, scope, tenant_id NULLS FIRST, release_no DESC LIMIT {int(limit)} "
                            f"OFFSET {int(offset)}", *args)
    return [dict(r) for r in rows], int(total or 0)


async def active_releases(conn) -> list[dict]:
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM shared.writing_memory_release WHERE status = 'active' "
                            "ORDER BY writer, scope, tenant_id NULLS FIRST")
    return [dict(r) for r in rows]


async def writers(conn) -> list[dict]:
    return [dict(r) for r in await conn.fetch(
        "SELECT writer, memory_enabled, train_closed_month, updated_by, updated_at FROM shared.writing_memory_writer "
        "ORDER BY writer")]


async def runs_of(conn, release: dict) -> dict[str, Optional[dict]]:
    out: dict[str, Optional[dict]] = {}
    for what in ("r0", "eval", "shadow"):
        rid = release.get(f"{what}_run_id")
        row = await conn.fetchrow(f"SELECT {_RUN_COLUMNS} FROM shared.prompt_eval_runs WHERE id = $1::uuid",
                                  rid) if rid else None
        out[what] = dict(row) if row else None
    return out


# The run `details` keys the Releases tab may show (AA-919 WM-22): counts, hashes and our own reason strings, never
# per-input rows or tenant text. `results` (baseline_import) is a {mode:verdict: count} map; `reasons` (r0_replay)
# are the verdict reasons release0 writes ("3 byte differences").
SUMMARY_NUMBERS = ("inputs", "errors", "byte_diffs", "byte_diffs_none_vs_r0", "byte_diffs_vs_pre", "compared_with_pre",
                   "stored_hashes", "stored_hash_match", "distinct_system_prompts", "skipped_post_edit",
                   "source_cost_usd", "cost_usd")
SUMMARY_STRINGS = ("prompt_sha256", "git_sha", "source")


def run_summary(details: Any) -> dict[str, Any]:
    """The part of a run's `details` the admin page shows: whitelisted numbers / short strings, the `results` count map
    and the `reasons` list (both shape-checked). Everything else (per-input rows, keys, sources) stays out."""
    d = _details({"details": details})
    out: dict[str, Any] = {}
    for k in SUMMARY_NUMBERS:
        if _num(d.get(k)) is not None:
            out[k] = d[k]
    for k in SUMMARY_STRINGS:
        if isinstance(d.get(k), str) and len(d[k]) <= 80:
            out[k] = d[k]
    res = d.get("results")
    if isinstance(res, dict):
        out["results"] = {str(k)[:40]: v for k, v in res.items() if _num(v) is not None}
    reasons = d.get("reasons")
    if isinstance(reasons, list):
        out["reasons"] = [str(r)[:200] for r in reasons if isinstance(r, str)][:10]
    return out


async def run_verdicts(conn, run_ids: list[str]) -> dict[str, dict]:
    """{run id: {run_kind, verdict, finished}} for the evidence runs a page of releases names (one query)."""
    ids = sorted({str(r) for r in run_ids if r})
    if not ids:
        return {}
    rows = await conn.fetch("SELECT id::text AS id, run_kind, verdict, finished_at IS NOT NULL AS finished "
                            "FROM shared.prompt_eval_runs WHERE id = ANY($1::uuid[])", ids)
    return {r["id"]: {"run_kind": r["run_kind"], "verdict": r["verdict"], "finished": r["finished"]} for r in rows}


def evidence_of(release: dict, verdicts: dict[str, dict]) -> dict[str, Optional[dict]]:
    """Per evidence slot (r0 / eval / shadow): None when the release names no run, else the run's kind and verdict
    (`missing: true` when the named run is not found)."""
    out: dict[str, Optional[dict]] = {}
    for what in ("r0", "eval", "shadow"):
        rid = release.get(f"{what}_run_id")
        out[what] = None if not rid else (verdicts.get(str(rid)) or {"run_kind": None, "verdict": None,
                                                                     "finished": False, "missing": True})
    return out


async def _rules(conn, rule_ids: list[str]) -> list[dict]:
    if not rule_ids:
        return []
    rows = await conn.fetch("SELECT rule_id::text AS rule_id, kind, scope, tenant_id::text AS tenant_id, writer, "
                            "status, rule_text, replacement FROM shared.writing_rule WHERE rule_id = ANY($1::uuid[])",
                            list(rule_ids))
    return [dict(r) for r in rows]


async def _closed_month(conn, writer: str) -> Optional[date]:
    return await conn.fetchval("SELECT train_closed_month FROM shared.writing_memory_writer WHERE writer = $1", writer)


async def check(conn, release: dict, *, first_activation: bool = True) -> Optional[tuple[str, str]]:
    """The API mirror on a release row (read its runs, rules and the writer's closed month)."""
    return evidence_problem(release, await runs_of(conn, release), await _rules(conn, release.get("rule_ids") or []),
                            first_activation=first_activation,
                            closed_month=await _closed_month(conn, release["writer"]))


# ── fingerprint and reverify ───────────────────────────────────────────────────────────────────────────────────────

async def current_fingerprint(conn, writer: str, *, prompt_sha256: Optional[str] = None) -> dict:
    """What a reverify compares: the active golden set, the writer's stage route and the judge route, the gate
    taxonomy version (and, when known, the writer's system-prompt sha256: the prompt register)."""
    from services.quality.registry import TAXONOMY_VERSION
    fp: dict[str, Any] = {"gate_taxonomy": TAXONOMY_VERSION}
    gs = await conn.fetchrow("SELECT name, version FROM shared.eval_golden_set WHERE name = $1 AND status = 'active'",
                             GOLDEN_SET)
    fp["golden_set"] = f"{gs['name']}@{gs['version']}" if gs else None
    for key, stage in (("writer_route", WRITER_STAGES[writer]), ("judge_route", JUDGE_STAGE)):
        r = await conn.fetchrow("SELECT model_id, fallback_model_ids, account_route FROM shared.llm_role_config "
                                "WHERE stage = $1", stage)
        fp[key] = None if r is None else {"stage": stage, "model_id": r["model_id"],
                                          "fallbacks": list(r["fallback_model_ids"] or []),
                                          "account_route": r["account_route"]}
    if prompt_sha256:
        fp["prompt_sha256"] = prompt_sha256
    return fp


def reverify_reasons(stored: dict, now: dict) -> list[str]:
    """Which of golden set / model route / gate taxonomy changed since the release's fingerprint (missing keys in
    the stored fingerprint are not a change)."""
    out = []
    if "golden_set" in stored and stored.get("golden_set") != now.get("golden_set"):
        out.append("golden_set")
    if any(k in stored and stored.get(k) != now.get(k) for k in ("writer_route", "judge_route")):
        out.append("model_route")
    if "gate_taxonomy" in stored and stored.get("gate_taxonomy") != now.get("gate_taxonomy"):
        out.append("gate_taxonomy")
    return out


async def reverify_scan(conn, *, actor: str = "system:reverify_scan") -> list[dict]:
    """Flag every active release whose fingerprint no longer matches (it keeps serving; a re-run needs approval,
    WM-18 scope 5). Returns the flagged releases {release_id, writer, reasons}. Callers: the golden-set importer
    after a supersede, the llm-config route PATCHes, the worker start, POST /releases/reverify-scan."""
    flagged = []
    for rel in await active_releases(conn):
        stored = rel["fingerprint"] if isinstance(rel["fingerprint"], dict) else json.loads(rel["fingerprint"] or "{}")
        reasons = reverify_reasons(stored, await current_fingerprint(conn, rel["writer"]))
        new = sorted(set(reasons) - set(rel["reverify_reasons"] or []))
        if not new:
            continue
        await conn.execute(
            "UPDATE shared.writing_memory_release SET reverify = true, reverify_at = coalesce(reverify_at, now()), "
            "reverify_reasons = (SELECT array_agg(DISTINCT x ORDER BY x) "
            "FROM unnest(reverify_reasons || $2::text[]) x) "
            "WHERE release_id = $1::uuid", rel["release_id"], new)
        await _audit(conn, rel, actor, "reverify_flagged", {"reasons": new})
        flagged.append({"release_id": rel["release_id"], "writer": rel["writer"], "reasons": new})
    if flagged:
        logger.warning("wm_release_reverify_flagged", releases=flagged)
    return flagged


async def reverify_scan_safe(pool, *, actor: str) -> list[dict]:
    """reverify_scan in its own transaction, never raising (a caller's main action must not fail on it)."""
    try:
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('shared.writing_memory_release') IS NOT NULL"):
                return []
            async with conn.transaction():
                return await reverify_scan(conn, actor=actor)
    except Exception as e:  # noqa: BLE001
        logger.warning("wm_release_reverify_scan_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return []


# ── writes ─────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _audit(conn, release: dict, actor: str, what: str, details: Optional[dict] = None) -> None:
    from services.acp_shared.audit_log import write_audit_log
    await write_audit_log(
        conn, tenant_id=release.get("tenant_id") or PLATFORM_TENANT_ID, actor=actor[:128],
        action=f"writing_memory.release_{what}", resource_type="writing_memory_release",
        resource_id=str(release["release_id"]),
        details={"writer": release["writer"], "scope": release["scope"], "release_no": release["release_no"],
                 "content_class": release["content_class"], **(details or {})})


def _db_refused(e: Exception) -> DBRefused:
    return DBRefused(409, "db_refused", f"{type(e).__name__}: {str(e)[:300]}")


async def _open_hard_stop(conn, writer: str, scope: str, tenant_id: Optional[str], ref: str) -> bool:
    """An emergency release needs a hard stop open on the owner's active release: `ref` is a finished
    writing_memory run of that release that stopped on a hard stop (critic 20)."""
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM shared.prompt_eval_runs p JOIN shared.writing_memory_release r "
        "ON r.release_id = p.release_id WHERE p.id = $1::uuid AND p.stop_reason = 'hard_stop' "
        "AND p.finished_at IS NOT NULL AND r.status = 'active' AND r.writer = $2 AND r.scope = $3 "
        "AND r.tenant_id IS NOT DISTINCT FROM $4::uuid)", ref, writer, scope, tenant_id))


async def create_draft(conn, *, writer: str, scope: str = "platform", tenant_id: Optional[str] = None,
                       rule_ids: Optional[list[str]] = None, train_kind: Optional[str] = None,
                       emergency_reason: Optional[str] = None, emergency_ref: Optional[str] = None,
                       notes: Optional[str] = None, created_by: str, prompt_sha256: Optional[str] = None) -> dict:
    """A new draft: its content class from its rules' kinds, its items hash, the next number for the writer and
    owner (0 for the first, which must be empty), the current fingerprint. Rules are read with the owner filter."""
    if writer not in WRITERS:
        raise ReleaseError(422, "invalid", f"writer must be one of {list(WRITERS)}")
    if (scope == "platform") != (tenant_id is None) or scope not in ("platform", "tenant"):
        raise ReleaseError(422, "invalid", "a platform release has no tenant_id; a tenant release has one")
    ids = sorted({str(UUID(str(r))) for r in (rule_ids or [])})
    if len(ids) > MAX_RULES:
        raise ReleaseError(422, "invalid", f"at most {MAX_RULES} rules in a release")
    rules = await _rules(conn, ids)
    bad = [r["rule_id"] for r in rules if r["writer"] != writer or r["scope"] != scope or r["tenant_id"] != tenant_id]
    if len(rules) != len(ids) or bad:
        raise ReleaseError(409, "rule_isolation", "every rule must exist and be a rule of this writer and owner")
    cls = content_class_of({r["kind"] for r in rules})
    kind = train_kind_for(cls, train_kind)
    if kind == "emergency":
        if emergency_reason not in EMERGENCY_REASONS or not emergency_ref:
            raise ReleaseError(422, "invalid", f"an emergency release needs emergency_reason in "
                                               f"{list(EMERGENCY_REASONS)} and emergency_ref")
        if not await _open_hard_stop(conn, writer, scope, tenant_id, emergency_ref):
            raise ReleaseError(409, "no_hard_stop", "an emergency release only clears a hard stop open on the active "
                                                   "release (emergency_ref: the run that stopped on it)")
    else:
        emergency_reason = emergency_ref = None
    nxt = await conn.fetchval(
        "SELECT coalesce(max(release_no) + 1, 0) FROM shared.writing_memory_release WHERE writer = $1 AND scope = $2 "
        "AND coalesce(tenant_id, $3::uuid) = coalesce($4::uuid, $3::uuid)", writer, scope, ZERO_TENANT, tenant_id)
    if nxt == 0 and cls != "empty":
        raise ReleaseError(409, "release0_first", "release 0 of a writer and owner is the empty baseline (WM-20)")
    fp = await current_fingerprint(conn, writer, prompt_sha256=prompt_sha256)
    try:
        row = await conn.fetchrow(
            f"""INSERT INTO shared.writing_memory_release (writer, scope, tenant_id, release_no, train_kind,
                       content_class, rule_ids, items_sha256, emergency_reason, emergency_ref, fingerprint, notes,
                       created_by)
                VALUES ($1, $2, $3::uuid, $4, $5, $6, $7::uuid[], $8, $9, $10::uuid, $11::jsonb, $12, $13)
                RETURNING {_COLUMNS}""",
            writer, scope, tenant_id, int(nxt), kind, cls, ids, items_sha256(rules), emergency_reason, emergency_ref,
            json.dumps(fp), notes, created_by[:128])
    except Exception as e:  # noqa: BLE001 — asyncpg integrity errors: the DB layer refused
        if type(e).__name__ in ("CheckViolationError", "RaiseError", "RestrictViolationError", "UniqueViolationError",
                                "ForeignKeyViolationError"):
            raise _db_refused(e)
        raise
    rel = dict(row)
    await _audit(conn, rel, created_by, "created", {"rules": len(ids), "train_kind": kind})
    return rel


async def attach_evidence(conn, release_id: str, *, r0_run_id: Optional[str] = None,
                          eval_run_id: Optional[str] = None, shadow_run_id: Optional[str] = None, actor: str) -> dict:
    """Name the release's evidence runs (draft / evaluated only); it becomes `evaluated` once the runs its class
    needs are all named. Here: an eval / shadow run must be a run OF this release (release_link_problem, 409); kind,
    verdict and finish are checked at activation (API mirror + the CIS 287 / 292 trigger)."""
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    if rel["status"] not in ("draft", "evaluated"):
        raise ReleaseError(409, "not_editable", f"a {rel['status']} release keeps its evidence")
    new = {"r0_run_id": r0_run_id or rel["r0_run_id"], "eval_run_id": eval_run_id or rel["eval_run_id"],
           "shadow_run_id": shadow_run_id or rel["shadow_run_id"]}
    for what, rid in (("eval", eval_run_id), ("shadow", shadow_run_id)):
        if rid:                          # a readable early refusal; activation re-checks it (API + CIS 292)
            row = await conn.fetchrow(f"SELECT {_RUN_COLUMNS} FROM shared.prompt_eval_runs WHERE id = $1::uuid", rid)
            if row is None:
                raise ReleaseError(409, f"{what}_missing", f"no such {what} run")
            p = release_link_problem(dict(row), rel, what)
            if p:
                raise ReleaseError(409, *p)
    need = {"empty": ("r0", "eval"), "lexicon_only": ("r0",), "repair_hint_only": ("eval", "shadow"),
            "rules": ("eval", "shadow")}[rel["content_class"]]
    complete = all(new[f"{w}_run_id"] for w in need)
    try:
        row = await conn.fetchrow(
            f"""UPDATE shared.writing_memory_release SET r0_run_id = $2::uuid, eval_run_id = $3::uuid,
                       shadow_run_id = $4::uuid, status = CASE WHEN $5 THEN 'evaluated' ELSE status END
                 WHERE release_id = $1::uuid RETURNING {_COLUMNS}""",
            str(release_id), new["r0_run_id"], new["eval_run_id"], new["shadow_run_id"],
            complete and rel["status"] == "draft")
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("ForeignKeyViolationError", "CheckViolationError", "RestrictViolationError"):
            raise _db_refused(e)
        raise
    out = dict(row)
    await _audit(conn, out, actor, "evidence", {"status": out["status"], **{k: v for k, v in new.items() if v}})
    return out


async def approve(conn, release_id: str, *, approved_by: str, approval_ref: Optional[str] = None,
                  allow_agent: bool = False) -> dict:
    """evaluated -> approved: one human's click (ADR 0010 decision 1). `allow_agent` (the release-0 CLI only, empty
    class, with the human decision in approval_ref) is the ADR 0010 note's exception."""
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    agent = allow_agent and str(approved_by).startswith("agent:") and rel["content_class"] == "empty" \
        and bool(approval_ref)
    if not (is_human(approved_by) or agent):
        raise ReleaseError(403, "no_actor", "an approval needs a signed-in admin (never a job)")
    if rel["status"] != "evaluated":
        raise ReleaseError(409, "not_evaluated", f"a {rel['status']} release cannot be approved (evaluate it first)")
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_memory_release SET status = 'approved', approved_by = $2, approved_at = now(),
                   approval_ref = $3 WHERE release_id = $1::uuid RETURNING {_COLUMNS}""",
        str(release_id), str(approved_by)[:128], (approval_ref or None) and approval_ref[:300])
    out = dict(row)
    await _audit(conn, out, f"admin:{approved_by}" if is_human(approved_by) else str(approved_by), "approved",
                 {"approval_ref": approval_ref})
    return out


async def reject(conn, release_id: str, *, actor: str, reason: str) -> dict:
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    if rel["status"] not in ("draft", "evaluated", "approved"):
        raise ReleaseError(409, "not_rejectable", f"a {rel['status']} release cannot be rejected")
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_memory_release SET status = 'rejected', rejected_by = $2, rejected_reason = $3
             WHERE release_id = $1::uuid RETURNING {_COLUMNS}""", str(release_id), actor[:128], reason[:500])
    out = dict(row)
    await _audit(conn, out, actor, "rejected")
    return out


async def _active_of(conn, rel: dict, *, for_update: bool = True) -> Optional[dict]:
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM shared.writing_memory_release WHERE writer = $1 AND scope = $2 "
        "AND tenant_id IS NOT DISTINCT FROM $3::uuid AND status = 'active'" + (" FOR UPDATE" if for_update else ""),
        rel["writer"], rel["scope"], rel.get("tenant_id"))
    return dict(row) if row else None


async def activate(conn, release_id: str, *, actor: str, enforce_wiring: bool = True) -> dict:
    """approved -> active, in the caller's transaction: lock the target and the current active release of the same
    writer and owner, check the evidence (the API mirror), retire the current one (`superseded`) and activate the
    target with previous_release_id = the current one. The DB checks the same (CHECK + trigger)."""
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    if rel["status"] != "approved":
        raise ReleaseError(409, "not_approved", f"a {rel['status']} release cannot be activated (approve it first)")
    if enforce_wiring:
        p = wiring_problem(rel)
        if p:
            raise ReleaseError(409, *p)
    p = await check(conn, rel, first_activation=True)
    if p:
        raise ReleaseError(409, *p)
    current = await _active_of(conn, rel)
    try:
        if current:
            await conn.execute("UPDATE shared.writing_memory_release SET status = 'retired', "
                               "retired_reason = 'superseded' WHERE release_id = $1::uuid", current["release_id"])
        row = await conn.fetchrow(
            f"""UPDATE shared.writing_memory_release SET status = 'active', previous_release_id = $2::uuid
                 WHERE release_id = $1::uuid RETURNING {_COLUMNS}""",
            str(release_id), current["release_id"] if current else rel["previous_release_id"])
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("CheckViolationError", "RaiseError", "RestrictViolationError", "UniqueViolationError"):
            raise _db_refused(e)
        raise
    out = dict(row)
    await _audit(conn, out, actor, "activated",
                 {"previous_release_id": out["previous_release_id"],
                  "superseded_release_no": current["release_no"] if current else None})
    logger.info("wm_release_activated", release_id=out["release_id"], writer=out["writer"], scope=out["scope"],
                release_no=out["release_no"], previous=out["previous_release_id"])
    return out


async def rollback(conn, release_id: str, *, actor: str, reason: str) -> dict:
    """Move the pointer back: the active release -> retired (`rollback: <reason>`), its previous release -> active
    (on its own evidence). Returns the release that is active now. 409 no_previous for a release without one."""
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    if rel["status"] != "active":
        raise ReleaseError(409, "not_active", f"a {rel['status']} release is not the active one")
    prev = await get_release(conn, rel["previous_release_id"], for_update=True) if rel["previous_release_id"] else None
    if prev is None:
        raise ReleaseError(409, "no_previous", "this release has no previous release to roll back to (release 0: "
                                               "switch memory off instead)")
    p = transition_problem(prev["status"], "active", prev.get("retired_reason"))
    if p:
        raise ReleaseError(409, *p)
    p = await check(conn, prev, first_activation=False)
    if p:
        raise ReleaseError(409, *p)
    try:
        await conn.execute("UPDATE shared.writing_memory_release SET status = 'retired', retired_reason = $2 "
                           "WHERE release_id = $1::uuid", str(release_id), f"{ROLLBACK_PREFIX} {reason}"[:500])
        row = await conn.fetchrow(f"UPDATE shared.writing_memory_release SET status = 'active' "
                                  f"WHERE release_id = $1::uuid RETURNING {_COLUMNS}", prev["release_id"])
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("CheckViolationError", "RaiseError", "RestrictViolationError", "UniqueViolationError"):
            raise _db_refused(e)
        raise
    out = dict(row)
    await _audit(conn, rel, actor, "rolled_back", {"to_release_id": out["release_id"],
                                                     "to_release_no": out["release_no"]})
    logger.info("wm_release_rolled_back", writer=rel["writer"], scope=rel["scope"], from_no=rel["release_no"],
                to_no=out["release_no"])
    return out


async def retire_without_previous(conn, release_id: str, *, actor: str, reason: str) -> dict:
    """active -> retired (`rollback: <reason>`, never re-activated) with nothing put in its place: the owner then
    writes with no release of its own (a tenant: the platform release only). The post-activation check's fallback
    for a TENANT release that superseded nothing (409 no_previous from `rollback`). A platform release always has
    one (release 0); without it the check turns the writer's memory off instead."""
    rel = await get_release(conn, release_id, for_update=True)
    if rel is None:
        raise ReleaseError(404, "not_found", "Unknown release")
    if rel["status"] != "active":
        raise ReleaseError(409, "not_active", f"a {rel['status']} release is not the active one")
    if rel["scope"] != "tenant" or rel["previous_release_id"]:
        raise ReleaseError(409, "use_rollback", "only a tenant release with no previous release is retired alone")
    try:
        row = await conn.fetchrow(f"UPDATE shared.writing_memory_release SET status = 'retired', retired_reason = $2 "
                                  f"WHERE release_id = $1::uuid RETURNING {_COLUMNS}", str(release_id),
                                  f"{ROLLBACK_PREFIX} {reason}"[:500])
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("CheckViolationError", "RaiseError", "RestrictViolationError"):
            raise _db_refused(e)
        raise
    out = dict(row)
    await _audit(conn, rel, actor, "retired_alone", {"reason": "rollback"})
    logger.info("wm_release_retired_alone", writer=rel["writer"], scope=rel["scope"], release_no=rel["release_no"])
    return out


async def set_memory_enabled(conn, writer: str, enabled: bool, *, actor: str) -> dict:
    """The kill switch: memory on needs an active platform release (the trigger checks it too)."""
    if writer not in WRITERS:
        raise ReleaseError(404, "not_found", "Unknown writer")
    if enabled and not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM shared.writing_memory_release WHERE writer = $1 AND scope = 'platform' "
            "AND status = 'active')", writer):
        raise ReleaseError(409, "no_active_release", f"memory for {writer} needs an active platform release (release 0 "
                                                     "first, WM-20)")
    try:
        row = await conn.fetchrow("UPDATE shared.writing_memory_writer SET memory_enabled = $2, updated_by = $3 "
                                  "WHERE writer = $1 RETURNING writer, memory_enabled, train_closed_month, updated_by, "
                                  "updated_at", writer, enabled, actor[:128])
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ in ("CheckViolationError", "RaiseError"):
            raise _db_refused(e)
        raise
    from services.acp_shared.audit_log import write_audit_log
    await write_audit_log(conn, tenant_id=PLATFORM_TENANT_ID, actor=actor[:128],
                          action="writing_memory.memory_switch", resource_type="writing_memory_writer",
                          resource_id=writer, details={"memory_enabled": enabled})
    return dict(row)


async def close_month(conn, writer: str, month: date, *, actor: str) -> dict:
    """Close the monthly train of a writer up to `month` (the first of a month): no regular release of that month or
    earlier activates any more. L0 lexicon, emergency releases and rollbacks are never blocked by the train."""
    if writer not in WRITERS:
        raise ReleaseError(404, "not_found", "Unknown writer")
    month = month.replace(day=1)
    row = await conn.fetchrow("UPDATE shared.writing_memory_writer SET train_closed_month = greatest("
                              "coalesce(train_closed_month, $2::date), $2::date), updated_by = $3 WHERE writer = $1 "
                              "RETURNING writer, memory_enabled, train_closed_month, updated_by, updated_at",
                              writer, month, actor[:128])
    from services.acp_shared.audit_log import write_audit_log
    await write_audit_log(conn, tenant_id=PLATFORM_TENANT_ID, actor=actor[:128],
                          action="writing_memory.train_closed", resource_type="writing_memory_writer",
                          resource_id=writer, details={"month": month.isoformat()})
    return dict(row)


__all__ = ["CONTENT_CLASSES", "DBRefused", "EMERGENCY_REASONS", "MIN_R0_INPUTS", "ReleaseError", "STATUSES",
           "TRAIN_KINDS", "WIRED_CLASSES", "WRITERS", "WRITER_STAGES", "activate", "active_releases", "approve",
           "attach_evidence", "check", "close_month", "content_class_of", "create_draft", "current_fingerprint",
           "evidence_problem", "get_release", "is_human", "items_sha256", "list_releases", "reject",
           "release_link_problem", "retire_without_previous", "reverify_reasons", "reverify_scan",
           "reverify_scan_safe", "rollback", "serialise",
           "set_memory_enabled", "transition_problem", "wiring_problem", "writers"]
