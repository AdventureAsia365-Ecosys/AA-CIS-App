#!/usr/bin/env python3
"""AA-729 step 1 — pick the golden set and freeze it as a versioned fixture. Read-only, $0, deterministic.

Mix (decision memo S245 §1, option a): 50 S1/Master tours, 10 T2 tenant versions, 15 T9 blogs,
15 T9 LinkedIn posts, 10 synthetic prompt-injection samples. Inside each stratum the hard cases are
quotas filled first (common.QUOTAS, incl. a floor of one per minority S1 tour type), the rest is
spread over countries (S1) or tenants (T2/T9) and, inside each share, over tour types (S1), countries
(T2) or destinations (T9); the order is `random.Random(--seed)` over sorted ids, labelled samples
first (common.pick_stratified).

Per sample the fixture holds: ids (refs), stratum and pick reason, the frozen INPUT the writer needs to
regenerate it (S1: source tour + SEO + brand rules; T2: Master source + tenant brand rules + SEO; T9: the
writer context rebuilt from `writer_input` and the job payload), the frozen OUTPUT for score-only mode,
the pipeline's own verdict (kept apart from the label), and the label with its provenance
(`label_source`; `needs_panel` when no existing decision labels it — see docs/implementation-notes/AA-729.md
for what counts). Personal data is dropped / masked (common.scrub) and the writer asserts none is left.

Read-only by construction: every pool connection runs with `default_transaction_read_only = on`.
No LLM, no DataForSEO. Runs in the api container as `cis_app`:

  cd /app && PYTHONPATH=/tmp/aa729 python3 -m scripts.aa729.select_samples [--seed 729]
      [--version 1.0.0] [--audit-labels s3:scripts/in/aa729/audit-labels.json] [--no-upload]

Output: s3://<bronze>/scripts/out/aa729/fixture-<version>.json (+ /tmp/aa729-out/ copy) and a summary on
stdout. `--audit-labels` is an optional JSON {"<tour_id|version_id|piece_id>": {"verdict": "pass|fail",
"grade": "...", "source": "S234 r4 panel", "reason": "..."}} built from earlier audit rounds.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

sys.path.insert(0, os.getcwd())

from scripts.aa729 import injection  # noqa: E402
from scripts.aa729.common import (  # noqa: E402
    FIXTURE_VERSION, LONG_TOUR_DAYS, MASTER_TENANT_ID, MIX, PIECE_LABEL_VERDICT, POOR_RAW_PERCENTILE,
    QUOTAS, REVIEW_VERDICT, S3_PREFIX, SCHEMA, TOUR_TYPE_FLOORS, Candidate, DEFAULT_SEED, duration_days, find_pii,
    first_label,
    make_label, percentile, pick_stratified, save_output, scrub, tour_type,
)


def _j(v: Any, default: Any = None) -> Any:
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


async def _pool(read_only: bool = True):
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    settings = {"default_transaction_read_only": "on"} if read_only else {}
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=4,
                                     server_settings=settings)


# ── S1 / Master ──────────────────────────────────────────────────────────────────────────────────

_S1_SQL = """
    SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description,
           rt.src_highlights, rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions,
           rt.exclusions, rt.activities,
           length(coalesce(rt.src_itineraries, '')) + length(coalesce(rt.src_description, '')) AS raw_chars,
           pt.id::text AS published_tour_id, pt.master_status::text AS master_status,
           pt.generated_content_id::text AS published_gc_id,
           gc.id::text AS gc_id, gc.version_num, gc.status::text AS gc_status, gc.human_edited, gc.metadata,
           gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights, gc.aa_itineraries,
           gc.mobile_card_text, gc.seo_title, gc.seo_meta, gc.seo_keywords_used, gc.og_tags,
           qs.score_overall, qs.failure_codes, qs.brand_audit_status
    FROM silver_aa_internal.raw_tours rt
    LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id AND pt.deleted_at IS NULL
    JOIN LATERAL (
        SELECT g.* FROM silver_aa_internal.generated_content g
        WHERE g.tour_id = rt.tour_id
        ORDER BY (g.id = pt.generated_content_id) DESC NULLS LAST, g.version_num DESC
        LIMIT 1) gc ON true
    LEFT JOIN LATERAL (
        SELECT q.score_overall, q.failure_codes, q.brand_audit_status
        FROM silver_aa_internal.quality_scores q
        WHERE q.generated_content_id = gc.id
        ORDER BY q.evaluated_at DESC LIMIT 1) qs ON true
    WHERE rt.tenant_id = $1::uuid AND rt.source_status = 'active' AND rt.deleted_at IS NULL
"""

_S1_REVIEW_SQL = """
    SELECT rq.tour_id::text AS tour_id, rq.generated_content_id::text AS gc_id,
           rq.review_status::text AS review_status, rq.reviewer_notes, rq.failure_summary,
           (rq.reviewed_by IS NOT NULL) AS has_reviewer, rq.created_at
    FROM silver_aa_internal.review_queue rq
    WHERE rq.tenant_id = $1::uuid AND rq.tenant_tour_version_id IS NULL
    ORDER BY rq.created_at
"""

# Reviewer truth stamped on Jev verdicts (decision_log.outcome) of a tour's S1 runs. One scan, filtered on
# outcome (rare), joined to the s1_rewrite job that produced the verdict (job rows are kept 30 days).
_S1_JEV_OUTCOME_SQL = """
    SELECT j.payload->>'tour_id' AS tour_id, dl.question_key, dl.zone, dl.outcome
    FROM shared.decision_log dl
    JOIN shared.job j ON j.id = dl.job_id
    WHERE dl.outcome IS NOT NULL AND j.kind = 's1_rewrite'
"""

_LESSONS_SQL = """
    SELECT what_to_do, field FROM shared.pipeline_lessons
    WHERE stage IN ('S1_rewrite', 'S1_editor') AND is_active = true
    ORDER BY id
"""

_SEO_SQL = """
    SELECT keyword_search, keyword_ideas, top_keywords, people_also_ask, related_keywords
    FROM silver_aa_internal.seo_context
    WHERE tour_id = $1::uuid
    ORDER BY fetched_at DESC LIMIT 1
"""


def _s1_tour(r) -> dict:
    """The S1 writer's `tour` dict, same shape as admin_pipeline._execute_run_tour."""
    hl = _j(r["src_highlights"], [])
    return {"name": r["src_name"], "subtitle": r["src_subtitle"], "summary": r["src_summary"],
            "description": r["src_description"], "highlights": hl if isinstance(hl, list) else [],
            "itineraries": r["src_itineraries"], "country": r["country"], "duration": r["duration"],
            "price": r["price_raw"], "inclusions": r["inclusions"], "exclusions": r["exclusions"]}


def _s1_output(r) -> dict:
    """The frozen Master text, same shape as admin_pipeline._revalidate_tour's `generated`."""
    return {"name": r["aa_name"], "subtitle": r["aa_subtitle"], "summary": r["aa_summary"],
            "description": r["aa_description"], "highlights": _j(r["aa_highlights"], []) or [],
            "itineraries": r["aa_itineraries"], "mobile_card_text": r["mobile_card_text"],
            "seo_title": r["seo_title"], "seo_meta": r["seo_meta"],
            "seo_keywords_used": _j(r["seo_keywords_used"], []) or [], "og_tags": _j(r["og_tags"], {}) or {}}


def seo_from_row(row) -> dict:
    """seo_data in the S1 reuse shape (s1_prefetch.load_fresh) plus the flat/nested keyword keys
    _execute_run_tour normalises to — without load_fresh's freshness filter, so a frozen sample never
    triggers a DataForSEO purchase. Empty dict shape when the tour has no row."""
    if not row:
        return {"top_keywords": [], "keywords": {"top_keywords": []}, "people_also_ask": []}
    top = _j(row["top_keywords"], []) or []
    return {"keywords": {"top_keywords": top}, "top_keywords": top,
            "keyword_ideas": _j(row["keyword_ideas"], []) or [],
            "people_also_ask": _j(row["people_also_ask"], []) or [],
            "related_keywords": _j(row["related_keywords"], []) or [],
            "destination": row["keyword_search"]}


async def _s1_brand_rules(conn, brand_name: Optional[str], lessons_text: str) -> dict:
    """The Master brand rules exactly as _execute_run_tour builds them (+ the active S1 lessons)."""
    from api.routers.admin_pipeline import _resolve_brand_rule

    br = await _resolve_brand_rule(conn, MASTER_TENANT_ID, None, brand_name)
    rules: dict = {}
    if br:
        voice = _j(br["voice_examples"], []) or []
        rules = {"system_prompt": br["system_prompt"] or "", "style_guide": br["style_guide"] or "",
                 "forbidden_words": list(_j(br["forbidden_words"], []) or []), "rewrite_language": "en-US",
                 "core_idea": br.get("core_idea") or "", "customer_segment": br.get("customer_segment") or "",
                 "customer_mindset": br.get("customer_mindset") or "",
                 "voice_examples": list(voice) if isinstance(voice, list) else [],
                 "good_examples": br.get("good_examples") or "", "brand_type": br.get("brand_type") or ""}
    if lessons_text:
        rules["system_prompt"] = (rules.get("system_prompt") or "") + lessons_text
    return rules


def _lessons_text(rows) -> str:
    if not rows:
        return ""
    lines = []
    for r in rows:
        prefix = f"[{r['field']}] " if r["field"] else ""
        lines.append(f"- {prefix}{r['what_to_do']}")
    return ("\n\n## LESSONS FROM PREVIOUS BATCHES\n"
            "Apply these rules strictly — they come from real batch failures:\n" + "\n".join(lines))


def _outcome_truth(outcome: Any) -> Optional[bool]:
    o = _j(outcome, {})
    return o.get("truth") if isinstance(o, dict) and isinstance(o.get("truth"), bool) else None


def s1_label(r: dict, reviews: list[dict], jev: list[dict], audit: dict) -> tuple[dict, bool]:
    """(label, post_edit) for one S1 tour, in priority order: admin edit, review decision, reviewer
    truth on a Jev verdict, audit verdict, else needs_panel."""
    gc_id = r["gc_id"]
    ev = [{"review": x["review_status"], "summary": (x.get("failure_summary") or "")[:200]} for x in reviews]
    options = []
    if r.get("human_edited"):
        options.append(make_label("fail", "admin_edit", reason="the AI version was edited by an admin before use",
                                  evidence=ev))
    for x in reversed(reviews):  # newest decision on THIS version first
        if x["gc_id"] == gc_id and x["review_status"] in REVIEW_VERDICT:
            options.append(make_label(REVIEW_VERDICT[x["review_status"]], "human_review",
                                      reason=(x.get("reviewer_notes") or x.get("failure_summary") or "")))
            break
    falses = [x for x in jev if _outcome_truth(x["outcome"]) is False]
    if falses:
        options.append(make_label("fail", "jev_outcome",
                                  reason=f"{len(falses)} sentence(s) marked unsupported by a reviewer",
                                  evidence=[{"question": x["question_key"], "zone": x["zone"]} for x in falses[:5]]))
    a = audit.get(r["tour_id"]) or audit.get(gc_id)
    if a and a.get("verdict") in ("pass", "fail"):
        options.append(make_label(a["verdict"], "audit_verdict", grade=a.get("grade"),
                                  reason=f"{a.get('source', 'audit')}: {a.get('reason', '')}"))
    label = first_label(options or [None])
    if not label.get("evidence"):
        label["evidence"] = ev
    return label, bool(r.get("human_edited"))


async def s1_candidates(conn, audit: dict) -> tuple[list[Candidate], dict]:
    rows = [dict(r) for r in await conn.fetch(_S1_SQL, MASTER_TENANT_ID)]
    reviews: dict[str, list[dict]] = {}
    for x in await conn.fetch(_S1_REVIEW_SQL, MASTER_TENANT_ID):
        reviews.setdefault(x["tour_id"], []).append(dict(x))
    jev: dict[str, list[dict]] = {}
    for x in await conn.fetch(_S1_JEV_OUTCOME_SQL):
        if x["tour_id"]:
            jev.setdefault(x["tour_id"], []).append(dict(x))
    p_raw = percentile([float(r["raw_chars"] or 0) for r in rows], POOR_RAW_PERCENTILE) if rows else 0.0
    out = []
    for r in rows:
        label, post_edit = s1_label(r, reviews.get(r["tour_id"], []), jev.get(r["tour_id"], []), audit)
        days = duration_days(r["duration"], r["src_name"])
        ttype = tour_type(r["src_name"], r["activities"])
        in_master = r["master_status"] == "active" and r["published_gc_id"] == r["gc_id"]
        tags = set()
        if ttype == "cycle_bike" or ttype in TOUR_TYPE_FLOORS:
            tags.add(ttype)
        if days and days >= LONG_TOUR_DAYS:
            tags.add("long_18d")
        if float(r["raw_chars"] or 0) <= p_raw:
            tags.add("poor_raw")
        fc = _j(r["failure_codes"], []) or []
        known = (not in_master or label["verdict"] == "fail" or r["brand_audit_status"] == "manual_check"
                 or any(x["review_status"] in ("pending", "rejected") for x in reviews.get(r["tour_id"], []))
                 or "elephant" in f"{r['src_name']} {r['src_description'] or ''}".lower())
        if known:
            tags.add("known_failure")
        out.append(Candidate(id=r["tour_id"], group=r["country"] or "unknown", sub=ttype, tags=tags,
                             labelled=label["verdict"] is not None, data={
                                 "row": r, "label": label, "post_edit": post_edit, "days": days, "type": ttype,
                                 "pipeline": {"passed": in_master, "master_status": r["master_status"],
                                              "gc_status": r["gc_status"], "score": r["score_overall"],
                                              "failure_codes": fc, "brand_audit_status": r["brand_audit_status"]}}))
    return out, {"candidates": len(out), "poor_raw_chars_p15": p_raw}


async def freeze_s1(conn, c: Candidate, shared: dict, lessons_text: str) -> dict:
    r = c.data["row"]
    meta = _j(r["metadata"], {}) or {}
    brand_name = meta.get("brand_name") if isinstance(meta, dict) else None
    ref = f"s1:{brand_name or 'default'}"
    if ref not in shared:
        shared[ref] = await _s1_brand_rules(conn, brand_name, lessons_text)
    seo = seo_from_row(await conn.fetchrow(_SEO_SQL, r["tour_id"]))
    return {
        "kind": "s1", "country": r["country"], "tour_type": c.data["type"], "days": c.data["days"],
        "refs": {"tour_id": r["tour_id"], "generated_content_id": r["gc_id"], "version_num": r["version_num"],
                 "published_tour_id": r["published_tour_id"]},
        "input": {"tour": _s1_tour(r), "seo": seo, "brand_rules_ref": ref},
        "output": _s1_output(r), "output_post_edit": c.data["post_edit"],
        "pipeline_verdict": c.data["pipeline"], "label": c.data["label"],
    }


# ── T2 ───────────────────────────────────────────────────────────────────────────────────────────

_T2_SQL = """
    SELECT v.id::text AS version_id, v.tenant_id::text AS tenant_id, t.slug AS tenant_slug,
           v.published_tour_id::text AS published_tour_id, v.version_number, v.status, v.quality_score,
           v.qa_status, v.qa_repair_count, v.edit_source, v.rewritten_content, v.rewrite_language,
           pt.tour_id::text AS tour_id, rt.country, rt.duration, rt.src_name, rt.activities,
           EXISTS (SELECT 1 FROM gold_aa_internal.tenant_tour_versions c
                   WHERE c.parent_version_id = v.id AND c.edit_source = 'tenant_edit') AS tenant_edited
    FROM gold_aa_internal.tenant_tour_versions v
    JOIN shared.tenants t ON t.tenant_id = v.tenant_id
    JOIN gold_aa_internal.published_tours pt ON pt.id = v.published_tour_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
    WHERE v.edit_source IN ('ai_generated', 're_rewrite') AND v.status NOT IN ('pending', 'failed')
"""

_T2_REVIEW_SQL = """
    SELECT rq.tenant_tour_version_id::text AS version_id, rq.review_status::text AS review_status,
           rq.reviewer_notes, rq.failure_summary
    FROM silver_aa_internal.review_queue rq
    WHERE rq.tenant_tour_version_id IS NOT NULL
    ORDER BY rq.created_at
"""

# Human actions on a piece / tour version (admin sentence edits, tenant edits, tenant publish).
_AUDIT_SQL = """
    SELECT resource_id, action, count(*) AS n
    FROM acp_shared.audit_log
    WHERE action LIKE 'content_piece.admin_sentence_%' OR action LIKE 'tenant_tour_version.admin_sentence_%'
       OR action IN ('content_piece.edited', 'content_piece.published')
    GROUP BY resource_id, action
"""


def t2_label(r: dict, reviews: list[dict], actions: dict, audit: dict) -> tuple[dict, bool]:
    admin_edits = sum(n for a, n in actions.items() if a.startswith("tenant_tour_version.admin_sentence_"))
    options = []
    if admin_edits:
        options.append(make_label("fail", "admin_edit", reason=f"{admin_edits} admin sentence edit(s)"))
    if r["tenant_edited"]:
        options.append(make_label("fail", "tenant_edit", reason="the tenant saved an edited copy of this version"))
    for x in reversed(reviews):
        if x["review_status"] in REVIEW_VERDICT:
            options.append(make_label(REVIEW_VERDICT[x["review_status"]], "human_review",
                                      reason=x.get("reviewer_notes") or x.get("failure_summary") or ""))
            break
    if r["status"] in ("approved", "rejected"):
        options.append(make_label("pass" if r["status"] == "approved" else "fail", "tenant_review",
                                  reason=f"tenant {r['status']} the version"))
    a = audit.get(r["version_id"])
    if a and a.get("verdict") in ("pass", "fail"):
        options.append(make_label(a["verdict"], "audit_verdict", grade=a.get("grade"),
                                  reason=f"{a.get('source', 'audit')}: {a.get('reason', '')}"))
    return first_label(options or [None]), bool(admin_edits)


async def t2_candidates(conn, audit: dict, actions: dict) -> tuple[list[Candidate], dict]:
    rows = [dict(r) for r in await conn.fetch(_T2_SQL)]
    reviews: dict[str, list[dict]] = {}
    for x in await conn.fetch(_T2_REVIEW_SQL):
        reviews.setdefault(x["version_id"], []).append(dict(x))
    out = []
    for r in rows:
        label, post_edit = t2_label(r, reviews.get(r["version_id"], []), actions.get(r["version_id"], {}), audit)
        passed = r["status"] in ("ai_generated", "approved") and r["qa_status"] == "passed"
        tags = {"known_failure"} if (not passed or label["verdict"] == "fail") else set()
        out.append(Candidate(id=r["version_id"], group=r["tenant_slug"], sub=r["country"] or "unknown", tags=tags,
                             labelled=label["verdict"] is not None, data={
                                 "row": r, "label": label, "post_edit": post_edit,
                                 "pipeline": {"passed": passed, "status": r["status"], "qa_status": r["qa_status"],
                                              "score": r["quality_score"], "qa_repairs": r["qa_repair_count"]}}))
    return out, {"candidates": len(out)}


async def t2_seo(pool, tenant_id: str, tour_id: str) -> tuple[dict, str]:
    """The T2 job's own SEO lookup (t2_rewrite_job._rewrite_and_save): the tenant-market research cache,
    else the tour's admin seo_context row. Cache reads only."""
    from services.seo_intelligence.s1_prefetch import tenant_market_seo

    seo: dict = {}
    source = "none"
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT top_keywords, keyword_ideas, people_also_ask "
                                  "FROM silver_aa_internal.seo_context WHERE tour_id = $1::uuid "
                                  "ORDER BY fetched_at DESC LIMIT 1", tour_id)
        if row:
            top = _j(row["top_keywords"], []) or []
            seo = {"top_keywords": top, "keywords": {"top_keywords": top},
                   "people_also_ask": _j(row["people_also_ask"], []) or []}
        _market, mkt = await tenant_market_seo(conn, tenant_id, tour_id)
    if mkt:
        return mkt, "tenant_market"
    if seo:
        source = "admin_us_fallback"
    return seo, source


async def freeze_t2(pool, c: Candidate, shared: dict) -> dict:
    from services.jobs.t2_rewrite_job import _PT_SQL, _prepare

    r = c.data["row"]
    async with pool.acquire() as conn:
        pt = await conn.fetchrow(_PT_SQL, r["published_tour_id"])
    if not pt:
        raise ValueError("published tour gone")
    tour_dict, brand_rules = await _prepare(pool, pt, r["tenant_id"], r["rewrite_language"] or "EN-US")
    brand_loaded = bool(brand_rules.pop("_brand_loaded", False))
    ref = f"t2:{r['tenant_slug']}"
    shared.setdefault(ref, brand_rules)
    seo, seo_source = await t2_seo(pool, r["tenant_id"], r["tour_id"])
    rc = _j(r["rewritten_content"], {}) or {}
    output = {k: rc.get(k) for k in ("name", "subtitle", "summary", "highlights", "itineraries", "seo_title",
                                     "seo_meta", "trip_type")}
    return {
        "kind": "t2", "country": r["country"], "tour_type": tour_type(r["src_name"], r["activities"]),
        "days": duration_days(r["duration"], r["src_name"]),
        "refs": {"version_id": r["version_id"], "tenant_id": r["tenant_id"], "tenant_slug": r["tenant_slug"],
                 "published_tour_id": r["published_tour_id"], "tour_id": r["tour_id"]},
        "input": {"tour": dict(tour_dict), "seo": seo, "seo_source": seo_source, "brand_rules_ref": ref,
                  "brand_loaded": brand_loaded, "tenant_id": r["tenant_id"],
                  "rewrite_language": r["rewrite_language"] or "EN-US"},
        "output": output, "output_post_edit": c.data["post_edit"],
        "pipeline_verdict": c.data["pipeline"], "label": c.data["label"],
    }


# ── T9 ───────────────────────────────────────────────────────────────────────────────────────────

_T9_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, t.slug AS tenant_slug,
           cp.angle_gate_request_id::text AS request_id, cp.channel, cp.status, cp.held_reason,
           cp.content_text, cp.seo_title, cp.meta_description, cp.slug, cp.gate_ledger, cp.flags,
           cp.writer_input, cp.job_id::text AS job_id, cp.route_hub_name
    FROM acp_shared.content_piece cp
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE cp.channel = ANY($1::text[]) AND cp.status IN ('approved', 'held')
      AND cp.writer_input IS NOT NULL AND length(cp.content_text) > 0
"""

_PIECE_LABEL_SQL = """
    SELECT piece_id::text AS piece_id, label, defects, minutes_to_fix, reason, content_sha256, labelled_at
    FROM acp_shared.content_piece_label
    ORDER BY labelled_at
"""


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def t9_label(r: dict, labels: list[dict], actions: dict, audit: dict) -> tuple[dict, bool]:
    admin_edits = sum(n for a, n in actions.items() if a.startswith("content_piece.admin_sentence_"))
    tenant_edits = actions.get("content_piece.edited", 0)
    edited = bool(admin_edits or tenant_edits)
    options = []
    if labels:
        votes = Counter(PIECE_LABEL_VERDICT.get(x["label"]) for x in labels)
        verdict = "pass" if votes["pass"] > votes["fail"] else "fail"
        grades = [x["label"] for x in labels]
        shas = {x.get("content_sha256") for x in labels if x.get("content_sha256")}
        if shas:
            edited = _sha(r["content_text"]) not in shas  # the label describes the text as stored now?
        options.append(make_label(verdict, "human_label", grade="/".join(grades),
                                  reason=" | ".join((x.get("reason") or "") for x in labels if x.get("reason")),
                                  evidence=[{"defects": _j(x.get("defects"), {}),
                                             "minutes_to_fix": x.get("minutes_to_fix")} for x in labels]))
    if admin_edits:
        options.append(make_label("fail", "admin_edit", reason=f"{admin_edits} admin sentence edit(s)"))
    if tenant_edits:
        options.append(make_label("fail", "tenant_edit", reason=f"{tenant_edits} tenant hand edit(s)"))
    a = audit.get(r["piece_id"])
    if a and a.get("verdict") in ("pass", "fail"):
        options.append(make_label(a["verdict"], "audit_verdict", grade=a.get("grade"),
                                  reason=f"{a.get('source', 'audit')}: {a.get('reason', '')}"))
    label = first_label(options or [None])
    if actions.get("content_piece.published"):
        label["evidence"] = list(label.get("evidence") or []) + [{"tenant_published": True}]
    return label, edited


async def t9_candidates(conn, audit: dict, actions: dict) -> tuple[dict[str, list[Candidate]], dict]:
    rows = [dict(r) for r in await conn.fetch(_T9_SQL, ["blog", "linkedin"])]
    labels: dict[str, list[dict]] = {}
    try:
        for x in await conn.fetch(_PIECE_LABEL_SQL):
            labels.setdefault(x["piece_id"], []).append(dict(x))
    except Exception as e:  # noqa: BLE001 — table missing on an old DB: no human labels
        print(f"content_piece_label unreadable: {e}", flush=True)
    out: dict[str, list[Candidate]] = {"blog": [], "linkedin": []}
    for r in rows:
        wi = _j(r["writer_input"], {}) or {}
        if not isinstance(wi, dict) or not wi.get("seed"):
            continue
        label, post_edit = t9_label(r, labels.get(r["piece_id"], []), actions.get(r["piece_id"], {}), audit)
        tags = set()
        if r["status"] == "held":
            tags.add("held")
        if r["status"] == "held" or label["verdict"] == "fail":
            tags.add("known_failure")
        out[r["channel"]].append(Candidate(
            id=r["piece_id"], group=r["tenant_slug"], sub=wi.get("destination") or "unknown", tags=tags,
            labelled=label["verdict"] is not None,
            data={"row": r, "wi": wi, "label": label, "post_edit": post_edit,
                  "pipeline": {"passed": r["status"] == "approved", "status": r["status"],
                               "held_reason": (r["held_reason"] or "")[:300]}}))
    return out, {"candidates": {k: len(v) for k, v in out.items()}}


async def t9_context(pool, r: dict, wi: dict) -> tuple[dict, str]:
    """The T9 writer context for a stored piece: the t9_write job payload's context when the job row is
    still kept, else rebuilt from the angle request (goal, chosen angle); then the stored `writer_input`
    (what the writer really got: seed after the place-card re-read, route moments, Facts, brand pack,
    CTA, keyword, scope) laid over it, with `facts_pending = False` so nothing is re-read at score time."""
    from services.acp_angle_gate import service as angle_gate_service
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal

    ctx: Optional[dict] = None
    source = "job_payload"
    if r.get("job_id"):
        async with pool.acquire() as conn:
            payload = await conn.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", r["job_id"])
        payload = _j(payload, {}) or {}
        if isinstance(payload, dict) and isinstance(payload.get("context"), dict):
            ctx = copy.deepcopy(payload["context"])
    if ctx is None:
        source = "angle_request"
        req = await angle_gate_service.fetch_request(UUID(r["tenant_id"]), UUID(r["request_id"]), pool)
        chosen = next((a for a in req.get("angles") or [] if a.get("chosen")), None)
        goal = get_goal(req.get("goal") or wi.get("goal") or "")
        style = get_channel_style(r["channel"])
        if chosen is None or goal is None or style is None:
            raise ValueError("cannot rebuild the writer context (no chosen angle / goal / channel style)")
        ctx = {"goal": dict(goal), "channel_style": dict(style), "chosen": dict(chosen),
               "atom_id": req.get("atom_id"), "real_questions": [], "sibling_openers": [],
               "route_hub_name": r.get("route_hub_name"), "route_segment_count": None, "route_atoms": []}
    moments = wi.get("route_moments") or []
    ctx.update({
        "atom_text": wi.get("seed") or ctx.get("atom_text") or "",
        "route_segments": ([[m.get("atom_id"), m.get("seed")] for m in moments] if moments
                           else ctx.get("route_segments")),
        "facts_text": wi.get("facts") or "", "facts_pending": False,
        "brand_audience": wi.get("brand_pack") or ctx.get("brand_audience") or {},
        "brand_rubric_text": wi.get("brand_rubric") if wi.get("brand_rubric") is not None
        else ctx.get("brand_rubric_text"),
        "channel": r["channel"], "keyword": wi.get("keyword", ctx.get("keyword")),
        "cta": wi.get("cta") or ctx.get("cta"), "trip_name": wi.get("trip", ctx.get("trip_name")),
        "destination": wi.get("destination", ctx.get("destination")), "scope": wi.get("scope", ctx.get("scope")),
        "trip_places": wi.get("trip_places") or ctx.get("trip_places") or [],
        "tenant_id": r["tenant_id"],
    })
    if r["channel"] == "blog" and wi.get("search_questions") is not None:
        ctx["people_also_ask"] = [q.get("q") if isinstance(q, dict) else q for q in wi.get("search_questions") or []]
    ctx.setdefault("people_also_ask", [])
    for k in ("writer_trip_name", "paa_questions", "search_questions", "faq_questions"):
        ctx.pop(k, None)  # derived by run_write_background itself
    return ctx, source


async def freeze_t9(pool, c: Candidate, shared: dict) -> dict:
    r, wi = c.data["row"], c.data["wi"]
    ctx, source = await t9_context(pool, r, wi)
    return {
        "kind": "t9", "channel": r["channel"], "country": wi.get("destination") or "unknown",
        "refs": {"piece_id": r["piece_id"], "request_id": r["request_id"], "tenant_id": r["tenant_id"],
                 "tenant_slug": r["tenant_slug"], "job_id": r["job_id"]},
        "input": {"context": ctx, "context_source": source, "request_id": r["request_id"],
                  "piece_id": r["piece_id"], "tenant_id": r["tenant_id"]},
        "output": {"content_text": r["content_text"], "seo_title": r["seo_title"],
                   "meta_description": r["meta_description"], "slug": r["slug"],
                   "gate_ledger": _j(r["gate_ledger"], []) or [], "flags": _j(r["flags"], []) or []},
        "output_post_edit": c.data["post_edit"], "pipeline_verdict": c.data["pipeline"], "label": c.data["label"],
    }


# ── injection ────────────────────────────────────────────────────────────────────────────────────


def injection_samples(t9_samples: list[dict], s1_ref: str, seed: int) -> list[dict]:
    out = []
    for a in injection.S1_ATTACKS:
        out.append({
            "sample_id": a["id"], "stratum": "injection", "kind": "injection_s1", "country": a["country"],
            "tags": ["prompt_injection"], "pick_reason": "synthetic", "group": "synthetic",
            "refs": {}, "input": {"tour": injection.tour_source(a), "brand_rules_ref": s1_ref,
                                  "seo": seo_from_row(None)},
            "output": injection.compromised_s1_output(a), "output_post_edit": False,
            "pipeline_verdict": None, "injection_spec": injection.spec_of(a),
            "label": make_label("fail", "synthetic", reason="frozen output obeys the injected instruction; "
                                                            "a regenerated output must not (defended)"),
        })
    for a in injection.T9_ATTACKS:
        host = next((s for s in t9_samples if s["channel"] == a["channel"]), None)
        if host is None:
            print(f"no {a['channel']} host for {a['id']} — skipped", flush=True)
            continue
        ctx = copy.deepcopy(host["input"]["context"])
        ctx.update({"atom_text": injection.t9_seed(a), "route_segments": None, "route_atoms": [],
                    "scope": None, "trip_places": [], "people_also_ask": [], "real_questions": [],
                    "keyword": None, "facts_text": "", "trip_name": None, "destination": None,
                    "route_hub_name": None, "route_segment_count": None})
        piece_id = str(uuid.uuid5(UUID(int=seed), a["id"]))
        out.append({
            "sample_id": a["id"], "stratum": "injection", "kind": "injection_t9", "channel": a["channel"],
            "country": "synthetic", "tags": ["prompt_injection"], "pick_reason": "synthetic", "group": "synthetic",
            "refs": {"host_sample_id": host["sample_id"], "tenant_slug": host["refs"]["tenant_slug"]},
            "input": {"context": ctx, "context_source": "synthetic_seed_on_host",
                      "request_id": host["input"]["request_id"], "piece_id": piece_id,
                      "tenant_id": host["input"]["tenant_id"]},
            "output": {"content_text": injection.compromised_t9_output(a), "seo_title": None,
                       "meta_description": None, "slug": None, "gate_ledger": [], "flags": []},
            "output_post_edit": False, "pipeline_verdict": None, "injection_spec": injection.spec_of(a),
            "label": make_label("fail", "synthetic", reason="frozen output obeys the injected instruction; "
                                                            "a regenerated output must not (defended)"),
        })
    return out


# ── assembly ─────────────────────────────────────────────────────────────────────────────────────


async def pick_and_freeze(cands: list[Candidate], *, total: int, quotas, seed: int, prefix: str, stratum: str,
                          freeze, notes: list[str]) -> list[dict]:
    """Pick, freeze each pick; a pick that cannot be frozen is dropped and the pick is redone without it
    (at most 3 rounds), so the stratum still reaches `total` when candidates allow."""
    failed: set[str] = set()
    for _round in range(3):
        picks = pick_stratified([c for c in cands if c.id not in failed], total=total, quotas=quotas, seed=seed)
        frozen: list[dict] = []
        bad = False
        for i, (c, reason) in enumerate(picks, start=1):
            try:
                s = await freeze(c)
            except Exception as e:  # noqa: BLE001
                notes.append(f"{stratum}: {c.id} not frozen ({type(e).__name__}: {str(e)[:160]})")
                failed.add(c.id)
                bad = True
                continue
            s.update({"sample_id": f"{prefix}-{i:03d}", "stratum": stratum, "group": c.group,
                      "tags": sorted(c.tags), "pick_reason": reason,
                      "label_source": s["label"]["source"]})
            frozen.append(s)
        if not bad:
            return frozen
    for i, s in enumerate(frozen, start=1):
        s["sample_id"] = f"{prefix}-{i:03d}"
    return frozen


async def build(seed: int, version: str, audit: dict) -> dict:
    pool = await _pool(read_only=True)
    notes: list[str] = []
    shared: dict = {}
    try:
        async with pool.acquire() as conn:
            lessons_text = _lessons_text(await conn.fetch(_LESSONS_SQL))
            actions: dict[str, dict[str, int]] = {}
            for x in await conn.fetch(_AUDIT_SQL):
                actions.setdefault(x["resource_id"], {})[x["action"]] = int(x["n"])
            s1_c, s1_info = await s1_candidates(conn, audit)
            t2_c, t2_info = await t2_candidates(conn, audit, actions)
            t9_c, t9_info = await t9_candidates(conn, audit, actions)

        async def _f_s1(c):
            async with pool.acquire() as conn:
                return await freeze_s1(conn, c, shared, lessons_text)

        s1 = await pick_and_freeze(s1_c, total=MIX["s1"], quotas=QUOTAS["s1"], seed=seed, prefix="s1",
                                   stratum="s1", freeze=_f_s1, notes=notes)
        t2 = await pick_and_freeze(t2_c, total=MIX["t2"], quotas=QUOTAS["t2"], seed=seed, prefix="t2",
                                   stratum="t2", freeze=lambda c: freeze_t2(pool, c, shared), notes=notes)
        t9b = await pick_and_freeze(t9_c["blog"], total=MIX["t9_blog"], quotas=QUOTAS["t9_blog"], seed=seed,
                                    prefix="t9b", stratum="t9_blog", freeze=lambda c: freeze_t9(pool, c, shared),
                                    notes=notes)
        t9l = await pick_and_freeze(t9_c["linkedin"], total=MIX["t9_linkedin"], quotas=QUOTAS["t9_linkedin"],
                                    seed=seed, prefix="t9l", stratum="t9_linkedin",
                                    freeze=lambda c: freeze_t9(pool, c, shared), notes=notes)
    finally:
        await pool.close()

    if not shared.get("s1:default") and s1:
        s1_ref = Counter(s["input"]["brand_rules_ref"] for s in s1).most_common(1)[0][0]
    else:
        s1_ref = "s1:default"
    inj = injection_samples(t9b + t9l, s1_ref, seed)
    for s in inj:
        s["label_source"] = s["label"]["source"]
    samples = s1 + t2 + t9b + t9l + inj
    fixture = {
        "schema": SCHEMA, "fixture_version": version, "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed, "mix": MIX, "quotas": {k: [list(q) for q in v] for k, v in QUOTAS.items()},
        "selection": {"s1": s1_info, "t2": t2_info, "t9": t9_info, "notes": notes},
        "shared_inputs": {"brand_rules": shared},
        "samples": samples,
    }
    return scrub(fixture)


def summary(fixture: dict) -> dict:
    samples = fixture["samples"]
    by = Counter(s["stratum"] for s in samples)
    src = Counter((s["stratum"], s["label_source"]) for s in samples)
    return {
        "fixture_version": fixture["fixture_version"], "samples": len(samples), "by_stratum": dict(by),
        "label_sources": {f"{a}/{b}": n for (a, b), n in sorted(src.items())},
        "needs_panel": sum(1 for s in samples if s["label_source"] == "needs_panel"),
        "tags": dict(Counter(t for s in samples for t in s.get("tags") or [])),
        "countries_s1": dict(Counter(s["country"] for s in samples if s["stratum"] == "s1")),
        "groups_t": dict(Counter(f"{s['stratum']}:{s['group']}" for s in samples if s["stratum"].startswith("t"))),
        "notes": fixture["selection"]["notes"][:20],
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--version", default=FIXTURE_VERSION)
    ap.add_argument("--audit-labels", help="local path or s3:<key> of an audit-verdict JSON")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args(argv)
    from scripts.aa729.common import load_json

    audit = load_json(a.audit_labels) if a.audit_labels else {}
    fixture = asyncio.run(build(a.seed, a.version, audit))
    leaks = find_pii(fixture)
    if leaks:
        print(f"PII check failed: {leaks[:10]}", flush=True)
        return 2
    save_output(fixture, f"{S3_PREFIX}/fixture-{a.version}.json", upload=not a.no_upload)
    print(json.dumps(summary(fixture), indent=1, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
