"""AA-1052 W6 (root ADR 0020 layer 3 "lineage and versions") — trace every writer output to the versions it was made
with, from stored fields only ($0, read-only).

Four lineage fields per object:

* ``policy_sha``            the compiled writing policy (``services/quality/writing_policy.yaml``) in force at the
                            exit;
* ``fs_id``                 the FactSheet the writer read (``acp_shared.fact_sheet``, CIS 386);
* ``prompt_version``        the writer's own stamp (sha256[:8] of the exact system prompt; the eval: the judge
                            version) plus, from the ledger, the registered ``prompt_version`` of every stage that
                            answered for the object;
* ``route_config_version``  the route config version in force (``shared.llm_route_settings.current_version``) at
                            the exit, plus the versions the ledger trigger stamped on the object's calls.

Where each lives (no migration: existing columns and jsonb):

========  =====================================================================================================
S1        ``silver_aa_internal.generated_content``: ``fs_id``; ``metadata.prompt_version``;
          ``metadata.final_contract.lineage`` (``policy_sha``, ``route_config_version``); ledger rows by the run's
          job (``metadata.s1_job_id``), else by the tour in the hours before the version
T2        ``gold_aa_internal.tenant_tour_versions``: ``fs_id``; ``generation_meta.writer_prompt_version``;
          ``judge_breakdown.final_contract.lineage``; ledger rows by ``job_id``
T9        ``acp_shared.content_piece``: ``writer_input.fs_id`` / ``writer_prompt_version`` /
          ``final_contract.lineage``; ledger rows by ``content_piece_id`` (or the piece's job)
eval      ``shared.eval_item_result``: ``judge_version``; its run's fingerprint (``prompt_eval_runs.fingerprint
          .config_fp``: ``parts.policy`` and ``route_config_version``); ledger rows by ``quality_signal.run_id`` /
          ``item_id``. An eval item has no FactSheet (the golden item is its input): ``fs_id`` is not expected.
========  =====================================================================================================

``stamp()`` is what the final contract's store step (``final_contract.stamp_stored``) writes under ``lineage`` on
every S1 / T2 / T9 exit: pure, no I/O (the route version is the one the gateway last read in this process,
``route_settings.cached_version``; None before any model call of the process).

``assemble`` (pure) builds one object's lineage from its row and ledger rows; ``coverage_from_row`` (pure) the share
of recent objects that carry each field. The SQL is here too; ``api/routers/admin_quality_lineage.py`` runs it.
Table and column names: root ``docs/architecture/db-schema-reference.md``.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

LINEAGE_VERSION = "lineage-v1"

S1, T2, T9, EVAL = "s1", "t2", "t9", "eval"
WORKFLOWS = (S1, T2, T9, EVAL)
FIELDS = ("policy_sha", "fs_id", "prompt_version", "route_config_version")
# An eval item has no FactSheet: the field is not expected there (never "missing").
EXPECTED: dict[str, tuple[str, ...]] = {S1: FIELDS, T2: FIELDS, T9: FIELDS,
                                        EVAL: ("policy_sha", "prompt_version", "route_config_version")}
COVERAGE_DAYS_MAX = 90


def stamp() -> dict:
    """The lineage the store step writes next to the final contract record. Never raises, no I/O."""
    out: dict[str, Any] = {"v": LINEAGE_VERSION, "policy_sha": None, "route_config_version": None}
    try:
        from services.quality import writing_policy
        out["policy_sha"] = writing_policy.policy_sha()
    except Exception:  # noqa: BLE001 — a lineage stamp never fails the exit it rides on
        pass
    try:
        from shared.llm_client import route_settings
        out["route_config_version"] = route_settings.cached_version()
    except Exception:  # noqa: BLE001
        pass
    return out


# ── SQL (read-only) ───────────────────────────────────────────────────────────────────────────────────────────────

# One object, by its id: the stored lineage fields and the keys the ledger read needs.
OBJECT_SQL: dict[str, str] = {
    S1: """
        SELECT gc.id::text AS object_id, gc.tour_id::text AS tour_id, gc.tenant_id::text AS tenant_id,
               gc.created_at, gc.status::text AS status, gc.fs_id::text AS fs_id,
               NULLIF(gc.metadata->>'prompt_version', '') AS writer_prompt_version,
               gc.metadata->'final_contract'->'lineage' AS stamp,
               NULLIF(gc.metadata->>'s1_job_id', '') AS job_id
          FROM silver_aa_internal.generated_content gc
         WHERE gc.id = $1::uuid""",
    T2: """
        SELECT ttv.id::text AS object_id, ttv.published_tour_id::text AS tour_id, ttv.tenant_id::text AS tenant_id,
               ttv.created_at, ttv.status, ttv.fs_id::text AS fs_id,
               NULLIF(ttv.generation_meta->>'writer_prompt_version', '') AS writer_prompt_version,
               ttv.judge_breakdown->'final_contract'->'lineage' AS stamp, ttv.job_id::text AS job_id
          FROM gold_aa_internal.tenant_tour_versions ttv
         WHERE ttv.id = $1::uuid""",
    T9: """
        SELECT cp.piece_id::text AS object_id, NULL::text AS tour_id, cp.tenant_id::text AS tenant_id,
               cp.created_at, cp.status, NULLIF(cp.writer_input->>'fs_id', '') AS fs_id,
               NULLIF(cp.writer_input->>'writer_prompt_version', '') AS writer_prompt_version,
               cp.writer_input->'final_contract'->'lineage' AS stamp, cp.job_id::text AS job_id
          FROM acp_shared.content_piece cp
         WHERE cp.piece_id = $1::uuid""",
    EVAL: """
        SELECT r.result_id::text AS object_id, r.item_id::text AS item_id, r.run_id::text AS run_id,
               r.tenant_id::text AS tenant_id, r.created_at, r.verdict AS status, NULL::text AS fs_id,
               r.judge_version AS writer_prompt_version,
               jsonb_build_object(
                   'policy_sha', p.fingerprint->'config_fp'->'parts'->>'policy',
                   'route_config_version', p.fingerprint->'config_fp'->'route_config_version',
                   'config_fp', p.fingerprint->'config_fp'->>'sha') AS stamp,
               p.prompt_version AS run_prompt_version, p.created_at AS run_created_at,
               COALESCE(p.finished_at, now()) AS run_finished_at
          FROM shared.eval_item_result r
          JOIN shared.prompt_eval_runs p ON p.id = r.run_id
         WHERE r.result_id = $1::uuid""",
}

# The object's model calls, one row per (stage, prompt_version, route_config_version, model). $1.. per workflow:
# S1 (job_id text|NULL, tour_id, created_at), T2 (job_id), T9 (piece_id, job_id text|NULL),
# eval (run_id text, item_id text, run_created_at, run_finished_at).
_LEDGER_COLUMNS = """
        SELECT stage, prompt_version, route_config_version, model, count(*)::int AS calls,
               min(created_at) AS first_at, max(created_at) AS last_at
          FROM shared.llm_call_log"""
_LEDGER_GROUP = " GROUP BY stage, prompt_version, route_config_version, model ORDER BY min(created_at), stage"
LEDGER_SQL: dict[str, str] = {
    S1: _LEDGER_COLUMNS + """
         WHERE CASE WHEN $1::text IS NOT NULL THEN job_id = $1::uuid
                    ELSE tour_id = $2::uuid AND stage LIKE 's1\\_%'
                         AND created_at BETWEEN $3::timestamptz - interval '3 hours'
                                            AND $3::timestamptz + interval '5 minutes' END""" + _LEDGER_GROUP,
    T2: _LEDGER_COLUMNS + " WHERE job_id = $1::uuid" + _LEDGER_GROUP,
    T9: _LEDGER_COLUMNS + """
         WHERE content_piece_id = $1::uuid
            OR ($2::text IS NOT NULL AND job_id = $2::uuid AND content_piece_id IS NULL)""" + _LEDGER_GROUP,
    EVAL: _LEDGER_COLUMNS + """
         WHERE created_at BETWEEN $3::timestamptz - interval '5 minutes' AND $4::timestamptz + interval '5 minutes'
           AND quality_signal->>'run_id' = $1 AND quality_signal->>'item_id' = $2""" + _LEDGER_GROUP,
}

# How a workflow's objects are counted over the last $1 days: one row of counts per workflow.
COVERAGE_SQL: dict[str, str] = {
    S1: """
        SELECT count(*)::int AS objects, count(gc.fs_id)::int AS fs_id,
               count(NULLIF(gc.metadata->>'prompt_version', ''))::int AS prompt_version,
               count(gc.metadata->'final_contract'->'lineage'->>'policy_sha')::int AS policy_sha,
               count(gc.metadata->'final_contract'->'lineage'->>'route_config_version')::int AS route_config_version
          FROM silver_aa_internal.generated_content gc
         WHERE gc.created_at >= now() - make_interval(days => $1::int)""",
    T2: """
        SELECT count(*)::int AS objects, count(ttv.fs_id)::int AS fs_id,
               count(NULLIF(ttv.generation_meta->>'writer_prompt_version', ''))::int AS prompt_version,
               count(ttv.judge_breakdown->'final_contract'->'lineage'->>'policy_sha')::int AS policy_sha,
               count(ttv.judge_breakdown->'final_contract'->'lineage'->>'route_config_version')::int
                   AS route_config_version
          FROM gold_aa_internal.tenant_tour_versions ttv
         WHERE ttv.created_at >= now() - make_interval(days => $1::int)
           AND ttv.edit_source IN ('ai_generated', 're_rewrite')""",
    T9: """
        SELECT count(*)::int AS objects, count(NULLIF(cp.writer_input->>'fs_id', ''))::int AS fs_id,
               count(NULLIF(cp.writer_input->>'writer_prompt_version', ''))::int AS prompt_version,
               count(cp.writer_input->'final_contract'->'lineage'->>'policy_sha')::int AS policy_sha,
               count(cp.writer_input->'final_contract'->'lineage'->>'route_config_version')::int
                   AS route_config_version
          FROM acp_shared.content_piece cp
         WHERE cp.created_at >= now() - make_interval(days => $1::int)
           AND cp.status <> 'processing'""",
    EVAL: """
        SELECT count(*)::int AS objects, 0::int AS fs_id,
               count(NULLIF(r.judge_version, ''))::int AS prompt_version,
               count(p.fingerprint->'config_fp'->'parts'->>'policy')::int AS policy_sha,
               count(p.fingerprint->'config_fp'->>'route_config_version')::int AS route_config_version
          FROM shared.eval_item_result r
          JOIN shared.prompt_eval_runs p ON p.id = r.run_id
         WHERE r.created_at >= now() - make_interval(days => $1::int)
           AND r.verdict IN ('pass', 'fail')""",
}


def ledger_args(workflow: str, row: Any) -> tuple:
    """The positional arguments of ``LEDGER_SQL[workflow]`` for one ``OBJECT_SQL`` row."""
    g = (lambda k: _get(row, k))
    if workflow == S1:
        return (g("job_id"), g("tour_id"), g("created_at"))
    if workflow == T2:
        return (g("job_id"),)
    if workflow == T9:
        return (g("object_id"), g("job_id"))
    if workflow == EVAL:
        return (g("run_id"), g("item_id"), g("run_created_at"), g("run_finished_at"))
    raise ValueError(f"unknown workflow {workflow!r}")


def can_read_ledger(workflow: str, row: Any) -> bool:
    """Whether the object has a key the ledger can be read by (a T2 version without a job has none)."""
    if workflow == T2:
        return bool(_get(row, "job_id"))
    if workflow == S1:
        return bool(_get(row, "job_id") or _get(row, "tour_id"))
    return True


# ── pure assembly ─────────────────────────────────────────────────────────────────────────────────────────────────

def _get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _version(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


def assemble(workflow: str, row: Any, ledger: Iterable[Any] = (), *, ledger_join: Optional[str] = None) -> dict:
    """One object's lineage: the stored fields, what its calls' ledger rows say, and the expected fields it lacks."""
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}")
    st = _json(_get(row, "stamp")) or {}
    st = st if isinstance(st, dict) else {}
    calls = [{"stage": _get(r, "stage"), "prompt_version": _get(r, "prompt_version"),
              "route_config_version": _version(_get(r, "route_config_version")), "model": _get(r, "model"),
              "calls": int(_get(r, "calls") or 0), "first_at": _iso(_get(r, "first_at")),
              "last_at": _iso(_get(r, "last_at"))} for r in ledger or ()]
    stage_versions: dict[str, list[str]] = {}
    for c in calls:
        if c["stage"] and c["prompt_version"]:
            seen = stage_versions.setdefault(c["stage"], [])
            if c["prompt_version"] not in seen:
                seen.append(c["prompt_version"])
    ledger_routes = sorted({c["route_config_version"] for c in calls if c["route_config_version"] is not None})
    stamped_route = _version(st.get("route_config_version"))
    out = {
        "workflow": workflow,
        "object_id": _get(row, "object_id"),
        "tenant_id": _get(row, "tenant_id"),
        "created_at": _iso(_get(row, "created_at")),
        "status": _get(row, "status"),
        "policy_sha": st.get("policy_sha") or None,
        "fs_id": _get(row, "fs_id") or None,
        "prompt_version": {"writer": _get(row, "writer_prompt_version") or None, "stages": stage_versions},
        "route_config_version": {"stamped": stamped_route, "ledger": ledger_routes},
        "calls": calls,
        "ledger_join": ledger_join,
        "lineage_version": st.get("v") or None,
    }
    if workflow == EVAL:
        out["run"] = {"run_id": _get(row, "run_id"), "item_id": _get(row, "item_id"),
                      "prompt_version": _get(row, "run_prompt_version"), "config_fp": st.get("config_fp") or None}
    elif _get(row, "tour_id"):
        out["tour_id"] = _get(row, "tour_id")
    have = {"policy_sha": bool(out["policy_sha"]), "fs_id": bool(out["fs_id"]),
            "prompt_version": bool(out["prompt_version"]["writer"] or stage_versions),
            "route_config_version": stamped_route is not None or bool(ledger_routes)}
    out["missing"] = [f for f in EXPECTED[workflow] if not have[f]]
    out["complete"] = not out["missing"]
    return out


def coverage_from_row(workflow: str, row: Any) -> dict:
    """{objects, fields: {field: {count, share}}, complete_share_floor} of one ``COVERAGE_SQL`` row. ``fs_id`` of the
    eval is reported as not expected (null)."""
    n = int(_get(row, "objects") or 0)
    fields: dict[str, Optional[dict]] = {}
    for f in FIELDS:
        if f not in EXPECTED[workflow]:
            fields[f] = None
            continue
        k = int(_get(row, f) or 0)
        fields[f] = {"count": k, "share": round(k / n, 4) if n else None}
    shares = [v["share"] for v in fields.values() if v is not None and v["share"] is not None]
    # the share of objects that carry EVERY expected field is at least this (each count is per field)
    floor = round(max(0.0, 1.0 - sum(1.0 - s for s in shares)), 4) if shares else None
    return {"workflow": workflow, "objects": n, "fields": fields, "complete_share_floor": floor}


__all__ = ["COVERAGE_DAYS_MAX", "COVERAGE_SQL", "EVAL", "EXPECTED", "FIELDS", "LEDGER_SQL", "LINEAGE_VERSION",
           "OBJECT_SQL", "S1", "T2", "T9", "WORKFLOWS", "assemble", "can_read_ledger", "coverage_from_row",
           "ledger_args", "stamp"]
