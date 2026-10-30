#!/usr/bin/env python3
"""AA-645 (S249) — shadow run of the live A1 chain with the S1 writer v2 (W x Sonnet) on chosen tours. PAID, writes
NOTHING to app data.

Plan (S248 platform-impact memo section 6 step 1, Nghiep d03): pilot 3 tours, then <= 50, before any real wave. Per tour
it runs `api.routers.v1_pipeline._rewrite_tour` — the exact graph the A1 job runs (`admin_pipeline._execute_run_tour`
calls it): writer v2 + meta, validate (day oracle, code gates), judge (brand fit report-only on a v2 draft, d02; rubric
v2 once the S249 code is deployed), the retry loop, brand audit, grounding + Jev `a1_claim_supported`, flag_fix,
revalidate — plus `_execute_run_tour`'s AA-736 Sonnet retry rule. It stops before the persist step: no
generated_content version, no review row, no publish, no export, no atomize, no place work.

Why `_rewrite_tour` and not `scripts/aa645/s1_screen.py`: the screen runs the writer + meta calls only (no validate /
judge / grounding / flag_fix / Jev). The questions here (judge pass rate, attempts per run, code gates after flag_fix,
Jev, real $ of the chain) need the whole graph, and `_rewrite_tour` is that graph with nothing persisted. The AA-729
golden-set harness already runs S1 this way (`scripts/aa729/score.py --mode regenerate`); this script reuses its
isolation layer, `scripts/aa729/evalpatch.install(run_id, stage="adhoc_aa645")`:

* every llm_call_log row (writer, judge, flag_fix, grounding repair, Jev) is written under stage `adhoc_aa645`, the
  real stage in `quality_signal.aa729_stage`, the run id in `quality_signal.aa729_run`, and the tour's own job id
  (uuid5(run, tour)) in `job_id` — real $ per tour = SUM(cost_usd) of its job id; Jev verdicts go to decision_log
  under `adhoc_aa645:<stage>`;
* no pipeline lesson is written, no LLMClient shadow call is made;
* the in-call cost stop (`evalpatch.BUDGET`) refuses any model call once spend + one call margin per running tour
  reaches `--max-cost-usd`, and a launch guard (`common.CostGuard`) starts a tour only while it fits.

Writer v2 is switched on IN THIS PROCESS ONLY (`S1_WRITER_V2=1` in os.environ; the worker task definition is not
touched) and its writer + meta calls are sent to `--writer-model` (default `sonnet-5-5`, d01 W x Sonnet) as the
request's model_tier — the other stages keep their live routes (s1_judge, s1_flag_fix Haiku, grounding). Pass
`--writer-model route` once the s1_generate route itself is Sonnet 5.5. SEO keywords come from the cached
`silver_aa_internal.seo_context` (the live chain would call DataForSEO, ~$16 a wave): no DataForSEO call.

Tour selection (`--select`):
* `pilot` — `--n` (default 3) published Laos tours (`--pilot-country`) that have built places (geo.tour_build ok /
  partial — the memo wants the component-id change measured on them), then other published tours of that country;
* `wave` — `--n` (<= 50, enforced) published tours stratified by country (largest remainder, at least one per country
  while n allows), md5(tour_id || seed) order inside a country;
* `ids` — the tour ids in `--tour-ids-file`.
Always excluded: the golden tours (`--exclude-ids-file`, the ids of fixture 1.1.0 — refs.tour_id of every sample),
elephant-riding tours (brand_audit_node.ELEPHANT_RIDING on the source; keep-as-is policy) and any other
`--exclude-ids-file` (e.g. the pilot's tours for the wave).

Per tour: judge pass (brand fit >= 7 on every judged draft, per rubric), attempts per run, code gates after flag_fix
(the A/B checks, scripts/aa645/gates.py) on the new page and on the live page, figure coverage, validate failure codes,
publishable (`admin_pipeline._is_publishable`), brand audit, grounding found / still unsupported, Jev verdicts,
degraded detectors, "Adventure Asia" / "Design This Journey" when the source lacks them, the day path (canonical
"Day N —" itinerary or the legacy path; days vs the plan and vs the live page), real $ and calls by stage from
llm_call_log, fallback / throttle rows. `shadow_report.py` turns the output into the wave table + the memo section 6
stop thresholds.

  cd /app && PYTHONPATH=/tmp/aa645 python3 -m scripts.aa645.shadow --select pilot --n 3 \\
      --exclude-ids-file /tmp/aa645/golden_ids.json --max-cost-usd 0.5 --concurrency 1 [--dry-run] [--resume RUN_ID]

Output: s3://<bronze>/scripts/out/aa645/shadow/<run_id>/shadow.json (+ shadow.partial.json after every tour,
panel-pages.json, panel/packet.json + key.json for the blind panel: new page vs live page per tour).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

STAGE = "adhoc_aa645"
S3_PREFIX = "scripts/out/aa645/shadow"
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
WAVE_MAX = 50                       # paid-runs-subset: <= 50 tours without a separate approval
DEFAULT_WRITER_MODEL = "sonnet-5-5"
PRIOR_USD_PER_TOUR = 0.12           # memo: W x Sonnet chain $0.068-0.117 a tour (mid 0.08); the launch guard's prior
CALL_MARGIN_USD = 0.15              # one call's worst case kept free per running tour (a Sonnet 5.5 writer call)
PER_TOUR_TIMEOUT_S = 1500
SETTLE_S = 2.0
MIN_QUALITY = 7.0
LEAK_PHRASES = ("Adventure Asia", "Design This Journey")     # = brand_fit_v2.LEAK_PHRASES (pinned by a test)
# = brand_audit_node.ELEPHANT_RIDING (pinned by a test); kept here so selection is testable without the app
ELEPHANT_RIDING = re.compile(r'elephant[-\s]?back|elephant\s+ride|elephant\s+trek', re.IGNORECASE)

CANDIDATES_SQL = """
SELECT rt.tour_id::text AS tour_id, rt.country, rt.duration, rt.src_name, rt.src_summary, rt.src_description,
       rt.src_highlights::text AS src_highlights, rt.src_itineraries, pt.generated_content_id::text AS live_id,
       EXISTS (SELECT 1 FROM geo.tour_build b
                WHERE b.tour_id = rt.tour_id AND b.status IN ('ok', 'partial')) AS has_places
FROM gold_aa_internal.published_tours pt
JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
WHERE pt.deleted_at IS NULL AND pt.tenant_id = $2::uuid AND pt.master_status = 'active'
  AND rt.deleted_at IS NULL AND rt.source_status = 'active'
  AND coalesce(trim(rt.src_itineraries), '') <> ''
  AND NOT (rt.tour_id::text = ANY($1::text[]))
"""
TOUR_SQL = "SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid"
SEO_SQL = "SELECT top_keywords, people_also_ask FROM silver_aa_internal.seo_context WHERE tour_id = $1::uuid"
LIVE_SQL = """
SELECT gc.id::text AS id, gc.metadata, gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights,
       gc.aa_itineraries, gc.seo_title, gc.seo_meta
FROM gold_aa_internal.published_tours pt
JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
WHERE pt.tour_id = $1::uuid AND pt.deleted_at IS NULL AND pt.tenant_id = $2::uuid
"""
BUSY_SQL = "SELECT count(*) FROM shared.job WHERE status IN ('queued', 'running')"
_RUN_FILTER = f"stage = '{STAGE}' AND created_at >= $1 AND quality_signal->>'aa729_run' = $2"
SPEND_SQL = f"SELECT COALESCE(sum(cost_usd), 0)::float FROM shared.llm_call_log WHERE {_RUN_FILTER}"
CALLS_SQL = f"""
SELECT job_id::text AS job_id, role, provider, quality_signal->>'aa729_stage' AS real_stage,
       count(*)::int AS n, COALESCE(sum(cost_usd), 0)::float AS usd,
       count(*) FILTER (WHERE fallback_used)::int AS fallback_rows,
       count(*) FILTER (WHERE quality_signal::text ILIKE '%throttl%')::int AS throttle_rows,
       count(*) FILTER (WHERE quality_signal ? 'billed_unusable' OR quality_signal ? 'gateway_attempt')::int
           AS unusable_rows
FROM shared.llm_call_log WHERE {_RUN_FILTER}
GROUP BY 1, 2, 3, 4
"""
# every judged draft of the run (one verdict row per judge verdict; repeat / median rows carry no "gate")
JUDGE_SQL = f"""
SELECT job_id::text AS job_id, (quality_signal->>'brand_fit_score')::float AS brand_fit,
       quality_signal->>'gate' AS gate, COALESCE(quality_signal->>'rubric', 'v1') AS rubric, created_at
FROM shared.llm_call_log
WHERE {_RUN_FILTER} AND role = 'judge' AND quality_signal->>'aa729_stage' = 's1_judge' AND quality_signal ? 'gate'
ORDER BY created_at
"""
JEV_SQL = """
SELECT job_id::text AS job_id, question_key, zone, count(*)::int AS n
FROM shared.decision_log
WHERE created_at >= $1 AND job_id = ANY($2::uuid[])
GROUP BY 1, 2, 3
"""


# ------------------------------------------------------------------ selection (pure)

def _md5(tour_id: str, seed: str) -> str:
    return hashlib.md5(f"{tour_id}{seed}".encode()).hexdigest()


def is_elephant(c: dict) -> bool:
    text = " ".join(str(c.get(k) or "") for k in ("src_name", "src_summary", "src_description", "src_highlights",
                                                    "src_itineraries"))
    return bool(ELEPHANT_RIDING.search(text))


def eligible(cands: list[dict], excluded: set[str]) -> tuple[list[dict], dict]:
    """Candidates minus excluded ids and elephant-riding tours; the counts of what was dropped."""
    out, drop = [], {"excluded_ids": 0, "elephant": 0}
    for c in cands:
        if c["tour_id"] in excluded:
            drop["excluded_ids"] += 1
        elif is_elephant(c):
            drop["elephant"] += 1
        else:
            out.append(c)
    return out, drop


def select_pilot(cands: list[dict], n: int, country: str, seed: str) -> list[dict]:
    """`n` tours of `country` (case-insensitive): built places first, then the rest, md5 order inside each."""
    pool = [c for c in cands if (c.get("country") or "").strip().lower() == country.strip().lower()]
    pool.sort(key=lambda c: (not c.get("has_places"), _md5(c["tour_id"], seed)))
    return pool[:n]


def select_wave(cands: list[dict], n: int, seed: str) -> list[dict]:
    """`n` tours stratified by country: largest-remainder allocation over the eligible counts with a floor of one per
    country while n allows (the biggest countries first), md5 order inside a country; the result is interleaved
    country by country so a cost-cap stop leaves the countries balanced."""
    from scripts.aa729.common import allocate

    by: dict[str, list[dict]] = {}
    for c in cands:
        by.setdefault((c.get("country") or "(none)").strip() or "(none)", []).append(c)
    for v in by.values():
        v.sort(key=lambda c: _md5(c["tour_id"], seed))
    quota = allocate(n, {k: len(v) for k, v in by.items()}, {k: 1 for k in by})
    picked = {k: by[k][:quota.get(k, 0)] for k in by}
    out, i = [], 0
    while any(len(v) > i for v in picked.values()):
        for k in sorted(picked):
            if len(picked[k]) > i:
                out.append(picked[k][i])
        i += 1
    return out[:n]


def load_ids(paths: list[str]) -> set[str]:
    ids: set[str] = set()
    for p in paths or []:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "samples" in data:          # an AA-729 fixture: every sample's tour id
            data = [((s.get("refs") or {}).get("tour_id")) for s in data["samples"]]
        ids |= {str(x).strip() for x in data if x and str(x).strip()}
    return ids


# ------------------------------------------------------------------ per-tour checks (app imports, no DB)

def leaks(page_text: str, source_text: str) -> list[str]:
    out = []
    for phrase in LEAK_PHRASES:
        pat = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, phrase.split())) + r"(?!\w)", re.I)
        if pat.search(page_text or "") and not pat.search(source_text or ""):
            out.append(phrase)
    return out


def _page_text(page: Optional[dict]) -> str:
    from services.quality.s1_facts import field_text
    return "\n".join(field_text((page or {}).get(f)) for f in ("name", "subtitle", "summary", "description",
                                                               "highlights", "itineraries", "seo_title", "seo_meta"))


def page_checks(tour: dict, page: Optional[dict], facts: dict) -> Optional[dict]:
    """The A/B code checks of one page (gate codes = the serious-error proxy, report codes, figure coverage) and its
    day path. None when there is no page."""
    if not page:
        return None
    from scripts.aa645.gates import gate_label, is_report, page_gate_codes
    from scripts.aa645.s1_screen import figure_coverage
    from scripts.aa645.wave_report import page_days
    from services.quality.s1_checks import run_checks

    findings = run_checks(tour, page, facts)
    days = page_days(page)
    return {"gate_codes": sorted({gate_label(c) for c in page_gate_codes(findings)}),
            "gate_codes_raw": sorted(page_gate_codes(findings)),
            "report_codes": sorted({f["code"] for f in findings if is_report(f["code"])}),
            "figure_coverage": figure_coverage(facts, page), "days": days, "canonical_days": bool(days)}


def live_page(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    from scripts.aa645.wave_report import page_of
    return page_of(row)


def tour_verdict(result: dict, tour: dict, live: Optional[dict], live_meta: Optional[dict]) -> dict:
    """Everything measured on one shadow run (no DB): verdicts from the final graph state + code checks."""
    from api.routers.admin_pipeline import _is_publishable, _surviving_hard_codes
    from services.content_generation import s1_writer_v2 as w
    from services.quality.s1_facts import build_fact_table
    from scripts.aa729.score import degraded_block

    facts = build_fact_table(tour)
    plan = w.day_plan(tour, facts)
    page = result.get("generated") or {}
    new = page_checks(tour, page, facts) if page else None
    old = page_checks(tour, live, facts) if live else None
    src = "\n".join(str(v or "") for v in tour.values())
    plan_days = [d["day"] for d in plan["days"]]
    grounding = {"ran": bool(result.get("grounding_ran")), "found": len(result.get("grounding_found") or []),
                 "still_unsupported": len(result.get("grounding_violations") or []),
                 "repaired_fields": list(result.get("grounding_repaired_fields") or [])}
    return {
        "status": result.get("status"), "error": (result.get("error") or "")[:300] or None,
        "writer_version": result.get("writer_version"), "model_used": result.get("model_used"),
        "attempts": int(result.get("retry_count") or 0) + 1,
        "quality_score": result.get("quality_score"), "publishable": bool(_is_publishable(result)),
        "surviving_hard_codes": sorted(_surviving_hard_codes(result)),
        "failure_codes": list(result.get("failure_codes") or []),
        "judge": {"brand_fit": result.get("judge_brand_fit"), "judge_score": result.get("judge_score"),
                  "rubric": result.get("judge_rubric") or "v1", "caps": list(result.get("judge_caps") or []),
                  "unsupported": len(result.get("judge_unsupported") or []),
                  "pass": (result.get("judge_brand_fit") is not None
                           and float(result.get("judge_brand_fit")) >= MIN_QUALITY)},
        "brand_audit": {"status": result.get("brand_audit_status"),
                        "codes": list(result.get("brand_audit_codes") or [])},
        "grounding": grounding,
        "flag_fix": {"applied": bool(result.get("fix_pass_applied")),
                     "fields": list(result.get("fix_pass_fields") or []),
                     "revalidate_ran": bool(result.get("revalidate_ran")),
                     "revalidate_passed": bool(result.get("revalidate_passed"))},
        **degraded_block(result.get("degraded")),
        "quality_shadow_codes": sorted({f.get("code") for f in (result.get("quality_shadow") or [])
                                        if isinstance(f, dict) and f.get("code")}),
        "new_page": new, "live_page": old,
        "live_writer_version": (live_meta or {}).get("writer_version") or ("v1" if live else None),
        "leaks": leaks(_page_text(page), src) if page else [],
        "days": {"plan_mode": plan["mode"], "plan": plan_days, "new": (new or {}).get("days"),
                 "live": (old or {}).get("days"),
                 "legacy_path": bool(page) and not (new or {}).get("canonical_days"),
                 "matches_plan": bool(new) and new["days"] == plan_days,
                 "count_changed": bool(new and old and len(new["days"]) != len(old["days"])),
                 "count_dropped": bool(new and old and len(new["days"]) < len(old["days"]))},
    }


# ------------------------------------------------------------------ the writer model (in-process patch)

def install_writer_model(model: Optional[str]) -> None:
    """Send the v2 writer + meta calls (s1_writer_v2_live._call, looked up by name inside write()) to `model` as the
    request's model_tier; every other stage keeps its live route. None / "route" = no patch."""
    if not model or model == "route":
        return
    import inspect

    from services.content_generation import s1_writer_v2_live as live
    if not hasattr(live, "_call") or "_call(client, state" not in inspect.getsource(live.write):
        raise RuntimeError("s1_writer_v2_live.write no longer calls _call(client, state, ...) — update shadow.py")
    if getattr(live._call, "_aa645_model", None) == model:
        return
    original = live._call

    def _call(client, state, **kw):
        return original(client, {**state, "model_tier": model}, **kw)

    _call._aa645_model = model
    live._call = _call


# ------------------------------------------------------------------ DB / one tour

def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _texts(value, keys) -> list[str]:
    out = []
    for v in value if isinstance(value, list) else []:
        t = next((v.get(k) for k in keys if isinstance(v, dict) and v.get(k)), None) if isinstance(v, dict) else v
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def seo_from_cache(row: Optional[dict]) -> dict:
    """The seo dict `_execute_run_tour` would pass (flat + nested keywords, PAA), from the cached seo_context."""
    row = row or {}
    kws = _texts(_json(row.get("top_keywords")), ("keyword", "text"))
    paa = _texts(_json(row.get("people_also_ask")), ("question", "title", "text"))
    return {"top_keywords": kws, "keywords": {"top_keywords": kws}, "people_also_ask": paa}


def brand_rules_from_row(br: Optional[dict]) -> dict:
    """`_execute_run_tour`'s brand_rules dict from a `_resolve_brand_rule` row."""
    if not br:
        return {}

    def lst(v):
        v = _json(v)
        return list(v) if isinstance(v, list) else []
    return {"system_prompt": br.get("system_prompt") or "", "style_guide": br.get("style_guide") or "",
            "forbidden_words": lst(br.get("forbidden_words")), "rewrite_language": "en-US",
            "core_idea": br.get("core_idea") or "", "customer_segment": br.get("customer_segment") or "",
            "customer_mindset": br.get("customer_mindset") or "", "voice_examples": lst(br.get("voice_examples")),
            "good_examples": br.get("good_examples") or "", "brand_type": br.get("brand_type") or ""}


def tour_from_row(row: dict) -> dict:
    hl = _json(row.get("src_highlights"))
    return {"name": row["src_name"], "subtitle": row.get("src_subtitle"), "summary": row.get("src_summary"),
            "description": row.get("src_description"), "highlights": hl if isinstance(hl, list) else (hl or []),
            "itineraries": row.get("src_itineraries"), "country": row.get("country"), "duration": row.get("duration"),
            "price": row.get("price_raw"), "inclusions": row.get("inclusions"), "exclusions": row.get("exclusions")}


async def run_tour(pool, tour_id: str, brand_rules: dict) -> tuple[dict, dict, dict, Optional[dict]]:
    """One shadow run: (result of _rewrite_tour [+ the AA-736 retry], the source tour + agency fields, inputs, the
    live page row). The DB is read up front; no connection is held while the graph runs."""
    from api.routers.admin_pipeline import _REWRITE_CLASS_HARD_CODES, _surviving_hard_codes
    from api.routers.v1_pipeline import _rewrite_tour
    from services.content_generation.s1_writer_v2 import with_agency_fields

    async with pool.acquire() as conn:
        row = dict(await conn.fetchrow(TOUR_SQL, tour_id))
        seo = seo_from_cache(dict(await conn.fetchrow(SEO_SQL, tour_id) or {}))
        lrow = await conn.fetchrow(LIVE_SQL, tour_id, MASTER_TENANT_ID)
    tour = tour_from_row(row)
    agency = {"group_size": row.get("group_size"), "best_time_to_go": row.get("best_time_to_go")}
    kw = {"brand_rules": brand_rules, "seo": seo, "model_tier": None, "subtitle_focus": "standard",
          "seo_mode": "dataforseo", "tour_agency": agency}
    result = await _rewrite_tour(copy.deepcopy(tour), 0, 1, **kw)
    sonnet = False
    if (result.get("status") == "success" and _surviving_hard_codes(result) & _REWRITE_CLASS_HARD_CODES
            and "haiku" in (result.get("model_used") or "").lower()):
        upgraded = await _rewrite_tour(copy.deepcopy(tour), 0, 1, **{**kw, "model_tier": "sonnet"})
        if len(_surviving_hard_codes(upgraded)) < len(_surviving_hard_codes(result)):
            result = upgraded
        sonnet = True
    result["_sonnet_retried"] = sonnet
    return result, with_agency_fields(tour, agency), {"keywords": seo["top_keywords"][:5]}, \
        (dict(lrow) if lrow else None)


# ------------------------------------------------------------------ run

def _calls_by_job(rows) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["job_id"], {"calls": {}, "by_stage": {}, "usd_by_stage": {}, "cost_usd": 0.0,
                                         "fallback_rows": 0, "throttle_rows": 0, "unusable_rows": 0})
        st = r["real_stage"] or "?"
        d["calls"][r["role"]] = d["calls"].get(r["role"], 0) + r["n"]
        d["by_stage"][st] = d["by_stage"].get(st, 0) + r["n"]
        d["usd_by_stage"][st] = round(d["usd_by_stage"].get(st, 0.0) + (r["usd"] or 0.0), 6)
        d["cost_usd"] = round(d["cost_usd"] + (r["usd"] or 0.0), 6)
        for k in ("fallback_rows", "throttle_rows", "unusable_rows"):
            d[k] += int(r.get(k) or 0)
    return out


def _judged_by_job(rows) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["job_id"], []).append({"brand_fit": r["brand_fit"], "gate": r["gate"], "rubric": r["rubric"]})
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--select", choices=("pilot", "wave", "ids"), required=True)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--pilot-country", default="Laos")
    ap.add_argument("--tour-ids-file")
    ap.add_argument("--exclude-ids-file", action="append", default=[],
                    help="JSON list of tour ids, or an AA-729 fixture (refs.tour_id of every sample); repeatable")
    ap.add_argument("--seed", default="aa645-s249")
    ap.add_argument("--writer-model", default=DEFAULT_WRITER_MODEL,
                    help="catalog key for the v2 writer + meta calls, or 'route' (the live s1_generate route)")
    ap.add_argument("--max-cost-usd", type=float, required=True)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--resume", help="RUN_ID: continue that run's partial output under the same cap")
    ap.add_argument("--allow-busy-queue", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="select tours, build one writer prompt, install the "
                                                           "patches; no model call ($0)")
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args(argv)
    if args.select == "wave" and not 1 <= args.n <= WAVE_MAX:
        ap.error(f"--n must be 1..{WAVE_MAX} for a wave (paid-runs-subset; more needs a separate approval)")
    if args.select == "ids" and not args.tour_ids_file:
        ap.error("--select ids needs --tour-ids-file")
    return args


def _prepare_env() -> None:
    """Writer v2 on and the AA-729 isolation stage set to adhoc_aa645 — this process only, before any import that
    reads them."""
    if os.environ.get("AA729_STAGE", STAGE) != STAGE:
        raise SystemExit(f"AA729_STAGE is {os.environ['AA729_STAGE']!r}; this harness logs under {STAGE}")
    os.environ["AA729_STAGE"] = STAGE
    os.environ["S1_WRITER_V2"] = "1"
    if "scripts.aa729.common" in sys.modules and sys.modules["scripts.aa729.common"].STAGE != STAGE:
        raise SystemExit("scripts.aa729.common was imported with another stage")


async def _pool():
    import asyncpg
    return await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=4)


async def run(args) -> dict:
    _prepare_env()
    from api.routers.admin_pipeline import _resolve_brand_rule
    from services.content_generation import s1_writer_v2 as w
    from services.content_generation import s1_writer_v2_live as live
    from services.quality.s1_facts import build_fact_table
    from scripts.aa645.shadow_report import summarize
    from scripts.aa729 import evalpatch
    from scripts.aa729.common import CostCapReached, CostGuard, jev_summary, load_json, sample_job_id, save_output
    from shared.llm_client import call_log

    checkpoint = load_json(f"s3:{S3_PREFIX}/{args.resume}/shadow.partial.json") if args.resume else None
    run_id = checkpoint["run_id"] if checkpoint else str(uuid.uuid4())
    started = datetime.fromisoformat(checkpoint["started_at"]) if checkpoint else datetime.now(timezone.utc)
    since = started - timedelta(minutes=5)
    results: dict[str, dict] = {r["tour_id"]: r for r in (checkpoint or {}).get("tours", []) if not r.get("error")}
    out_key = f"{S3_PREFIX}/{run_id}/shadow"
    local_dir = "/tmp/aa645-shadow"

    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            busy = await conn.fetchval(BUSY_SQL)
            if busy and not args.allow_busy_queue and not args.dry_run:
                raise SystemExit(f"{busy} job(s) queued/running in shared.job — paid runs wait for an idle queue; "
                                 "pass --allow-busy-queue to override")
            excluded = load_ids(args.exclude_ids_file)
            cands = [dict(r) for r in await conn.fetch(CANDIDATES_SQL, sorted(excluded), MASTER_TENANT_ID)]
            pool_ok, dropped = eligible(cands, excluded)
            if checkpoint:
                chosen_ids = checkpoint["planned"]
            elif args.select == "pilot":
                chosen_ids = [c["tour_id"] for c in select_pilot(pool_ok, args.n, args.pilot_country, args.seed)]
            elif args.select == "wave":
                chosen_ids = [c["tour_id"] for c in select_wave(pool_ok, args.n, args.seed)]
            else:
                ok_ids = {c["tour_id"] for c in pool_ok}
                chosen_ids = [t for t in sorted(load_ids([args.tour_ids_file])) if t in ok_ids]
            by_id = {c["tour_id"]: c for c in cands}
            br = await _resolve_brand_rule(conn, MASTER_TENANT_ID, None, None)
            brand_rules = brand_rules_from_row(dict(br) if br else None)
        if not chosen_ids:
            raise SystemExit("no tour selected (check --pilot-country / exclusions)")

        evalpatch.install(run_id, stage=STAGE)
        evalpatch.CALL_MARGIN_USD = CALL_MARGIN_USD          # read at check time: a Sonnet 5.5 writer call's margin
        evalpatch.BUDGET.configure(args.max_cost_usd, slots=max(1, args.concurrency))
        install_writer_model(args.writer_model)

        head = {"schema": "aa645-shadow/1", "run_id": run_id, "stage": STAGE, "select": args.select, "n": args.n,
                "seed": args.seed, "writer_model": args.writer_model, "cap_usd": args.max_cost_usd,
                "concurrency": args.concurrency, "started_at": started.isoformat(), "planned": chosen_ids,
                "candidates": len(cands), "eligible": len(pool_ok), "dropped": dropped,
                "excluded_ids": len(excluded), "s1_writer_v2": live.enabled(),
                "cost_source": f"shared.llm_call_log stage={STAGE} quality_signal.aa729_run={run_id} job_id=<tour>"}
        print(f"START run={run_id} select={args.select} tours={len(chosen_ids)} eligible={len(pool_ok)} "
              f"dropped={dropped} writer={args.writer_model} cap=${args.max_cost_usd} v2={live.enabled()}", flush=True)
        for t in chosen_ids:
            c = by_id.get(t, {})
            print(f"  {t} {c.get('country')} places={c.get('has_places')} {str(c.get('src_name'))[:60]}", flush=True)

        if args.dry_run:
            async with pool.acquire() as conn:
                row = dict(await conn.fetchrow(TOUR_SQL, chosen_ids[0]))
            tour = w.with_agency_fields(tour_from_row(row), row)
            facts = build_fact_table(tour)
            plan = w.day_plan(tour, facts)
            prompt = w.w_user_prompt(tour, facts, plan)
            dry = {**head, "dry_run": True, "sample_tour": chosen_ids[0], "plan_mode": plan["mode"],
                   "plan_days": len(plan["days"]), "writer_prompt_chars": len(prompt),
                   "estimate_usd": round(len(chosen_ids) * PRIOR_USD_PER_TOUR, 2),
                   "tours": [{"tour_id": t, "country": by_id.get(t, {}).get("country"),
                              "has_places": by_id.get(t, {}).get("has_places")} for t in chosen_ids]}
            save_output(dry, f"{out_key}-dry.json", local_dir=local_dir, upload=not args.no_upload)
            print(f"DRY plan={plan['mode']} days={len(plan['days'])} prompt_chars={len(prompt)} "
                  f"estimate=${dry['estimate_usd']}", flush=True)
            return dry

        guard = CostGuard(cap_usd=args.max_cost_usd)
        stopped: dict = {"reason": None}
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, args.concurrency))
        todo = [t for t in chosen_ids if t not in results]

        async def logged_spend() -> float:
            async with pool.acquire() as conn:
                usd = float(await conn.fetchval(SPEND_SQL, since, run_id) or 0.0)
            evalpatch.BUDGET.note_logged(usd)
            return usd

        def snapshot(final: bool) -> dict:
            return {**head, "updated_at": datetime.now(timezone.utc).isoformat(), "final": final,
                    "stopped": stopped["reason"], "tours": list(results.values())}

        async def one(tour_id: str) -> None:
            async with sem:
                if not stopped["reason"] and evalpatch.BUDGET.tripped:
                    stopped["reason"] = f"cost_cap: {evalpatch.BUDGET.tripped}"
                if stopped["reason"]:
                    return
                try:
                    guard.check(sample_id=tour_id, prior=PRIOR_USD_PER_TOUR, logged_usd=await logged_spend(),
                                local_usd=evalpatch.SPEND.usd)
                except CostCapReached as e:
                    stopped["reason"] = f"cost_cap: {e}"
                    print(f"STOP {stopped['reason']}", flush=True)
                    return
                c = by_id.get(tour_id, {})
                row = {"tour_id": tour_id, "country": c.get("country"), "has_places": c.get("has_places"),
                       "name": c.get("src_name"), "job_id": sample_job_id(run_id, tour_id), "live_id": c.get("live_id")}
                t0 = time.monotonic()
                timed_out = False
                try:
                    with call_log.bind_job(row["job_id"]):
                        result, tour, inputs, lrow = await asyncio.wait_for(run_tour(pool, tour_id, brand_rules),
                                                                            timeout=PER_TOUR_TIMEOUT_S)
                    row.update(tour_verdict(result, tour, live_page(lrow), _json((lrow or {}).get("metadata"))))
                    row["sonnet_retried"] = result.get("_sonnet_retried", False)
                    row["inputs"] = inputs
                    row["_output"] = result.get("generated") or {}
                    row["_tour"] = tour
                    row["_live"] = live_page(lrow)
                    if row.get("writer_version") != "v2" and row.get("status") == "success":
                        row["error"] = "not written by writer v2 (flag / applies() check)"
                        stopped["reason"] = stopped["reason"] or "not_v2"
                except asyncio.TimeoutError:
                    timed_out = True
                    row["error"] = f"TimeoutError: no result after {PER_TOUR_TIMEOUT_S}s"
                except Exception as e:  # noqa: BLE001 — one tour's failure is a result, not the run's
                    row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                if row["job_id"] in evalpatch.BUDGET.tripped_jobs:
                    row["error"] = row.get("error") or f"CostCapReached: {evalpatch.BUDGET.tripped}"
                    stopped["reason"] = stopped["reason"] or f"cost_cap: {evalpatch.BUDGET.tripped}"
                await asyncio.sleep(SETTLE_S)
                async with pool.acquire() as conn:
                    calls = _calls_by_job(await conn.fetch(CALLS_SQL, since, run_id)).get(row["job_id"], {})
                row.update({"calls": calls.get("calls", {}), "calls_by_stage": calls.get("by_stage", {}),
                            "usd_by_stage": calls.get("usd_by_stage", {}), "cost_usd": calls.get("cost_usd", 0.0),
                            "fallback_rows": calls.get("fallback_rows", 0),
                            "throttle_rows": calls.get("throttle_rows", 0),
                            "seconds": round(time.monotonic() - t0, 1)})
                guard.done(tour_id, row["cost_usd"], release=not timed_out)
                async with lock:
                    results[tour_id] = row
                    save_output(_public(snapshot(False)), f"{out_key}.partial.json", local_dir=local_dir,
                                upload=not args.no_upload)
                print(f"{tour_id} {row.get('country')} status={row.get('status')} attempts={row.get('attempts')} "
                      f"publishable={row.get('publishable')} judge={row.get('judge', {}).get('brand_fit')} "
                      f"gates={(row.get('new_page') or {}).get('gate_codes')} cost=${row['cost_usd']:.4f} "
                      f"{row.get('error') or ''}", flush=True)

        await asyncio.gather(*(one(t) for t in todo))
        await asyncio.sleep(SETTLE_S * 2)
        async with pool.acquire() as conn:
            calls = _calls_by_job(await conn.fetch(CALLS_SQL, since, run_id))
            judged = _judged_by_job(await conn.fetch(JUDGE_SQL, since, run_id))
            job_ids = [r["job_id"] for r in results.values()]
            jev_rows = [dict(r) for r in await conn.fetch(JEV_SQL, since, job_ids)] if job_ids else []
            total = float(await conn.fetchval(SPEND_SQL, since, run_id) or 0.0)
    finally:
        await pool.close()

    jev_by: dict[str, list[dict]] = {}
    for r in jev_rows:
        jev_by.setdefault(r["job_id"], []).append(r)
    for row in results.values():                 # final cost (late log writes included), judged drafts, Jev
        c = calls.get(row["job_id"])
        if c:
            row.update({"calls": c["calls"], "calls_by_stage": c["by_stage"], "usd_by_stage": c["usd_by_stage"],
                        "cost_usd": c["cost_usd"], "fallback_rows": c["fallback_rows"],
                        "throttle_rows": c["throttle_rows"]})
        row["judged_drafts"] = judged.get(row["job_id"], [])
        row["jev"] = jev_summary(jev_by.get(row["job_id"], []))
    final = snapshot(True)
    final["totals"] = {"cost_usd_llm_call_log": round(total, 6), "local_llm_usd": round(evalpatch.SPEND.usd, 6),
                       "local_llm_calls": evalpatch.SPEND.calls}
    final["summary"] = summarize(final["tours"])
    _write_panel(final, out_key, local_dir, upload=not args.no_upload)
    save_output(_public(final), f"{out_key}.json", local_dir=local_dir, upload=not args.no_upload)
    return final


def _public(doc: dict) -> dict:
    """The output without page text / source (they go to panel-pages.json only)."""
    return {**doc, "tours": [{k: v for k, v in t.items() if not k.startswith("_")} for t in doc.get("tours", [])]}


def _write_panel(doc: dict, out_key: str, local_dir: str, upload: bool) -> None:
    """New page vs live page per tour, in the s1_screen units shape (arm shadow_w / stored), + the blind packet."""
    from scripts.aa645.s1_screen import panel_packet
    from scripts.aa729.common import save_output

    units, samples = [], {}
    for t in doc.get("tours", []):
        if t.get("error") or not t.get("_output"):
            continue
        sid = t["tour_id"]
        samples[sid] = {"tour": t["_tour"]}
        units.append({"key": f"{sid}__shadow_w__{doc['writer_model']}", "sample_id": sid, "arm": "shadow_w",
                      "model": doc["writer_model"], "tour": t["_tour"], "output": t["_output"]})
        if t.get("_live"):
            units.append({"key": f"{sid}__stored__live", "sample_id": sid, "arm": "stored", "model": "live",
                          "tour": t["_tour"], "output": t["_live"]})
    if not units:
        return
    packet, key = panel_packet(units, samples, doc["run_id"], include_stored=True)
    save_output(units, f"{out_key}-panel-pages.json", local_dir=local_dir, upload=upload)
    save_output(packet, f"{out_key}-panel/packet.json", local_dir=local_dir, upload=upload)
    save_output(key, f"{out_key}-panel/key.json", local_dir=local_dir, upload=upload)


def main(argv=None) -> int:
    args = parse_args(argv)
    out = asyncio.run(run(args))
    s = out.get("summary") or {}
    print(f"DONE run={out['run_id']} tours={len(out.get('tours', []))} stopped={out.get('stopped')} "
          f"cost=${(out.get('totals') or {}).get('cost_usd_llm_call_log')} "
          f"stop={[k for k, v in (s.get('stop_conditions') or {}).items() if v]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
