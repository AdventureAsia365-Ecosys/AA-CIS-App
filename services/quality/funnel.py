"""AA-900 (S271 W0, root ADR 0020 "Measurement") — the content quality funnel and its watchdog rules, read-only ($0).

One snapshot per day (report CIS-08, ``services/reports/funnel_report.py``; nightly through the report chain), per
workflow and ISO week (ICT, Monday first), from stored rows only (names: root docs/architecture/db-schema-reference.md):

- **T9** (tenant social content, per channel): ``acp_shared.content_piece``. One row per piece (the placeholder row is
  updated by the write job). ``created`` = rows created in the week; ``approved`` = passed every blocking gate;
  ``held`` = an admin-only label (the agency still receives the text, product rule S232/S270); ``delivered`` =
  approved + held (content the agency received); ``failed`` = no content. ``first_pass`` = approved on attempt 1 with
  no discarded attempt. ``attempts`` of a piece = the highest attempt it reached (its final ``attempt_number`` or a
  discarded attempt's). Gate failures are counted on EVERY attempt (the final ``gate_ledger`` and each
  ``discarded_attempts[].gate_ledger``), not only the first ``held_reason``, and ``F9_cta_fact`` is split into the
  judge outage (``F9_cta_fact.judge_unavailable``: the result's ``degraded`` marker or a "judge unavailable"
  violation), broken text (``F9_cta_fact.text_integrity``: "TEXT_INTEGRITY: " violations) and the claim findings
  (``F9_cta_fact``), so an infrastructure failure never inflates the claim baseline (critic F15). The first-gate count
  (``held_reason`` prefix) is kept next to it for comparison with the S271 audit.
- **S1** (Master rewrite): ``silver_aa_internal.generated_content`` (one row per draft) with its evaluations in
  ``silver_aa_internal.quality_scores`` and its publication in ``gold_aa_internal.published_tours``. ``delivered`` =
  ever published: a published_tours row points at the draft, or the draft is ``approved`` / ``published`` (a later
  rewrite repoints the tour's one published_tours row, the older draft stays approved and stays delivered in its
  week); ``held`` = status ``hitl``; ``failed`` = ``rejected``; attempts = ``retry_count`` + 1, + 1 when the S1 fix
  pass ran (``fix_pass_applied``: flag_fix rewrites fields without raising retry_count); first pass = delivered with
  no retry, no fix pass and no human (``human_edited``, a reviewer, or a ``review_queue`` row: a hitl draft a reviewer
  approved). Gate failures per attempt = the failure and brand audit codes of each evaluation (evaluation order =
  attempt).
- **T2** (tenant tour rewrite): ``gold_aa_internal.tenant_tour_versions`` written by AI (``edit_source`` ai_generated
  or re_rewrite). ``delivered`` = a written version (not pending / failed); ``held`` = ``qa_status`` escalated (the
  version still reaches the tenant, ``qa_auto_passed``); attempts = ``qa_repair_count`` + 1 (T3 rounds); first pass =
  ``qa_status`` passed with no repair round. Per-attempt gate codes are not stored on the version (follow-up).
- **Stages** (``shared.llm_call_log``, production traffic only: ``route_watch.LEDGER_FILTER``, no shadow / eval / Jev
  rows, and no AI gateway route-test row (``NOT_ROUTE_TEST``)), per stage of each workflow (``STAGE_RE``, the raw
  ``stage``: eval rows are out, unlike ``route_cut``, which keeps them under their real stage) and week: answered
  requests by rung (the ``route_watch`` rung rule: primary, rung 1, rung 2+, unknown), fallback share, replies cut at
  their token ceiling (``route_watch.CUT_ROW``), refused rows (expected refusals apart, ``expected_refusals``: the
  ``shared/llm_client/expected_refusal.py`` rule), error rows, cost. LLM USD per delivered object = the workflow's
  stage cost of the week / its delivered objects, for every workflow (T9 included: the totals use the whole T9 stage
  cost, attributed + unattributed). T9 per channel can only use the ledger rows that carry the piece or its angle-gate
  request: that per-channel figure is the *attributed* cost (``attributed_usd_per_delivered`` in the totals); the rest
  (judges, angle generation before a piece exists) is ``unattributed_cost_usd`` per week.

Watchdog (:func:`watch_findings`, over the trailing ``WATCH_DAYS`` days that end with the snapshot day; thresholds per
stage from :func:`watch_policy`, documented in CONTEXT.md "Quality funnel"):

- ``qf_cut.<stage>`` — more than ``CUT_ALERT_PCT`` % of the stage's billed replies were cut at their ceiling;
- ``qf_fb.<stage>`` — at least the stage's ``shared.llm_route_policy`` ``fallback_alert_pct`` % (route_watch's rule:
  own row, ``*`` row, default 20) of the answered requests came from a fallback rung;
- ``qf_drift.<stage>`` — served-model drift: at least ``DRIFT_ALERT_PCT`` % of the answered requests came from a
  model that served under ``DRIFT_NEW_MODEL_PCT`` % of the stage's answers in the ``WATCH_DAYS`` before (a route,
  catalog or provider change that moved the served model).

Each needs at least ``max(MIN_REQUESTS, policy min_requests)`` rows in the window (and drift as many in the prior
window). Alerts fire on the TRANSITION only (:func:`mark_repeats`): a finding the previous scheduled CIS-08 run already
had is stored with ``repeat: true`` and not raised again, and the night's new findings go out as one ``qf_digest``
alert (:func:`digest_finding`). The 24-hour route alerts of ``llm_route_watch`` stay as they are; these are the
weekly-scale quality view of the same ledger.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from services.reports import periods
from shared.aa_llm.errors import TRUNCATED_STOP_REASONS
from shared.llm_client import route_watch
from shared.llm_client.expected_refusal import EXPECTED_SQL
from shared.llm_client.route_watch import CUT_ROW, FALLBACK_RUNG, LEDGER_FILTER, OK, POSITION_EXPR, PRIMARY_RUNG
from shared.ops_checks import QF_CUT_PREFIX, QF_DIGEST_CHECK, QF_DRIFT_PREFIX, QF_FALLBACK_PREFIX, Finding

SNAPSHOT_VERSION = 1
WEEKS = 8                    # ISO weeks in the series (the snapshot day's week and the 7 before)
WATCH_DAYS = 7
MIN_REQUESTS = 20           # weekly floor; a stage's llm_route_policy min_requests above it wins (watch_policy)
CUT_ALERT_PCT = route_watch.CUT_ALERT_PCT     # the route_cut rule of llm_route_watch, over a week instead of 24 h
FALLBACK_ALERT_PCT = float(route_watch.DEFAULTS.fallback_alert_pct)   # used only when no policy is stored
DRIFT_ALERT_PCT = 10.0
DRIFT_NEW_MODEL_PCT = 1.0
THRESHOLDS = {"watch_days": WATCH_DAYS, "min_requests": MIN_REQUESTS, "cut_alert_pct": CUT_ALERT_PCT,
              "fallback_alert_pct": FALLBACK_ALERT_PCT, "drift_alert_pct": DRIFT_ALERT_PCT,
              "drift_new_model_pct": DRIFT_NEW_MODEL_PCT,
              "policy": "per stage: shared.llm_route_policy fallback_alert_pct; min = max(min_requests, policy)",
              "alert_on": "transition: a finding present in the previous scheduled run is stored as a repeat"}
DIGEST_MAX_LINES = 8

# The live route check of AI gateway > Trace (api/services/llm_route_trace.run_route_test): a few tiny real requests
# capped at 16 output tokens. They are production rows for LEDGER_FILTER, but they are no content, and every one of
# them stops on its ceiling, so they stay out of every funnel read.
NOT_ROUTE_TEST = "coalesce(quality_signal ->> 'route_test', '') <> 'true'"

CUT_PREFIX, FALLBACK_PREFIX, DRIFT_PREFIX = QF_CUT_PREFIX, QF_FALLBACK_PREFIX, QF_DRIFT_PREFIX

WORKFLOWS = ("t9", "s1", "t2")
# stage -> workflow: T9 = angle generation (t8), the writer and its helpers (t9), the gates and judges (t10).
STAGE_RE = r"^(t8|t9|t10|s1|t2)_"
_STAGE_WORKFLOW = (("t8_", "t9"), ("t9_", "t9"), ("t10_", "t9"), ("s1_", "s1"), ("t2_", "t2"))

# T9 ledger names (guarded against the gate code by tests/unit/test_aa900_quality_funnel.py)
F9_GATE = "F9_cta_fact"
F9_JUDGE_UNAVAILABLE = "f9_judge_unavailable"          # services/quality/degraded.F9_JUDGE_UNAVAILABLE
JUDGE_UNAVAILABLE_TEXT = "judge unavailable"            # quality_gates F9 outage violation text
TEXT_INTEGRITY_PREFIX = "TEXT_INTEGRITY: "             # sentence_repair.TEXT_INTEGRITY_PREFIX
F9_JUDGE_CLASS = F9_GATE + ".judge_unavailable"
F9_TEXT_CLASS = F9_GATE + ".text_integrity"

T9_DELIVERED = ("approved", "held")
S1_HELD, S1_FAILED = "hitl", "rejected"
S1_DELIVERED = ("approved", "published")      # content_status_enum values of a draft that reached the Master
S1_DELIVERED_SQL = ", ".join(f"'{v}'" for v in S1_DELIVERED)
T2_EDIT_SOURCES = ("ai_generated", "re_rewrite")


def week_sql(col: str) -> str:
    """The ICT ISO week (its Monday) of a timestamptz column; no dependency on the database time zone."""
    return f"(date_trunc('week', ({col} AT TIME ZONE 'UTC') + interval '7 hours'))::date"


def workflow_of(stage: str) -> Optional[str]:
    for prefix, wf in _STAGE_WORKFLOW:
        if stage.startswith(prefix):
            return wf
    return None


# ── SQL ($1 = window start, $2 = window end, timestamptz) ───────────────────────────────────────────────────────────

_PIECES = f"""
    SELECT piece_id, angle_gate_request_id, coalesce(channel, '-') AS channel, status, attempt_number, held_reason,
           CASE WHEN jsonb_typeof(gate_ledger) = 'array' THEN gate_ledger ELSE '[]'::jsonb END AS ledger,
           CASE WHEN jsonb_typeof(discarded_attempts) = 'array' THEN discarded_attempts
                ELSE '[]'::jsonb END AS discarded,
           CASE WHEN jsonb_typeof(flags) = 'array' THEN flags ELSE '[]'::jsonb END AS flags,
           {week_sql('created_at')} AS week
      FROM acp_shared.content_piece
     WHERE created_at >= $1 AND created_at < $2
"""

T9_OBJECTS_SQL = f"""
    WITH p AS ({_PIECES}), a AS (
        SELECT p.*, greatest(p.attempt_number, coalesce((
                   SELECT max((d ->> 'attempt_number')::int) FROM jsonb_array_elements(p.discarded) d
                    WHERE d ->> 'attempt_number' ~ '^[0-9]{{1,3}}$'), 0)) AS attempts,
               p.flags @> '[{{"gate": "degraded"}}]'::jsonb AS degraded
          FROM p
    )
    SELECT week, channel,
           count(*) AS created,
           count(*) FILTER (WHERE status = 'approved') AS approved,
           count(*) FILTER (WHERE status = 'held') AS held,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'processing') AS processing,
           count(*) FILTER (WHERE status = 'approved' AND attempt_number = 1
                              AND jsonb_array_length(discarded) = 0) AS first_pass,
           coalesce(sum(attempts) FILTER (WHERE status IN ('approved', 'held')), 0) AS attempts_delivered,
           coalesce(sum(attempts) FILTER (WHERE status = 'approved'), 0) AS attempts_approved,
           coalesce(sum(attempts) FILTER (WHERE status = 'held'), 0) AS attempts_held,
           count(*) FILTER (WHERE status IN ('approved', 'held') AND degraded) AS degraded
      FROM a
     GROUP BY 1, 2
     ORDER BY 1, 2
"""

# Every failing gate entry of every attempt; F9 split into its three classes (module doc).
T9_GATES_SQL = f"""
    WITH p AS ({_PIECES}), att AS (
        SELECT week, channel, piece_id, attempt_number AS attempt, ledger FROM p
        UNION ALL
        SELECT p.week, p.channel, p.piece_id,
               CASE WHEN d ->> 'attempt_number' ~ '^[0-9]{{1,3}}$' THEN (d ->> 'attempt_number')::int END,
               d -> 'gate_ledger'
          FROM p, jsonb_array_elements(p.discarded) d
         WHERE jsonb_typeof(d -> 'gate_ledger') = 'array'
    ), f AS (
        SELECT a.week, a.channel, a.piece_id, a.attempt, g ->> 'gate' AS gate,
               coalesce(g ->> 'blocking', 'true') <> 'false' AS blocking,
               (g ->> 'degraded' = '{F9_JUDGE_UNAVAILABLE}' OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(g -> 'violations') = 'array'
                                                                 THEN g -> 'violations' ELSE '[]'::jsonb END) v
                     WHERE v LIKE '{JUDGE_UNAVAILABLE_TEXT}%')) AS judge_down,
               EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(g -> 'violations') = 'array'
                                                                    THEN g -> 'violations' ELSE '[]'::jsonb END) v
                        WHERE v LIKE '{TEXT_INTEGRITY_PREFIX}%') AS has_text,
               EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(g -> 'violations') = 'array'
                                                                    THEN g -> 'violations' ELSE '[]'::jsonb END) v
                        WHERE v NOT LIKE '{TEXT_INTEGRITY_PREFIX}%') AS has_other
          FROM att a, jsonb_array_elements(a.ledger) g
         WHERE jsonb_typeof(g) = 'object' AND g ->> 'passed' = 'false' AND coalesce(g ->> 'gate', '') <> ''
    )
    SELECT week, channel, attempt, cls AS gate, blocking, count(*) AS fires, count(DISTINCT piece_id) AS pieces
      FROM f CROSS JOIN LATERAL unnest(CASE
               WHEN gate <> '{F9_GATE}' THEN ARRAY[gate]
               WHEN judge_down THEN ARRAY['{F9_JUDGE_CLASS}']
               ELSE array_remove(ARRAY[CASE WHEN has_text THEN '{F9_TEXT_CLASS}' END,
                                       CASE WHEN has_other OR NOT has_text THEN '{F9_GATE}' END], NULL) END) cls
     GROUP BY 1, 2, 3, 4, 5
     ORDER BY 1, 2, 3, 4, 5
"""

# The first failing gate only (held_reason prefix): the number the S271 audit had, kept for comparison.
T9_FIRST_GATE_SQL = f"""
    WITH p AS ({_PIECES})
    SELECT week, channel, split_part(held_reason, ':', 1) AS gate, count(*) AS held
      FROM p WHERE status = 'held' AND coalesce(held_reason, '') <> ''
     GROUP BY 1, 2, 3
     ORDER BY 1, 2, 3
"""

# T9 stage cost per channel: a ledger row of a T9 stage that carries the piece, or (no piece id) its angle-gate
# request; a request shared by several pieces is counted once, on its lowest piece id.
T9_COST_SQL = f"""
    WITH p AS ({_PIECES}), l AS (
        SELECT id, cost_usd, content_piece_id, angle_gate_request_id FROM shared.llm_call_log
         WHERE created_at >= $1::timestamptz - interval '1 day' AND created_at < $2::timestamptz + interval '1 day'
           AND {LEDGER_FILTER} AND {NOT_ROUTE_TEST}
           AND stage ~ '^(t8|t9|t10)_' AND cost_usd IS NOT NULL
           AND (content_piece_id IS NOT NULL OR angle_gate_request_id IS NOT NULL)
    ), m AS (
        SELECT DISTINCT ON (l.id) l.id, l.cost_usd, p.week, p.channel
          FROM l JOIN p ON p.piece_id = l.content_piece_id
                        OR (l.content_piece_id IS NULL AND p.angle_gate_request_id = l.angle_gate_request_id)
         ORDER BY l.id, p.piece_id
    )
    SELECT week, channel, count(*) AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM m GROUP BY 1, 2 ORDER BY 1, 2
"""

# delivered = ever published: a published_tours row points at the draft now, or the draft is approved / published
# (process_export only publishes an approved draft and leaves it approved; the next rewrite repoints the tour's
# published_tours row to the new draft, so "points at it now" alone would move older weeks back out of delivered).
# first pass = delivered on attempt 1 with no repair and no human: no retry, no S1 fix pass (flag_fix rewrites
# the flagged fields without raising retry_count), no human edit or reviewer, no review_queue row (a hitl draft a
# reviewer approved). attempts = retry_count + 1, + 1 for the fix pass round.
S1_OBJECTS_SQL = f"""
    WITH g AS (
        SELECT gc.id, gc.status::text AS status, gc.retry_count, gc.fallback_used,
               coalesce(gc.fix_pass_applied, false) AS fix_pass,
               (coalesce(gc.human_edited, false) OR gc.reviewed_by IS NOT NULL OR gc.reviewed_by_legacy IS NOT NULL
                OR EXISTS (SELECT 1 FROM silver_aa_internal.review_queue rq WHERE rq.generated_content_id = gc.id))
                   AS human,
               CASE WHEN jsonb_typeof(gc.metadata -> 'degraded') = 'array'
                    THEN jsonb_array_length(gc.metadata -> 'degraded') > 0 ELSE false END AS degraded,
               (gc.status::text IN ({S1_DELIVERED_SQL}) OR EXISTS (
                    SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.generated_content_id = gc.id))
                   AS delivered,
               {week_sql('gc.created_at')} AS week
          FROM silver_aa_internal.generated_content gc
         WHERE gc.created_at >= $1 AND gc.created_at < $2
    )
    SELECT week,
           count(*) AS created,
           count(*) FILTER (WHERE delivered) AS delivered,
           count(*) FILTER (WHERE status = '{S1_HELD}' AND NOT delivered) AS held,
           count(*) FILTER (WHERE status = '{S1_FAILED}' AND NOT delivered) AS failed,
           count(*) FILTER (WHERE NOT delivered AND status NOT IN ('{S1_HELD}', '{S1_FAILED}')) AS open,
           count(*) FILTER (WHERE delivered AND retry_count = 0 AND NOT fix_pass AND NOT human) AS first_pass,
           coalesce(sum(retry_count + 1 + fix_pass::int) FILTER (WHERE delivered), 0) AS attempts_delivered,
           count(*) FILTER (WHERE delivered AND fix_pass) AS fix_pass,
           count(*) FILTER (WHERE delivered AND human) AS human,
           count(*) FILTER (WHERE delivered AND fallback_used) AS fallback_writer,
           count(*) FILTER (WHERE delivered AND coalesce(degraded, false)) AS degraded
      FROM g
     GROUP BY 1
     ORDER BY 1
"""

# Each evaluation of a draft is one attempt (evaluation order); its failure codes (array or object keys) and brand
# audit codes ("brand:" prefix).
S1_GATES_SQL = f"""
    WITH q AS (
        SELECT qs.id, qs.failure_codes, qs.brand_audit_codes, {week_sql('gc.created_at')} AS week,
               row_number() OVER (PARTITION BY qs.generated_content_id ORDER BY qs.evaluated_at, qs.id) AS attempt,
               qs.generated_content_id
          FROM silver_aa_internal.quality_scores qs
          JOIN silver_aa_internal.generated_content gc ON gc.id = qs.generated_content_id
         WHERE gc.created_at >= $1 AND gc.created_at < $2
    ), c AS (
        SELECT week, attempt, generated_content_id, code FROM q, LATERAL (
            SELECT x AS code FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(q.failure_codes) = 'array' THEN q.failure_codes ELSE '[]'::jsonb END) x
            UNION ALL SELECT k FROM jsonb_object_keys(
                CASE WHEN jsonb_typeof(q.failure_codes) = 'object' THEN q.failure_codes ELSE '{{}}'::jsonb END) k
            UNION ALL SELECT 'brand:' || x FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(q.brand_audit_codes) = 'array' THEN q.brand_audit_codes ELSE '[]'::jsonb END) x
            UNION ALL SELECT 'brand:' || k FROM jsonb_object_keys(
                CASE WHEN jsonb_typeof(q.brand_audit_codes) = 'object' THEN q.brand_audit_codes
                     ELSE '{{}}'::jsonb END) k
        ) codes
    )
    SELECT week, attempt, code AS gate, count(*) AS fires, count(DISTINCT generated_content_id) AS objects
      FROM c GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

T2_OBJECTS_SQL = f"""
    WITH v AS (
        SELECT status, qa_status, qa_repair_count, judge_breakdown, {week_sql('created_at')} AS week
          FROM gold_aa_internal.tenant_tour_versions
         WHERE created_at >= $1 AND created_at < $2 AND edit_source = ANY($3::text[])
    )
    SELECT week,
           count(*) AS created,
           count(*) FILTER (WHERE status NOT IN ('pending', 'failed')) AS delivered,
           count(*) FILTER (WHERE status NOT IN ('pending', 'failed') AND qa_status = 'escalated') AS held,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'pending') AS open,
           count(*) FILTER (WHERE status NOT IN ('pending', 'failed') AND qa_status = 'passed'
                              AND coalesce(qa_repair_count, 0) = 0) AS first_pass,
           coalesce(sum(coalesce(qa_repair_count, 0) + 1)
                    FILTER (WHERE status NOT IN ('pending', 'failed')), 0) AS attempts_delivered,
           count(*) FILTER (WHERE status NOT IN ('pending', 'failed')
                              AND coalesce(judge_breakdown ? 'fallback_writer', false)) AS fallback_writer
      FROM v
     GROUP BY 1
     ORDER BY 1
"""

# $1 start, $2 the cut stop reasons (route_watch.CUT_ROW binds $2), $3 end.
# Production rows only (LEDGER_FILTER: eval rows out, counted under their raw ``stage``), unlike route_cut, which keeps
# eval rows under their real stage. ``refused`` leaves out the expected refusals (shared/llm_client/expected_refusal.py:
# a model with no grant yet on an account that answers), counted apart as ``expected_refusals``, as in route_digest.
_BILLED = "(coalesce(tokens_out, 0) > 0 OR coalesce(cost_usd, 0) > 0 OR status = 'truncated')"
STAGES_SQL = f"""
    WITH r AS (
        SELECT stage, status, stop_reason, tokens_out, cost_usd, attempt_no, fallback_used, quality_signal,
               {POSITION_EXPR} AS position, {week_sql('created_at')} AS week, {EXPECTED_SQL} AS expected
          FROM shared.llm_call_log
         WHERE created_at >= $1 AND created_at < $3 AND {LEDGER_FILTER} AND {NOT_ROUTE_TEST}
           AND stage ~ '{STAGE_RE}'
    )
    SELECT stage, week,
           count(*) FILTER (WHERE {OK}) AS answered,
           count(*) FILTER (WHERE {OK} AND {PRIMARY_RUNG}) AS primary_answered,
           count(*) FILTER (WHERE {OK} AND {FALLBACK_RUNG}) AS fallback_answered,
           count(*) FILTER (WHERE {OK} AND {FALLBACK_RUNG} AND coalesce(position, 1) <= 1) AS rung1,
           count(*) FILTER (WHERE {OK} AND {FALLBACK_RUNG} AND position >= 2) AS rung2plus,
           count(*) FILTER (WHERE {OK} AND attempt_no IS NULL AND NOT {FALLBACK_RUNG}) AS unknown_rung,
           count(*) FILTER (WHERE {_BILLED}) AS replies,
           count(*) FILTER (WHERE {_BILLED} AND {CUT_ROW}) AS cut,
           count(*) FILTER (WHERE status = 'refused' AND NOT expected) AS refused,
           count(*) FILTER (WHERE expected) AS expected_refusals,
           count(*) FILTER (WHERE status = 'error') AS errors,
           coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM r
     GROUP BY 1, 2
     ORDER BY 1, 2
"""

# Watch window: $1 prior start, $2 cut stop reasons, $3 recent start, $4 end. Per stage x model x window.
WATCH_SQL = f"""
    SELECT stage, model, CASE WHEN created_at >= $3 THEN 'recent' ELSE 'prior' END AS win,
           count(*) FILTER (WHERE {OK}) AS answered,
           count(*) FILTER (WHERE {OK} AND {FALLBACK_RUNG}) AS fallback_answered,
           count(*) FILTER (WHERE {_BILLED}) AS replies,
           count(*) FILTER (WHERE {_BILLED} AND {CUT_ROW}) AS cut
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $4 AND {LEDGER_FILTER} AND {NOT_ROUTE_TEST}
       AND stage ~ '{STAGE_RE}'
     GROUP BY 1, 2, 3
"""

ROUTES_SQL = f"SELECT stage, model_id FROM shared.llm_role_config WHERE stage ~ '{STAGE_RE}'"


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _ratio(part, whole) -> Optional[float]:
    return round(float(part) / float(whole), 4) if part is not None and whole else None


def _pct(part, whole) -> Optional[float]:
    r = _ratio(part, whole)
    return round(r * 100, 2) if r is not None else None


def _plain(row: Any) -> dict:
    out = {}
    for k, v in dict(row).items():
        out[k] = v.isoformat() if isinstance(v, (date, datetime)) else (float(v) if hasattr(v, "is_finite") else v)
    return out


def window(day: date, weeks: int = WEEKS) -> tuple[date, date, datetime, datetime]:
    """(first ICT day, last ICT day, start, end) of the series: the ISO week of ``day`` and the ``weeks - 1`` before,
    up to the end of ``day``."""
    monday = day - timedelta(days=day.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    start, end = periods.range_bounds(first, day)
    return first, day, start, end


# Is a held object part of `delivered`? T9 and T2: yes (the agency receives held content; the label is admin-only).
# S1: no (a hitl draft waits for review and is not in the Master yet).
HELD_IN_DELIVERED = {"t9": True, "s1": False, "t2": True}


def _rates(*, delivered: int, held: int, failed: int, first_pass: int, attempts: float, cost: Optional[float],
           held_in_delivered: bool) -> dict:
    """first-pass yield = clean on attempt 1 / finished (delivered + failed, + held when held is not delivered);
    hold rate = held / (delivered, + held when held is not delivered)."""
    seen = delivered + (0 if held_in_delivered else held)
    return {"first_pass_yield": _ratio(first_pass, seen + failed), "hold_rate": _ratio(held, seen),
            "attempts_per_delivered": _ratio(attempts, delivered),
            "cost_usd": round(cost, 6) if cost is not None else None,
            "usd_per_delivered": round(cost / delivered, 4) if cost is not None and delivered else None}


def _object_row(r: dict, cost: Optional[float], held_in_delivered: bool = True) -> dict:
    """Derived rates of one workflow x week (x channel) row."""
    delivered = int(r.get("delivered") if r.get("delivered") is not None
                    else int(r.get("approved") or 0) + int(r.get("held") or 0))
    out = {**r, "delivered": delivered,
           **_rates(delivered=delivered, held=int(r.get("held") or 0), failed=int(r.get("failed") or 0),
                    first_pass=int(r.get("first_pass") or 0), attempts=float(r.get("attempts_delivered") or 0),
                    cost=cost, held_in_delivered=held_in_delivered)}
    if "approved" in r:
        out["attempts_per_approved"] = _ratio(r.get("attempts_approved"), r.get("approved"))
        out["attempts_per_held"] = _ratio(r.get("attempts_held"), r.get("held"))
    return out


def _stage_row(r: dict) -> dict:
    return {**r, "workflow": workflow_of(r["stage"]),
            "rung_share": {"primary": _ratio(r["primary_answered"], r["answered"]),
                           "rung1": _ratio(r["rung1"], r["answered"]),
                           "rung2plus": _ratio(r["rung2plus"], r["answered"]),
                           "unknown": _ratio(r["unknown_rung"], r["answered"])},
            "fallback_share": _ratio(r["fallback_answered"], r["answered"]),
            "cut_rate": _ratio(r["cut"], r["replies"])}


# ── compute ─────────────────────────────────────────────────────────────────────────────────────────────────────────


async def _part(conn, unavailable: dict, name: str, sql: str, *args) -> list[dict]:
    """Rows of one source; a missing table or column (an environment without that workflow) is noted, not raised."""
    import asyncpg

    try:
        async with conn.transaction():      # a savepoint inside a caller's transaction: a missing table aborts nothing
            return [_plain(r) for r in await conn.fetch(sql, *args)]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError, asyncpg.InsufficientPrivilegeError) as e:
        unavailable[name] = f"{type(e).__name__}: {str(e)[:200]}"
        return []


async def compute(conn, day: date, *, weeks: int = WEEKS, now: Optional[datetime] = None) -> dict:
    """The funnel snapshot of ``day`` (ICT): every workflow per week, every stage per week, and the watch window."""
    first, last, start, end = window(day, weeks)
    stops = sorted(TRUNCATED_STOP_REASONS)
    un: dict[str, str] = {}

    stages = [_stage_row(r) for r in await _part(conn, un, "stages", STAGES_SQL, start, stops, end)]
    stage_cost: dict[tuple[str, str], float] = {}
    for s in stages:
        key = (s["workflow"], s["week"])
        stage_cost[key] = stage_cost.get(key, 0.0) + float(s["cost_usd"] or 0)

    t9_cost = {(r["week"], r["channel"]): float(r["cost_usd"])
               for r in await _part(conn, un, "t9_cost", T9_COST_SQL, start, end)}
    t9 = [_object_row(r, t9_cost.get((r["week"], r["channel"]), 0.0))
          for r in await _part(conn, un, "t9_objects", T9_OBJECTS_SQL, start, end)]
    t9_unattributed = {}
    for (wf, week), total in stage_cost.items():
        if wf == "t9":
            attributed = sum(v for (w, _c), v in t9_cost.items() if w == week)
            t9_unattributed[week] = round(max(total - attributed, 0.0), 6)
    s1 = [_object_row(r, stage_cost.get(("s1", r["week"]), 0.0), HELD_IN_DELIVERED["s1"])
          for r in await _part(conn, un, "s1_objects", S1_OBJECTS_SQL, start, end)]
    t2 = [_object_row(r, stage_cost.get(("t2", r["week"]), 0.0))
          for r in await _part(conn, un, "t2_objects", T2_OBJECTS_SQL, start, end, list(T2_EDIT_SOURCES))]

    watch = await watch_stats(conn, day, un)
    return {
        "version": SNAPSHOT_VERSION,
        "day": day.isoformat(),
        "window": {"first_day": first.isoformat(), "last_day": last.isoformat(), "weeks": weeks,
                   "week_start": "ISO week, ICT, Monday"},
        "generated_at": (now or periods.now_utc()).isoformat(),
        "workflows": {
            "t9": {"by_week_channel": t9,
                   "gate_fails": await _part(conn, un, "t9_gates", T9_GATES_SQL, start, end),
                   "first_gate_holds": await _part(conn, un, "t9_first_gate", T9_FIRST_GATE_SQL, start, end),
                   "unattributed_cost_usd": t9_unattributed},
            "s1": {"by_week": s1, "gate_fails": await _part(conn, un, "s1_gates", S1_GATES_SQL, start, end)},
            "t2": {"by_week": t2, "gate_fails": None,
                   "note": "per-attempt T3 codes are not stored on the version"},
        },
        "stages": stages,
        "watch": watch,
        "thresholds": THRESHOLDS,
        "unavailable": un,
        "totals": totals(t9, s1, t2, last_week=(day - timedelta(days=day.weekday())).isoformat(),
                         t9_unattributed=t9_unattributed),
    }


def totals(t9: list[dict], s1: list[dict], t2: list[dict], *, last_week: str,
           t9_unattributed: Optional[dict[str, float]] = None) -> dict:
    """Headline per workflow over the whole window and for the snapshot day's week.

    LLM USD per delivered is the workflow's whole stage cost for every workflow. T9 rows carry only the cost attributed
    to a channel, so the T9 totals add the week's ``t9_unattributed`` stage cost: ``cost_usd`` / ``usd_per_delivered``
    are the whole T9 figure, ``attributed_cost_usd`` / ``attributed_usd_per_delivered`` the per-channel part and
    ``unattributed_cost_usd`` the rest."""
    unattributed = t9_unattributed or {}

    def agg(rows: list[dict], held_in_delivered: bool, extra_cost: Optional[float] = None) -> dict:
        keys = ("created", "delivered", "held", "failed", "first_pass", "attempts_delivered")
        s = {k: sum(int(r.get(k) or 0) for r in rows) for k in keys}
        cost = sum(float(r.get("cost_usd") or 0) for r in rows)
        out = {**s, **_rates(delivered=s["delivered"], held=s["held"], failed=s["failed"],
                             first_pass=s["first_pass"], attempts=s["attempts_delivered"],
                             cost=cost + (extra_cost or 0.0), held_in_delivered=held_in_delivered)}
        if extra_cost is not None:
            out["attributed_cost_usd"] = round(cost, 6)
            out["attributed_usd_per_delivered"] = round(cost / s["delivered"], 4) if s["delivered"] else None
            out["unattributed_cost_usd"] = round(extra_cost, 6)
        return out

    out = {}
    for name, rows in (("t9", t9), ("s1", s1), ("t2", t2)):
        held_in = HELD_IN_DELIVERED[name]
        week_rows = [r for r in rows if r["week"] == last_week]
        if name == "t9":
            out[name] = {"window": agg(rows, held_in, sum(float(v) for v in unattributed.values())),
                         "this_week": agg(week_rows, held_in, float(unattributed.get(last_week, 0.0)))}
        else:
            out[name] = {"window": agg(rows, held_in), "this_week": agg(week_rows, held_in)}
    return out


# ── watchdog ────────────────────────────────────────────────────────────────────────────────────────────────────────


def watch_policy(rows: dict[str, dict], stage: str) -> dict:
    """The thresholds of ``stage`` (pure): the route policy's ``fallback_alert_pct`` (``route_watch.effective``: the
    stage's own ``shared.llm_route_policy`` row, then the ``*`` row, then its defaults, field by field) and the minimum
    rows ``max(MIN_REQUESTS, min_requests)``: the policy can raise the weekly floor of a noisy stage, never lower it
    (its default of 5 is sized for route_watch's 60-minute window)."""
    pol = route_watch.effective(rows or {}, stage)
    return {"fallback_alert_pct": float(pol.fallback_alert_pct), "min_requests": max(MIN_REQUESTS, pol.min_requests),
            "policy_min_requests": pol.min_requests}


async def watch_stats(conn, day: date, unavailable: dict) -> dict:
    """Per stage over the trailing WATCH_DAYS days ending with ``day`` and the WATCH_DAYS before: answered, fallback,
    replies, cut, the models that served, the route's primary model and the stage's thresholds (route policy)."""
    end = periods.day_bounds(day)[1]
    recent = end - timedelta(days=WATCH_DAYS)
    prior = recent - timedelta(days=WATCH_DAYS)
    rows = await _part(conn, unavailable, "watch", WATCH_SQL, prior, sorted(TRUNCATED_STOP_REASONS), recent, end)
    routes = {r["stage"]: r["model_id"] for r in await _part(conn, unavailable, "routes", ROUTES_SQL)}
    policies = {r["stage"]: r for r in await _part(conn, unavailable, "route_policy", route_watch.POLICY_SQL)}
    return summarize_watch(rows, routes, recent=recent, end=end, policies=policies)


def summarize_watch(rows: list[dict], routes: dict[str, str], *, recent: datetime, end: datetime,
                    policies: Optional[dict[str, dict]] = None) -> dict:
    """Pure: the WATCH_SQL rows -> per stage numbers (sorted by stage)."""
    by: dict[str, dict] = {}
    for r in rows:
        s = by.setdefault(r["stage"], {"stage": r["stage"], "workflow": workflow_of(r["stage"]),
                                       "primary_model": routes.get(r["stage"]),
                                       "recent": {"answered": 0, "fallback_answered": 0, "replies": 0, "cut": 0,
                                                  "models": {}},
                                       "prior": {"answered": 0, "fallback_answered": 0, "replies": 0, "cut": 0,
                                                 "models": {}}})
        w = s[r["win"]]
        for k in ("answered", "fallback_answered", "replies", "cut"):
            w[k] += int(r.get(k) or 0)
        if int(r.get("answered") or 0):
            w["models"][r["model"]] = w["models"].get(r["model"], 0) + int(r["answered"])
    out = []
    for stage in sorted(by):
        s = by[stage]
        rec, pri = s["recent"], s["prior"]
        new_models = sorted(m for m, n in rec["models"].items()
                            if pri["answered"] and (pri["models"].get(m, 0) * 100.0 / pri["answered"])
                            < DRIFT_NEW_MODEL_PCT)
        new_answered = sum(rec["models"][m] for m in new_models)
        off_primary = sum(n for m, n in rec["models"].items() if s["primary_model"] and m != s["primary_model"])
        out.append({**s, "policy": watch_policy(policies or {}, stage),
                    "cut_pct": _pct(rec["cut"], rec["replies"]),
                    "fallback_pct": _pct(rec["fallback_answered"], rec["answered"]),
                    "drift_pct": _pct(new_answered, rec["answered"]) if pri["answered"] else None,
                    "drift_models": new_models,
                    "off_primary_pct": _pct(off_primary, rec["answered"]) if s["primary_model"] else None})
    return {"from": recent.isoformat(), "to": end.isoformat(), "days": WATCH_DAYS, "stages": out}


def watch_findings(watch: dict) -> list[Finding]:
    """Pure: the alerts the watch numbers cross (module doc). Thresholds per stage: ``stage["policy"]``
    (:func:`watch_policy`; a stage stored without it uses the module defaults)."""
    found: list[Finding] = []
    period = f"{watch.get('from', '')[:10]} to {watch.get('to', '')[:10]}"
    for s in watch.get("stages") or []:
        stage, rec, pri = s["stage"], s["recent"], s["prior"]
        pol = s.get("policy") or watch_policy({}, stage)
        min_n, fb_pct = int(pol["min_requests"]), float(pol["fallback_alert_pct"])
        if rec["replies"] >= min_n and (s["cut_pct"] or 0) > CUT_ALERT_PCT:
            found.append(Finding(CUT_PREFIX + stage, f"{stage}: {s['cut_pct']} % of {rec['replies']} billed replies "
                                 f"cut at their token ceiling ({period})",
                                 {"stage": stage, "cut": rec["cut"], "replies": rec["replies"],
                                  "threshold_pct": CUT_ALERT_PCT, "min_requests": min_n}))
        if rec["answered"] >= min_n and (s["fallback_pct"] or 0) >= fb_pct:
            found.append(Finding(FALLBACK_PREFIX + stage, f"{stage}: {s['fallback_pct']} % of {rec['answered']} "
                                 f"answered requests came from a fallback model ({period})",
                                 {"stage": stage, "fallback_answered": rec["fallback_answered"],
                                  "answered": rec["answered"], "threshold_pct": fb_pct, "min_requests": min_n}))
        if (rec["answered"] >= min_n and pri["answered"] >= min_n
                and (s["drift_pct"] or 0) >= DRIFT_ALERT_PCT):
            found.append(Finding(DRIFT_PREFIX + stage, f"{stage}: {s['drift_pct']} % of {rec['answered']} answered "
                                 f"requests came from {', '.join(s['drift_models'])}, new against the week before "
                                 f"({period})",
                                 {"stage": stage, "models": s["drift_models"], "recent": rec["models"],
                                  "prior": pri["models"], "primary_model": s["primary_model"],
                                  "threshold_pct": DRIFT_ALERT_PCT, "min_requests": min_n}))
    return found


def _drift_models(f: dict) -> set:
    return set((f.get("details") or {}).get("models") or [])


def mark_repeats(findings: list[dict], previous: Optional[list[dict]]) -> list[dict]:
    """Pure, the transition rule: each stored finding (``{"check", "summary", "details"}``) gets ``repeat`` True when
    the previous scheduled CIS-08 run already had it (the same check; for drift, no model that was not drifting
    then), so a trailing-window condition alerts on the night it starts, not on every night it stays in the window.
    ``previous`` None (no earlier scheduled run in reach) marks nothing as a repeat."""
    before: dict[str, dict] = {}
    for f in previous or []:
        if isinstance(f, dict) and f.get("check"):
            before[f["check"]] = f
    out = []
    for f in findings:
        prev = before.get(f["check"])
        repeat = prev is not None and (not f["check"].startswith(DRIFT_PREFIX)
                                       or _drift_models(f) <= _drift_models(prev))
        out.append({**f, "repeat": repeat})
    return out


def digest_finding(findings: list[dict], *, day: str = "") -> Optional[Finding]:
    """Pure: the night's NEW findings (``repeat`` not set) as one ``qf_digest`` alert, like route_watch's digest, so a
    provider outage across several stages is one bell and one e-mail. None when nothing is new."""
    new = [f for f in findings if not f.get("repeat")]
    if not new:
        return None
    repeats = [f["check"] for f in findings if f.get("repeat")]
    lines = [f.get("summary") or f["check"] for f in new[:DIGEST_MAX_LINES]]
    more = len(new) - len(lines)
    summary = (f"{len(new)} new quality funnel alert(s)" + (f" for {day}" if day else "") + ": " + "; ".join(lines)
               + (f"; and {more} more" if more > 0 else "")
               + (f" ({len(repeats)} still open from an earlier night, not raised again)" if repeats else ""))
    return Finding(QF_DIGEST_CHECK, summary,
                   {"day": day, "new": [f["check"] for f in new], "still_open": repeats,
                    "findings": [{"check": f["check"], "summary": f.get("summary"), "details": f.get("details") or {}}
                                 for f in new]})
