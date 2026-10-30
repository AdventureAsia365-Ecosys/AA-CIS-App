"""AA-922 WM-30 — the weekly writing-memory KPI job: measure the loop, roll back on a soft stop, expire stale rules
(app ADR 0010; S245 audit §8; CIS 314). $0: read-only SQL over the tables that already exist, no model call.

KPIs (per writer, overall and per tenant id; counts first, rates derived, so tenants add up to the writer):
  writes              T9: content_piece approved / held of the writer's channel; T2: AI tour versions; A1:
                      generated_content.
  reflag_rate         T9: share of writes a blocking gate flagged (gate_ledger), with its Wilson 95% interval.
  repeat_error_rate   T9: share of writes flagged by a gate that also flagged the same owner in the 28 days before.
  light_edit_share    T9 labels (content_piece_label) as_is / light over every label of the week.
  minutes_to_fix      mean minutes_to_fix of those labels.
  tenant_edit_rate    tenant hand edits (writing_signal tenant_edit) per write.
  capture_complete    admin text / review signals with a reason code over all of them (target >= 80%).
  cost_per_write      llm_call_log cost of the writes' jobs (T9 / T2) or of the s1_generate stage (A1) per write.
  replay_hit_rate     human edits the writer's rules' R0 replays explain over the usable edits they read.
  funnel              rules drafted this week per status, and every rule per status.
  clean_pairs         human before / after pairs that passed the fact filter (all time): the fine-tune door stays
                      visible; there is no export path.
  learning_spend      the month's learning-loop spend against the $25 cap (budget.month_spend).
A writer / tenant without data gets None for a rate (never 0): "not measured" is not "no problem".

Soft stops (`soft_stops`, pure): for each active release with content, its writes since activation against the 28 days
before it (the same owner): reflag_over_8pct (Wilson lower bound of the re-flag rate > 8%, >= 20 writes after),
light_edit_drop (share down >= 10 points, >= 5 labels each side), minutes_to_fix_up (+20%, >= 5 labels each side),
tenant_edit_up (+20% and >= 5 points, >= 20 writes each side; tenant writers), cost_up (+20%, >= 20 writes each side).
A soft stop takes the release out of service (post_activation.move_pointer: rollback, else retire / memory off) unless
a human recorded a keep for it (shared.writing_memory_keep: the codes, or all, until kept_until). DRY RUN reports what
it would do and changes nothing. Dev has no real traffic: no grace window; the minimum samples are the guard.

Rule expiry (`expire_rules`): candidates / approved past expires_at expire as before (rules.expire_stale); an ACTIVE
rule past expires_at is re-confirmed (expires_at + 60 days) when a human pass signal of its owner joined its cluster
after it was approved (for a preference summary: any human pass signal of that tenant and writer), else it expires
(the release that holds it keeps serving the approved text until the next release, which cannot hold it).

Project stop (P3 rule, `project_stop`): a writer whose two latest regular releases both showed no improvement (re-flag,
else tenant edit rate, after vs before activation) is flagged; the report says so, nothing is switched off.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import asyncpg
import structlog

from services.writing_memory import config as cfg

logger = structlog.get_logger()

WRITERS = ("a1", "t2", "t9_blog", "t9_social")
TENANT_WRITERS = ("t2", "t9_blog", "t9_social")
SOFT_STOP_CODES = ("reflag_over_8pct", "light_edit_drop", "minutes_to_fix_up", "tenant_edit_up", "cost_up")
REFLAG_MAX = 0.08
LIGHT_DROP_POINTS = 0.10
MINUTES_UP = 0.20
TENANT_EDIT_UP = 0.20
TENANT_EDIT_MIN_POINTS = 0.05
COST_UP = 0.20
MIN_WRITES = 20
MIN_LABELS = 5
BASELINE_DAYS = 28
REPEAT_DAYS = 28
CAPTURE_TARGET = 0.80
KEEP_MAX_DAYS = 30
ADMIN_ACTIONS = ("sentence_cut", "sentence_rewrite", "sentence_manual", "field_edit", "reject", "dismiss",
                 "regenerate")
COUNT_KEYS = ("writes", "flagged", "repeat_flagged", "labels", "light", "minutes_sum", "minutes_n", "tenant_edits",
              "admin_signals", "admin_with_reason", "cost_usd")


# ── pure math ──────────────────────────────────────────────────────────────────────────────────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:
    """The Wilson score interval of k successes in n (None for n = 0)."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def _rate(k, n) -> Optional[float]:
    return round(k / n, 4) if n else None


def empty_counts() -> dict:
    return {k: 0 for k in COUNT_KEYS} | {"cost_usd": 0.0}


def add_counts(a: dict, b: dict) -> dict:
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in COUNT_KEYS}


def derive(c: dict, writer: str) -> dict:
    """Rates from counts. T9 measures re-flag, repeats and labels; T2 / A1 leave them None (not measured)."""
    t9 = writer.startswith("t9")
    w = int(c.get("writes") or 0)
    ci = wilson(int(c.get("flagged") or 0), w) if t9 else None
    out = {**{k: c.get(k) for k in COUNT_KEYS},
           "reflag_rate": _rate(c.get("flagged", 0), w) if t9 else None,
           "reflag_ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
           "repeat_error_rate": _rate(c.get("repeat_flagged", 0), w) if t9 else None,
           "light_edit_share": _rate(c.get("light", 0), c.get("labels", 0)) if t9 else None,
           "minutes_to_fix": (round(c["minutes_sum"] / c["minutes_n"], 2) if t9 and c.get("minutes_n") else None),
           "tenant_edit_rate": _rate(c.get("tenant_edits", 0), w) if writer != "a1" else None,
           "capture_complete": _rate(c.get("admin_with_reason", 0), c.get("admin_signals", 0)),
           "cost_per_write": round(float(c.get("cost_usd") or 0) / w, 6) if w else None}
    out["capture_ok"] = None if out["capture_complete"] is None else out["capture_complete"] >= CAPTURE_TARGET
    return out


def soft_stops(writer: str, scope: str, before: dict, after: dict) -> list[dict]:
    """The soft stops a release's writes since activation (`after`, derived) show against the 28 days before
    (`before`, derived). Each: {code, value, baseline, n}. Below the minimum samples a stop never fires."""
    out: list[dict] = []
    wa, wb = int(after.get("writes") or 0), int(before.get("writes") or 0)
    if writer.startswith("t9") and wa >= MIN_WRITES:
        ci = wilson(int(after.get("flagged") or 0), wa)
        if ci and ci[0] > REFLAG_MAX:
            out.append({"code": "reflag_over_8pct", "value": after.get("reflag_rate"), "lower95": round(ci[0], 4),
                        "baseline": before.get("reflag_rate"), "n": wa})
    la, lb = int(after.get("labels") or 0), int(before.get("labels") or 0)
    if la >= MIN_LABELS and lb >= MIN_LABELS:
        sa, sb = after.get("light_edit_share"), before.get("light_edit_share")
        if sa is not None and sb is not None and sb - sa >= LIGHT_DROP_POINTS:
            out.append({"code": "light_edit_drop", "value": sa, "baseline": sb, "n": la})
        ma, mb = after.get("minutes_to_fix"), before.get("minutes_to_fix")
        if ma is not None and mb and ma > mb * (1 + MINUTES_UP):
            out.append({"code": "minutes_to_fix_up", "value": ma, "baseline": mb, "n": la})
    if wa >= MIN_WRITES and wb >= MIN_WRITES:
        ta, tb = after.get("tenant_edit_rate"), before.get("tenant_edit_rate")
        if (writer in TENANT_WRITERS and ta is not None and tb is not None and ta > tb * (1 + TENANT_EDIT_UP)
                and ta - tb >= TENANT_EDIT_MIN_POINTS):
            out.append({"code": "tenant_edit_up", "value": ta, "baseline": tb, "n": wa})
        ca, cb = after.get("cost_per_write"), before.get("cost_per_write")
        if ca is not None and cb and ca > cb * (1 + COST_UP):
            out.append({"code": "cost_up", "value": ca, "baseline": cb, "n": wa})
    return out


def covered(stops: list[dict], keeps: list[dict], today: date) -> tuple[list[dict], list[dict]]:
    """(uncovered stops, kept stops): a keep valid today covers its codes (empty = every code)."""
    live = [k for k in keeps if k["kept_until"] >= today]
    kept, rest = [], []
    for s in stops:
        (kept if any(not k.get("codes") or s["code"] in k["codes"] for k in live) else rest).append(s)
    return rest, kept


def improved(before: dict, after: dict) -> Optional[bool]:
    """Did a release improve its writer: re-flag down, else tenant edit rate down (None: not measurable)."""
    for key in ("reflag_rate", "tenant_edit_rate"):
        a, b = after.get(key), before.get(key)
        if a is not None and b is not None and (after.get("writes") or 0) >= MIN_WRITES:
            return a < b
    return None


def project_stop(history: dict[str, list[Optional[bool]]]) -> dict:
    """The P3 stop rule over each writer's two latest regular releases (newest first): both not improved -> flag."""
    flagged = {w: v[:2] for w, v in history.items() if len(v) >= 2 and v[0] is False and v[1] is False}
    return {"flagged": sorted(flagged), "rule": "two releases without improvement: stop at P3",
            "per_writer": {w: v[:2] for w, v in history.items()}}


def week_of(day: date) -> date:
    """The Monday of `day`'s ISO week."""
    return day - timedelta(days=day.weekday())


def last_full_week(now: datetime) -> date:
    """The Monday of the last full week before `now` (UTC)."""
    return week_of(now.astimezone(timezone.utc).date()) - timedelta(days=7)


# ── collectors (SQL, grouped by tenant id; NULL = platform / A1) ───────────────────────────────────────────────────

_T9_SQL = """
    WITH p AS (
        SELECT cp.piece_id, cp.tenant_id, cp.job_id,
               ARRAY(SELECT DISTINCT g->>'gate' FROM jsonb_array_elements(cp.gate_ledger) g
                      WHERE (g->>'passed')::boolean = false AND coalesce((g->>'blocking')::boolean, true)) AS failed
          FROM acp_shared.content_piece cp
         WHERE cp.created_at >= $1 AND cp.created_at < $2 AND cp.status IN ('approved', 'held')
           AND (CASE WHEN $3 THEN cp.writer_input->>'channel' = 'blog'
                     ELSE coalesce(cp.writer_input->>'channel', '') <> 'blog' END)
           AND ($4::uuid IS NULL OR cp.tenant_id = $4::uuid)
    ), prior AS (
        SELECT DISTINCT cp.tenant_id, g->>'gate' AS gate
          FROM acp_shared.content_piece cp, jsonb_array_elements(cp.gate_ledger) g
         WHERE cp.created_at >= $1 - make_interval(days => $5) AND cp.created_at < $1
           AND (g->>'passed')::boolean = false AND coalesce((g->>'blocking')::boolean, true)
           AND (CASE WHEN $3 THEN cp.writer_input->>'channel' = 'blog'
                     ELSE coalesce(cp.writer_input->>'channel', '') <> 'blog' END)
           AND ($4::uuid IS NULL OR cp.tenant_id = $4::uuid)
    ), cost AS (
        SELECT j.tenant_id, sum(l.cost_usd) AS cost_usd
          FROM (SELECT DISTINCT tenant_id, job_id FROM p WHERE job_id IS NOT NULL) j
          JOIN shared.llm_call_log l ON l.job_id = j.job_id
         GROUP BY j.tenant_id
    )
    SELECT p.tenant_id::text AS tenant_id, count(*) AS writes,
           count(*) FILTER (WHERE cardinality(p.failed) > 0) AS flagged,
           count(*) FILTER (WHERE EXISTS (SELECT 1 FROM prior WHERE prior.tenant_id = p.tenant_id
                                                                AND prior.gate = ANY (p.failed))) AS repeat_flagged,
           coalesce(max(c.cost_usd), 0)::float8 AS cost_usd
      FROM p LEFT JOIN cost c ON c.tenant_id = p.tenant_id
     GROUP BY p.tenant_id
"""
_LABELS_SQL = """
    SELECT cp.tenant_id::text AS tenant_id, count(*) AS labels,
           count(*) FILTER (WHERE l.label IN ('as_is', 'light')) AS light,
           coalesce(sum(l.minutes_to_fix), 0) AS minutes_sum, count(l.minutes_to_fix) AS minutes_n
      FROM acp_shared.content_piece_label l
      JOIN acp_shared.content_piece cp ON cp.piece_id = l.piece_id
     WHERE l.labelled_at >= $1 AND l.labelled_at < $2
       AND (CASE WHEN $3 THEN cp.writer_input->>'channel' = 'blog'
                 ELSE coalesce(cp.writer_input->>'channel', '') <> 'blog' END)
       AND ($4::uuid IS NULL OR cp.tenant_id = $4::uuid)
     GROUP BY cp.tenant_id
"""
_T2_SQL = """
    WITH v AS (
        SELECT v.id, v.tenant_id, v.job_id FROM gold_aa_internal.tenant_tour_versions v
         WHERE v.created_at >= $1 AND v.created_at < $2 AND v.edit_source = 'ai_generated'
           AND ($3::uuid IS NULL OR v.tenant_id = $3::uuid)
    ), cost AS (
        SELECT j.tenant_id, sum(l.cost_usd) AS cost_usd
          FROM (SELECT DISTINCT tenant_id, job_id FROM v WHERE job_id IS NOT NULL) j
          JOIN shared.llm_call_log l ON l.job_id = j.job_id
         GROUP BY j.tenant_id
    )
    SELECT v.tenant_id::text AS tenant_id, count(*) AS writes, coalesce(max(c.cost_usd), 0)::float8 AS cost_usd
      FROM v LEFT JOIN cost c ON c.tenant_id = v.tenant_id
     GROUP BY v.tenant_id
"""
_A1_SQL = """
    SELECT NULL::text AS tenant_id,
           (SELECT count(*) FROM silver_aa_internal.generated_content
             WHERE created_at >= $1 AND created_at < $2) AS writes,
           (SELECT coalesce(sum(cost_usd), 0)::float8 FROM shared.llm_call_log
             WHERE stage = 's1_generate' AND created_at >= $1 AND created_at < $2) AS cost_usd
"""
_SIGNALS_SQL = """
    SELECT tenant_id::text AS tenant_id,
           count(*) FILTER (WHERE action = 'tenant_edit') AS tenant_edits,
           count(*) FILTER (WHERE actor_type = 'admin' AND action = ANY ($5::text[])) AS admin_signals,
           count(*) FILTER (WHERE actor_type = 'admin' AND action = ANY ($5::text[])
                              AND reason_code IS NOT NULL) AS admin_with_reason
      FROM shared.writing_signal
     WHERE writer = $3 AND created_at >= $1 AND created_at < $2 AND ($4::uuid IS NULL OR tenant_id = $4::uuid)
     GROUP BY tenant_id
"""
_CLEAN_PAIRS_SQL = """
    SELECT writer, count(*) AS n FROM shared.writing_signal
     WHERE actor_type IN ('admin', 'tenant') AND before_text IS NOT NULL AND after_text IS NOT NULL
       AND fact_status = 'pass'
     GROUP BY writer
"""
_FUNNEL_SQL = """
    SELECT writer, status, count(*) AS n, count(*) FILTER (WHERE created_at >= $1 AND created_at < $2) AS this_week
      FROM shared.writing_rule GROUP BY writer, status
"""
_REPLAY_SQL = """
    SELECT writer,
           coalesce(sum(nullif(lint->'r0'->'signals'->>'explained_edits', '')::int), 0) AS explained,
           coalesce(sum(nullif(lint->'r0'->'signals'->>'usable', '')::int), 0) AS usable
      FROM shared.writing_rule
     WHERE lint ? 'r0' AND status IN ('candidate', 'approved', 'active')
     GROUP BY writer
"""


async def _fetch(conn, sql: str, *args) -> Optional[list[dict]]:
    """Rows, or None when a table / column this DB lacks (the CI replay, a code-first deploy): not measured."""
    try:
        async with conn.transaction():          # a savepoint: a missing table aborts nothing
            return [dict(r) for r in await conn.fetch(sql, *args)]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as e:
        logger.info("aa922_kpi_source_missing", error=f"{type(e).__name__}: {str(e)[:160]}")
        return None


def _merge(into: dict[Optional[str], dict], rows: Optional[list[dict]], keys: Iterable[str]) -> None:
    for r in rows or []:
        c = into.setdefault(r["tenant_id"], empty_counts())
        for k in keys:
            c[k] = (c.get(k) or 0) + (float(r[k]) if k == "cost_usd" else int(r[k] or 0))


async def collect(conn, writer: str, start: datetime, end: datetime,
                  tenant_id: Optional[str] = None) -> tuple[dict[Optional[str], dict], list[str]]:
    """Counts per tenant id for one writer and window, and the sources this DB could not read."""
    by: dict[Optional[str], dict] = {}
    missing: list[str] = []
    if writer.startswith("t9"):
        blog = writer == "t9_blog"
        rows = await _fetch(conn, _T9_SQL, start, end, blog, tenant_id, REPEAT_DAYS)
        _merge(by, rows, ("writes", "flagged", "repeat_flagged", "cost_usd"))
        labels = await _fetch(conn, _LABELS_SQL, start, end, blog, tenant_id)
        _merge(by, labels, ("labels", "light", "minutes_sum", "minutes_n"))
        missing += [n for n, r in (("t9_pieces", rows), ("labels", labels)) if r is None]
    elif writer == "t2":
        rows = await _fetch(conn, _T2_SQL, start, end, tenant_id)
        _merge(by, rows, ("writes", "cost_usd"))
        if rows is None:
            missing.append("t2_versions")
    else:
        rows = await _fetch(conn, _A1_SQL, start, end) if tenant_id is None else []
        _merge(by, rows, ("writes", "cost_usd"))
        if rows is None:
            missing.append("a1_content")
    sig = await _fetch(conn, _SIGNALS_SQL, start, end, writer, tenant_id, list(ADMIN_ACTIONS))
    _merge(by, sig, ("tenant_edits", "admin_signals", "admin_with_reason"))
    if sig is None:
        missing.append("signals")
    return by, missing


def total(by: dict[Optional[str], dict]) -> dict:
    out = empty_counts()
    for c in by.values():
        out = add_counts(out, c)
    return out


async def writer_kpis(conn, start: datetime, end: datetime) -> dict:
    """The KPI block of a report: per writer, overall and per tenant id."""
    from services.writing_memory.budget import month_spend
    out: dict[str, Any] = {"window": {"start": start.isoformat(), "end": end.isoformat()}, "writers": {},
                           "missing": {}}
    for w in WRITERS:
        by, missing = await collect(conn, w, start, end)
        out["writers"][w] = {"overall": derive(total(by), w),
                             "tenants": {t: derive(c, w) for t, c in sorted(by.items(), key=lambda x: x[0] or "")
                                         if t is not None}}
        if missing:
            out["missing"][w] = missing
    pairs = {r["writer"]: int(r["n"]) for r in await _fetch(conn, _CLEAN_PAIRS_SQL) or []}
    funnel: dict[str, dict] = {}
    for r in await _fetch(conn, _FUNNEL_SQL, start, end) or []:
        f = funnel.setdefault(r["writer"], {"total": {}, "this_week": {}})
        f["total"][r["status"]] = int(r["n"])
        f["this_week"][r["status"]] = int(r["this_week"])
    replay = {r["writer"]: {"explained": int(r["explained"]), "usable": int(r["usable"]),
                            "rate": _rate(int(r["explained"]), int(r["usable"]))}
              for r in await _fetch(conn, _REPLAY_SQL) or []}
    for w in WRITERS:
        out["writers"][w]["clean_pairs"] = pairs.get(w, 0)
        out["writers"][w]["funnel"] = funnel.get(w, {"total": {}, "this_week": {}})
        out["writers"][w]["replay"] = replay.get(w, {"explained": 0, "usable": 0, "rate": None})
    try:
        spent = await month_spend(conn)
    except Exception:  # noqa: BLE001 — a spend read failure leaves the number out, never the report
        spent = None
    out["learning_spend"] = {"month_spent_usd": spent, "month_cap_usd": cfg.MONTH_CAP_USD,
                             "share": round(spent / cfg.MONTH_CAP_USD, 4) if spent is not None else None}
    return out


# ── soft stops on the active releases ──────────────────────────────────────────────────────────────────────────────

_ACTIVE_SQL = """
    SELECT release_id::text AS release_id, writer, scope, tenant_id::text AS tenant_id, release_no, content_class,
           rule_ids::text[] AS rule_ids, status, activated_at, train_kind
      FROM shared.writing_memory_release
     WHERE status = 'active' AND (content_class <> 'empty' OR cardinality(rule_ids) > 0) AND activated_at IS NOT NULL
     ORDER BY writer, scope, tenant_id NULLS FIRST
"""
_KEEPS_SQL = """
    SELECT keep_id::text AS keep_id, codes, reason, kept_by, kept_until FROM shared.writing_memory_keep
     WHERE release_id = $1::uuid ORDER BY kept_until DESC
"""


async def release_window(conn, release: dict, now: datetime) -> tuple[dict, dict]:
    """(before, after) derived KPIs of one release: its owner's writes 28 days before its activation and since.
    `activated_at` is the LATEST move to active: the release guard (CIS 287 / 292) sets it on every transition to
    active, a rollback (retired -> active) included. A release restored by a soft stop is therefore measured on its
    own writes since it came back, never on the writes of the release it replaced (no chained rollbacks; integration
    test `..._restored_by_a_soft_stop_...`). Its baseline is then the replaced release's period (lenient, by design)."""
    act = release["activated_at"]
    tid = release.get("tenant_id") if release["scope"] == "tenant" else None
    b_by, _ = await collect(conn, release["writer"], act - timedelta(days=BASELINE_DAYS), act, tid)
    a_by, _ = await collect(conn, release["writer"], act, now, tid)   # a platform release: every owner counts
    return derive(total(b_by), release["writer"]), derive(total(a_by), release["writer"])


async def keeps_of(conn, release_id: str) -> list[dict]:
    rows = await _fetch(conn, _KEEPS_SQL, release_id)
    return [{**r, "codes": list(r["codes"] or [])} for r in rows or []]


async def evaluate_releases(pool, now: datetime) -> list[dict]:
    """Every active release with content: its window, soft stops, and which a keep covers (read-only)."""
    out = []
    async with pool.acquire() as conn:
        releases = [dict(r) for r in await _fetch(conn, _ACTIVE_SQL) or []]
        for r in releases:
            before, after = await release_window(conn, r, now)
            stops = soft_stops(r["writer"], r["scope"], before, after)
            rest, kept = covered(stops, await keeps_of(conn, r["release_id"]), now.date())
            out.append({"release": r, "before": before, "after": after, "stops": rest, "kept": kept})
    return out


async def act_on_stops(pool, evaluated: list[dict], *, apply: bool, actor: str) -> list[dict]:
    """Take every release with an uncovered soft stop out of service (apply) or say what would happen (DRY RUN)."""
    from services.writing_memory.post_activation import _alert, move_pointer
    actions = []
    for e in evaluated:
        r = e["release"]
        base = {"release_id": r["release_id"], "writer": r["writer"], "scope": r["scope"], "tenant_id": r["tenant_id"],
                "release_no": r["release_no"], "codes": [s["code"] for s in e["stops"]],
                "kept_codes": [s["code"] for s in e["kept"]]}
        if not e["stops"]:
            actions.append({**base, "action": "kept" if e["kept"] else "none"})
            continue
        if not apply:
            actions.append({**base, "action": "dry_run"})
            continue
        reason = f"soft_stop: {','.join(base['codes'])}"
        moved = await move_pointer(pool, r, actor=actor, reason=reason,
                                   audit_action="writing_memory.release_soft_stop",
                                   audit_details={"codes": base["codes"],
                                                  "numbers": [{k: s.get(k) for k in ("code", "value", "baseline", "n")}
                                                              for s in e["stops"]]})
        actions.append({**base, **moved})
        report = {"action": moved.get("action"), "violation_codes": base["codes"], "violations": len(base["codes"]),
                  "carried": e["after"].get("writes")}
        await _alert(pool, r, report)
    return actions


async def release_history(conn, now: datetime) -> dict[str, list[Optional[bool]]]:
    """Per writer, whether each of its (up to) two latest regular releases improved it (newest first)."""
    rows = await _fetch(conn, """
        SELECT release_id::text AS release_id, writer, scope, tenant_id::text AS tenant_id, activated_at,
               retired_at, status, release_no
          FROM shared.writing_memory_release
         WHERE train_kind = 'regular' AND activated_at IS NOT NULL AND scope = 'platform'
         ORDER BY writer, activated_at DESC""")
    hist: dict[str, list[Optional[bool]]] = {}
    for r in rows or []:
        if len(hist.setdefault(r["writer"], [])) >= 2:
            continue
        end = r.get("retired_at") or now
        before, after = await release_window(conn, {**r, "activated_at": r["activated_at"]}, end)
        hist[r["writer"]].append(improved(before, after))
    return hist


# ── rule expiry with re-confirmation ───────────────────────────────────────────────────────────────────────────────

_STALE_ACTIVE_SQL = """
    SELECT rule_id::text AS rule_id, kind, scope, tenant_id::text AS tenant_id, writer, cluster_key,
           coalesce(approved_at, created_at) AS since
      FROM shared.writing_rule WHERE status = 'active' AND expires_at < now()
"""
_FRESH_SQL = """
    SELECT signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, before_text,
           after_text, gate, problem, reason_code, action, actor_type, actor_id, fact_status, source_id, created_at
      FROM shared.writing_signal
     WHERE fact_status = 'pass' AND actor_type IN ('admin', 'tenant') AND created_at > $1 AND writer = $2
       AND scope = $3 AND tenant_id IS NOT DISTINCT FROM $4::uuid
       AND created_at >= now() - make_interval(days => $5)
"""


def reconfirmed(rule: dict, fresh: list[dict]) -> bool:
    """A fresh human pass signal of the rule's owner joins its cluster (a summary: any fresh human edit with text)."""
    from services.writing_memory.mining import labels_of
    if rule["kind"] == "preference_summary":
        return any(s.get("after_text") is not None and s.get("before_text") for s in fresh)
    return any(f"{s['writer']}|{label}"[:200] == rule["cluster_key"] for s in fresh for label in labels_of(s))


async def expire_rules(conn, *, apply: bool) -> dict:
    """Candidates / approved past expiry expire (rules.expire_stale); active ones are re-confirmed or expire."""
    from services.writing_memory import rules as rules_mod
    out: dict[str, Any] = {"expired": [], "reconfirmed": [], "stale_candidates": 0}
    if apply:
        out["stale_candidates"] = await rules_mod.expire_stale(conn)
    for r in await _fetch(conn, _STALE_ACTIVE_SQL) or []:
        fresh = await _fetch(conn, _FRESH_SQL, r["since"], r["writer"], r["scope"], r["tenant_id"],
                             cfg.RULE_TTL_DAYS) or []
        if reconfirmed(r, fresh):
            out["reconfirmed"].append(r["rule_id"])
            if apply:
                await conn.execute("UPDATE shared.writing_rule SET expires_at = now() + make_interval(days => $2), "
                                   "updated_at = now() WHERE rule_id = $1::uuid", r["rule_id"], cfg.RULE_TTL_DAYS)
        else:
            out["expired"].append(r["rule_id"])
            if apply:
                await conn.execute("UPDATE shared.writing_rule SET status = 'expired', updated_at = now() "
                                   "WHERE rule_id = $1::uuid AND status = 'active'", r["rule_id"])
    return out


# ── the run ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def run_weekly(pool, *, week_start: Optional[date] = None, apply: bool = False, trigger: str = "manual",
                     actor: str = "job:writing_memory_kpi_weekly", job_id: Optional[str] = None,
                     now: Optional[datetime] = None) -> dict:
    """One weekly run. DRY RUN (apply False): the report is computed and returned, nothing is stored or moved. Apply:
    soft stops act, rules expire / re-confirm, the disagreement queue is filled and the report row is stored."""
    from services.writing_memory import disagreements
    now = now or datetime.now(timezone.utc)
    week = week_start or last_full_week(now)
    start = datetime(week.year, week.month, week.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    async with pool.acquire() as conn:
        kpis = await writer_kpis(conn, start, end)
    evaluated = await evaluate_releases(pool, now)
    actions = await act_on_stops(pool, evaluated, apply=apply, actor=actor)
    async with pool.acquire() as conn:
        async with conn.transaction():
            rules = await expire_rules(conn, apply=apply)
            hist = await release_history(conn, now)
            queued = await disagreements.fill_queue(conn, week, apply=apply)
    stops = [{"release_id": e["release"]["release_id"], "writer": e["release"]["writer"],
              "scope": e["release"]["scope"], "tenant_id": e["release"]["tenant_id"],
              "release_no": e["release"]["release_no"], "stops": e["stops"], "kept": e["kept"],
              "after_writes": e["after"].get("writes"), "before_writes": e["before"].get("writes")}
             for e in evaluated]
    report = {"week_start": week.isoformat(), "trigger": trigger, "apply": apply, "kpis": kpis, "soft_stops": stops,
              "actions": actions, "rules": rules, "project_stop": project_stop(hist), "disagreements": queued}
    if apply:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO shared.writing_memory_kpi_report (week_start, trigger, kpis, soft_stops, actions, rules,
                          project_stop, disagreements, job_id, created_by)
                   VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9::uuid, $10)
                   ON CONFLICT (week_start) WHERE trigger = 'schedule' DO NOTHING
                   RETURNING report_id::text AS report_id""",
                week, trigger, json.dumps(kpis, default=str), json.dumps(stops, default=str),
                json.dumps(actions, default=str), json.dumps(rules, default=str),
                json.dumps(report["project_stop"], default=str), int(queued.get("added", 0)), job_id, actor[:128])
        report["report_id"] = row["report_id"] if row else None
        report["stored"] = row is not None
    logger.info("aa922_kpi_weekly", week=week.isoformat(), apply=apply, releases=len(evaluated),
                stops=sum(len(e["stops"]) for e in evaluated), expired=len(rules["expired"]))
    return report


# ── reads and keeps for the admin page ─────────────────────────────────────────────────────────────────────────────

_REPORT_COLUMNS = ("report_id::text AS report_id, week_start, trigger, kpis, soft_stops, actions, rules, project_stop, "
                   "disagreements, job_id::text AS job_id, created_by, created_at")


def serialise(row: dict) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, str) and k in ("kpis", "soft_stops", "actions", "rules", "project_stop"):
            v = json.loads(v)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


async def list_reports(conn, limit: int = 12) -> list[dict]:
    rows = await conn.fetch(f"SELECT {_REPORT_COLUMNS} FROM shared.writing_memory_kpi_report "
                            f"ORDER BY week_start DESC, created_at DESC LIMIT {int(limit)}")
    return [serialise(dict(r)) for r in rows]


class KeepError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail


async def record_keep(conn, release_id: str, *, reason: str, actor: str, codes: Iterable[str] = (),
                      days: int = 14) -> dict:
    """A human's reason to keep an active release despite soft stops (the codes, or every code), for <= 30 days."""
    from services.acp_shared.audit_log import write_audit_log
    from services.writing_memory import releases as rel
    codes = sorted(set(codes))
    if any(c not in SOFT_STOP_CODES for c in codes):
        raise KeepError(422, "invalid", f"codes must be among {list(SOFT_STOP_CODES)}")
    if not 1 <= int(days) <= KEEP_MAX_DAYS:
        raise KeepError(422, "invalid", f"a keep lasts 1 to {KEEP_MAX_DAYS} days")
    reason = " ".join((reason or "").split())
    if len(reason) < 3:
        raise KeepError(422, "invalid", "a reason is required")
    r = await rel.get_release(conn, release_id)
    if r is None:
        raise KeepError(404, "not_found", "Unknown release")
    if r["status"] != "active":
        raise KeepError(409, "not_active", f"a {r['status']} release is not serving; nothing to keep")
    row = await conn.fetchrow(
        """INSERT INTO shared.writing_memory_keep (release_id, writer, scope, tenant_id, codes, reason, kept_by,
                  kept_until)
           VALUES ($1::uuid, $2, $3, $4::uuid, $5::text[], $6, $7, (now() AT TIME ZONE 'utc')::date + $8::int)
           RETURNING keep_id::text AS keep_id, release_id::text AS release_id, codes, reason, kept_by, kept_until,
                     created_at""",
        r["release_id"], r["writer"], r["scope"], r.get("tenant_id"), codes, reason[:500], actor[:128], int(days))
    await write_audit_log(conn, tenant_id=r.get("tenant_id") or rel.PLATFORM_TENANT_ID, actor=actor[:128],
                          action="writing_memory.release_keep", resource_type="writing_memory_release",
                          resource_id=str(r["release_id"]),
                          details={"writer": r["writer"], "scope": r["scope"], "release_no": r["release_no"],
                                   "codes": codes, "days": int(days)})
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()}


__all__ = ["SOFT_STOP_CODES", "KeepError", "add_counts", "collect", "covered", "derive", "evaluate_releases",
           "expire_rules", "improved", "last_full_week", "list_reports", "project_stop", "reconfirmed",
           "record_keep", "run_weekly", "serialise", "soft_stops", "total", "week_of", "wilson", "writer_kpis"]
