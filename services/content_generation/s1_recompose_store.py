"""AA-1049 path A-new — the DB side of the fixed-field recompose of published S1 Masters (``s1_recompose``).

One run (``scripts/quality/recompose_masters.py``, CIS api container through ``tools/ecs/ecsx.sh``):

1. ``load``: the active Masters of the AA master tenant in the set (``CANDIDATES_SQL``: the published version as the
   final contract reads it, ``final_contract_store.S1_ROW_COLUMNS``, plus the source state, a pending review, a newer
   version, an active pipeline job);
2. ``decide`` per Master (pure): ``recompose`` (a new version is due), ``resume`` (a recompose version of the served
   version was written but not published: publish it), or one skip reason — ``source_not_live``, ``pending_review``,
   ``newer_version``, ``job_active``, ``not_v2``, ``human_edited``, ``policy``, ``heading_parse``,
   ``narrative_changed``, ``no_op``, ``contract_worse``, ``error``;
3. ``apply_one`` (APPLY only): in one transaction, re-check that the Master still serves the decided version, insert
   the recomposed text as a NEW approved ``generated_content`` version (``insert_recomposed_version``: a copy of the
   served version with only ``aa_itineraries`` / ``seo_title`` / ``seo_meta`` replaced, ``metadata.recompose_of`` =
   the served version id, ``metadata.writer`` = ``recompose-v1``, its latest ``quality_scores`` row copied with
   today's failure codes), record the final contract (exit ``recompose``); then publish it through
   ``services.export.handler.process_export`` (exit ``recompose``): the lifecycle publish upsert, A3 atomize of the
   new version, the master source fan-out, ``updated_at`` (catalog ``text_updated_at``, TripPlanner, CRM) follow on
   their own. No model call anywhere in this module.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Awaitable, Callable, Optional

import structlog

from . import s1_recompose as rc

logger = structlog.get_logger()

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"   # aa_internal (admin_pipeline TENANT_SLUG_MAP)
ACTOR = "system:recompose"
RECOMPOSE, RESUME = "recompose", "resume"
DUE = (RECOMPOSE, RESUME)
SOURCE_NOT_LIVE = "source_not_live"
PENDING_REVIEW = "pending_review"
NEWER_VERSION = "newer_version"
JOB_ACTIVE = "job_active"
ERROR = "error"
# pipeline jobs that write or read the tour's Master text: a recompose waits until none is queued / running
ACTIVE_JOB_KINDS = ("s1_rewrite", "a3_atomize", "revalidate", "llm_batch")


def candidates_sql() -> str:
    """The active Masters of the master tenant with their served version; ``$2`` = tour ids (NULL = every one)."""
    from services.quality.final_contract_store import S1_ROW_COLUMNS, S1_ROW_FROM
    return f"""
        SELECT pt.id::text AS published_tour_id, rt.exclusions, rt.group_size, rt.best_time_to_go,
               (rt.tour_id IS NOT NULL AND rt.source_status::text = 'active' AND rt.deleted_at IS NULL) AS source_live,
               EXISTS (SELECT 1 FROM silver_aa_internal.review_queue rq
                        WHERE rq.tour_id = gc.tour_id AND rq.review_status = 'pending') AS pending_review,
               nv.id::text AS newer_id, nv.status::text AS newer_status, nv.recompose_of AS newer_recompose_of,
               EXISTS (SELECT 1 FROM shared.job j
                        WHERE j.kind = ANY($3::text[]) AND j.status IN ('queued', 'running')
                          AND (j.payload->>'tour_id' = gc.tour_id::text
                               OR (jsonb_typeof(j.payload->'tour_ids') = 'array'
                                   AND j.payload->'tour_ids' ? gc.tour_id::text))) AS job_active,
               {S1_ROW_COLUMNS}
          {S1_ROW_FROM}
          JOIN gold_aa_internal.published_tours pt ON pt.generated_content_id = gc.id
          LEFT JOIN LATERAL (
              SELECT n.id, n.status, n.metadata->>'recompose_of' AS recompose_of
                FROM silver_aa_internal.generated_content n
               WHERE n.tour_id = gc.tour_id AND n.version_num > gc.version_num
               ORDER BY n.version_num DESC
               LIMIT 1
          ) nv ON true
         WHERE pt.tenant_id = $1::uuid AND pt.deleted_at IS NULL AND pt.master_status = 'active'
           AND ($2::uuid[] IS NULL OR gc.tour_id = ANY($2::uuid[]))
         ORDER BY gc.tour_id
    """


WAVE_SQL = """
    SELECT id::text AS id, status, paused_at, expected_tour_ids::text[] AS expected
      FROM shared.rerun_wave WHERE id = $1::uuid
"""


async def wave_set(conn, wave_id: str) -> tuple[Optional[list[str]], str]:
    """(the wave's expected tour ids, "") when the wave is open and not paused, else (None, why)."""
    row = await conn.fetchrow(WAVE_SQL, wave_id)
    if row is None:
        return None, f"wave {wave_id[:8]} not found"
    if row["status"] != "open":
        return None, f"wave {wave_id[:8]} is {row['status']}, not open"
    if row["paused_at"] is not None:
        return None, f"wave {wave_id[:8]} is paused"
    return sorted(str(t) for t in row["expected"]), ""


async def load(conn, tour_ids: Optional[list[str]]) -> list[Any]:
    return list(await conn.fetch(candidates_sql(), MASTER_TENANT, tour_ids, list(ACTIVE_JOB_KINDS)))


# ── the decision (pure) ─────────────────────────────────────────────────────────────────────────────────────────


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def page_of(row: Any) -> dict:
    """The served page (highlights as a list), keyed like the S1 page."""
    return {"name": _get(row, "aa_name"), "subtitle": _get(row, "aa_subtitle"), "summary": _get(row, "aa_summary"),
            "description": _get(row, "aa_description"), "highlights": rc._json(_get(row, "aa_highlights"), []),
            "itineraries": _get(row, "aa_itineraries") or "", "seo_title": _get(row, "seo_title") or "",
            "seo_meta": _get(row, "seo_meta") or ""}


def tour_of(row: Any) -> dict:
    """The S1 source dict as the A1 job builds it from raw_tours, with the agency fields."""
    from .s1_writer_v2 import with_agency_fields
    tour = {"name": _get(row, "src_name"), "subtitle": _get(row, "src_subtitle"),
            "summary": _get(row, "src_summary"), "description": _get(row, "src_description"),
            "highlights": rc._json(_get(row, "src_highlights"), []), "itineraries": _get(row, "src_itineraries"),
            "country": _get(row, "country"), "duration": _get(row, "duration"), "price": _get(row, "price_raw"),
            "inclusions": _get(row, "inclusions"), "exclusions": _get(row, "exclusions")}
    return with_agency_fields(tour, {"group_size": _get(row, "group_size"),
                                     "best_time_to_go": _get(row, "best_time_to_go")})


def decide(row: Any, brand_words: list[str], *, include_human_edited: bool = False) -> dict:
    """One Master's decision: {tour_id, version_id, state, fields, changed, verdict, report, page?, failure_codes?}.
    ``state`` is RECOMPOSE / RESUME or a skip reason. Never raises (a failure is ``error``)."""
    from services.quality.s1_facts import build_fact_table
    tid, vid = str(_get(row, "tour_id")), str(_get(row, "generated_content_id"))
    out: dict = {"tour_id": tid, "version_id": vid, "fields": {}, "changed": [], "verdict": None, "report": {}}

    def _skip(state: str) -> dict:
        return {**out, "state": state}

    if not _get(row, "source_live"):
        return _skip(SOURCE_NOT_LIVE)
    if _get(row, "newer_id"):
        # a recompose of THIS version written but not published (a run stopped between insert and publish)
        if _get(row, "newer_recompose_of") == vid and _get(row, "newer_status") == "approved":
            return {**out, "state": RESUME, "new_version_id": str(_get(row, "newer_id"))}
        if _get(row, "newer_status") in ("pending", "approved"):
            return _skip(NEWER_VERSION)
    if _get(row, "pending_review"):
        return _skip(PENDING_REVIEW)
    if _get(row, "job_active"):
        return _skip(JOB_ACTIVE)
    md = rc._json(_get(row, "metadata"), {})
    if md.get("writer_version") != "v2":
        return _skip(rc.NOT_V2)
    if _get(row, "human_edited") and not include_human_edited:
        return _skip(rc.HUMAN_EDITED)
    try:
        with rc.no_model() as nm:
            tour = tour_of(row)
            facts = build_fact_table(tour)
            page = page_of(row)
            res = rc.recompose(page, tour, facts, brand_words)
            out.update(fields=res.fields, changed=res.changed, report=res.report)
            if not res.heading_ok:
                return _skip(rc.HEADING_PARSE)
            if not res.narrative_ok:
                return _skip(rc.NARRATIVE_CHANGED)
            if res.no_op:
                return _skip(rc.NO_OP)
            con = rc.contract_pair(row, page, res.page, tour, brand_words)
        if nm.attempts:
            out["report"]["model_call_refused"] = nm.attempts
            return _skip(ERROR)
    except Exception as e:  # noqa: BLE001 — one Master's failure is counted, the run goes on
        out["report"]["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return _skip(ERROR)
    out["verdict"] = {k: con[k] for k in ("old", "new", "stored", "old_blocks", "new_blocks")}
    if con["policy"]:
        return _skip(rc.POLICY)
    if con["worse"]:
        return _skip(rc.CONTRACT_WORSE)
    return {**out, "state": RECOMPOSE, "page": res.page, "failure_codes": con["new_failure_codes"]}


def pin_of(decisions: list[dict]) -> str:
    """``count:md5`` of the sorted ``tour_id:served version id`` of the Masters due (recompose + resume): the set an
    APPLY must find again (a publish, a new review or a rule change since the DRY RUN moves it)."""
    keys = sorted(f"{d['tour_id']}:{d['version_id']}" for d in decisions if d["state"] in DUE)
    return f"{len(keys)}:{hashlib.md5(','.join(keys).encode()).hexdigest()}"


def summary(decisions: list[dict]) -> dict:
    """Counts only: per state, per changed field (Masters due), per verdict move (old -> new, Masters due)."""
    states = Counter(d["state"] for d in decisions)
    due = [d for d in decisions if d["state"] in DUE]
    fields = Counter(f for d in due for f in d.get("changed") or [])
    verdicts = Counter(f"{d['verdict']['old']}->{d['verdict']['new']}" for d in due if d.get("verdict"))
    cut = sum(int((d.get("report") or {}).get("fact_clauses_cut") or 0) for d in due)
    return {"masters": len(decisions), "states": dict(sorted(states.items())), "fields": dict(sorted(fields.items())),
            "verdicts": dict(sorted(verdicts.items())), "fact_clauses_cut": cut, "pin": pin_of(decisions)}


def report_lines(decisions: list[dict]) -> list[str]:
    """The public-safe report: counts, then one line per Master (8-char ids, state, field names, verdicts)."""
    s = summary(decisions)
    lines = [f"masters={s['masters']} " + " ".join(f"{k}={v}" for k, v in s["states"].items()),
             "fields: " + ", ".join(f"{k}={v}" for k, v in s["fields"].items()),
             f"fact_clauses_cut={s['fact_clauses_cut']}",
             "verdicts: " + ", ".join(f"{k}={v}" for k, v in s["verdicts"].items())]
    for d in decisions:
        v = d.get("verdict") or {}
        verdict = f" verdict={v.get('old')}->{v.get('new')}" if v else ""
        err = f" error={d['report']['error'][:60]}" if (d.get("report") or {}).get("error") else ""
        lines.append(f"  {d['tour_id'][:8]} {d['state']:<17} {','.join(d.get('changed') or []) or '-'}{verdict}{err}")
    lines.append(f"PIN={s['pin']}")
    return lines


# ── the write ───────────────────────────────────────────────────────────────────────────────────────────────────

SERVED_SQL = """
    SELECT pt.generated_content_id::text AS version_id, pt.master_status::text AS master_status
      FROM gold_aa_internal.published_tours pt
     WHERE pt.tour_id = $1::uuid AND pt.tenant_id = $2::uuid AND pt.deleted_at IS NULL
     FOR UPDATE
"""

INSERT_VERSION_SQL = """
    INSERT INTO silver_aa_internal.generated_content (
        tour_id, tenant_id, version_num,
        aa_name, aa_subtitle, aa_summary, aa_description, aa_highlights, aa_itineraries,
        mobile_card_text, seo_title, seo_meta, seo_keywords_used, og_tags,
        model_editorial, model_schema, prompt_version, brand_rules_version,
        status, retry_count, metadata, fix_pass_applied, fix_pass_fields, requested_tier, fallback_used,
        human_edited, reviewed_by_legacy, edited_at, edit_diff, revalidate_passed, reviewed_by,
        satellite_used, satellite_account, fs_id
    )
    SELECT g.tour_id, g.tenant_id,
           (SELECT MAX(x.version_num) + 1 FROM silver_aa_internal.generated_content x WHERE x.tour_id = g.tour_id),
           g.aa_name, g.aa_subtitle, g.aa_summary, g.aa_description, g.aa_highlights, $2,
           g.mobile_card_text, $3, $4, g.seo_keywords_used, g.og_tags,
           g.model_editorial, g.model_schema, g.prompt_version, g.brand_rules_version,
           'approved'::content_status_enum, g.retry_count, $5::jsonb, g.fix_pass_applied, g.fix_pass_fields,
           g.requested_tier, g.fallback_used,
           g.human_edited, g.reviewed_by_legacy, g.edited_at, g.edit_diff, g.revalidate_passed, g.reviewed_by,
           g.satellite_used, g.satellite_account, g.fs_id
      FROM silver_aa_internal.generated_content g
     WHERE g.id = $1::uuid
    RETURNING id::text
"""

COPY_SCORE_SQL = """
    INSERT INTO silver_aa_internal.quality_scores (
        generated_content_id, tour_id, tenant_id, score_overall, score_brand, score_seo, score_structure,
        score_quality, failure_codes, issues, passed_count, failed_count, validator_fn_version, evaluated_at,
        brand_audit_status, brand_audit_codes, brand_audit_issues, brand_audit_fields, lessons_extracted
    )
    SELECT $2::uuid, q.tour_id, q.tenant_id, q.score_overall, q.score_brand, q.score_seo, q.score_structure,
           q.score_quality, $3::jsonb, q.issues, q.passed_count, q.failed_count, $4, NOW(),
           q.brand_audit_status, q.brand_audit_codes, q.brand_audit_issues, q.brand_audit_fields, q.lessons_extracted
      FROM silver_aa_internal.quality_scores q
     WHERE q.generated_content_id = $1::uuid
     ORDER BY q.evaluated_at DESC, q.id DESC
     LIMIT 1
"""

# metadata keys of the served version that do not describe the new one
_DROP_KEYS = ("final_contract", "final_contract_log", "s1_job_id")


def new_metadata(metadata: Any, decision: dict, at: str) -> dict:
    """The served version's metadata for the recompose version: the writer's own keys kept (writer_version,
    grounding, lineage: the narrative is the writer's), the contract log and the S1 job marker dropped, no model
    cost, and the recompose stamp."""
    md = {k: v for k, v in rc._json(metadata, {}).items() if k not in _DROP_KEYS}
    md.update(llm_cost_usd=0.0, recompose_of=decision["version_id"], writer=rc.WRITER,
              recompose={"of": decision["version_id"], "writer": rc.WRITER, "at": at,
                         "fields": list(decision.get("changed") or []),
                         "verdict": decision.get("verdict"), "report": decision.get("report") or {}})
    return md


async def insert_recomposed_version(conn, decision: dict) -> Optional[str]:
    """The recompose version of one Master, in the caller's transaction: refuses (None) when the Master no longer
    serves the decided version; else inserts the copy with the recomposed fixed fields, its score row (today's failure
    codes) and the final contract record of exit ``recompose``. Returns the new version id."""
    import datetime

    from services.quality.final_contract_store import record_s1_exit

    served = await conn.fetchrow(SERVED_SQL, decision["tour_id"], MASTER_TENANT)
    if served is None or served["master_status"] != "active" or served["version_id"] != decision["version_id"]:
        return None
    metadata = await conn.fetchval("SELECT metadata FROM silver_aa_internal.generated_content WHERE id = $1::uuid",
                                   decision["version_id"])
    at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    page = decision["page"]
    new_id = await conn.fetchval(INSERT_VERSION_SQL, decision["version_id"], page.get("itineraries"),
                                 page.get("seo_title"), page.get("seo_meta"),
                                 json.dumps(new_metadata(metadata, decision, at), default=str))
    await conn.execute(COPY_SCORE_SQL, decision["version_id"], new_id,
                       json.dumps(list(decision.get("failure_codes") or [])), rc.WRITER)
    # AA-1047 W1: the final contract of the stored recompose version (report mode, never raises)
    await record_s1_exit(conn, new_id, rc.EXIT, actual="approved")
    logger.info("s1_recompose_version_inserted", tour_id=decision["tour_id"], version_id=new_id,
                recompose_of=decision["version_id"], fields=decision.get("changed"))
    return new_id


Publish = Callable[..., Awaitable[dict]]


async def _default_publish(version_id: str, **kw) -> dict:
    from services.export.handler import process_export
    return await process_export(version_id, contract_exit=rc.EXIT, atomize_created_by=ACTOR, **kw)


async def apply_one(pool, decision: dict, *, publish: Optional[Publish] = None) -> dict:
    """Write and publish one due Master. {state: published | refused_moved | publish_failed, version_id?, ...}."""
    publish = publish or _default_publish
    new_id = decision.get("new_version_id") if decision["state"] == RESUME else None
    if new_id is None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                new_id = await insert_recomposed_version(conn, decision)
        if new_id is None:
            return {"state": "refused_moved"}
    try:
        res = await publish(new_id)
    except Exception as e:  # noqa: BLE001 — the version stays (a re-run resumes it, or the refusal moved it to hitl)
        logger.warning("s1_recompose_publish_failed", tour_id=decision["tour_id"], version_id=new_id,
                       error_type=type(e).__name__, error=str(e)[:200])
        return {"state": "publish_failed", "version_id": new_id, "error": type(e).__name__}
    return {"state": "published", "version_id": new_id, "atomize_job_id": (res or {}).get("atomize_job_id")}


__all__ = ["MASTER_TENANT", "ACTOR", "RECOMPOSE", "RESUME", "candidates_sql", "wave_set", "load", "decide",
           "pin_of", "summary", "report_lines", "new_metadata", "insert_recomposed_version", "apply_one"]
