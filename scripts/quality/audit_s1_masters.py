"""AA-1049 — a READ-ONLY, $0 audit of EVERY active S1 Master of the AA master tenant (no model call, no write).

Runs in the CIS api container through ``tools/ecs/ecsx.sh`` (cwd /app, the container DATABASE_URL = cis_app,
ssl=require), inside one read-only transaction:

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/quality/audit_s1_masters.py > <ecsx output file>
    python3 apps/AA-CIS-App/scripts/quality/audit_s1_masters_local.py <ecsx output file>

The set: ``gold_aa_internal.published_tours`` of the master tenant, ``master_status = 'active'``, ``deleted_at IS
NULL``, with its PUBLISHED version (``published_tours.generated_content_id``), that version's latest
``quality_scores`` row and its ``raw_tours`` source (``final_contract_store.S1_ROW_COLUMNS`` / ``S1_ROW_FROM``: the
read every contract exit makes). Per Master, one JSON line:

- ``writer``: ``metadata.writer_version`` (``v2``), else ``model_editorial``; ``writer_group`` v2 | older;
- ``contract``: the S1 final contract recomputed today (``final_contract.check_s1``) WITH the master tenant's brand
  forbidden words (``shared.tenant_brand_rules``, the active row), twice: ``verdict_today`` on the hard codes of
  TODAY's validate run over the stored text (the input the live graph exit gives check_s1), and
  ``verdict_stored_codes`` on the stored ``quality_scores.failure_codes`` (the record_s1_exit input). ``verdict``
  (counts, not-publish list) is ``verdict_today``; every finding of it (code, severity, class, field, count, the
  code-built detail), the stored-codes blocking codes and the stored verdict sit next to it;
- ``brand_words``: the forbidden words found per field (brand list vs the AA core list);
- ``rules``: the S1 writing rules run again on the stored text, all deterministic: validate (``graph.validate_node``;
  codes new / cleared vs the stored ``failure_codes``), the brand pre-audit (``pre_audit_checks``), the S1 code checks
  (``s1_checks.run_checks``) and the numeric grounding check (``check_grounding(use_jev=False)``); codes and fields,
  never a sentence;
- ``compose``: which fixed fields the S1 train (``s1_train.compose_page``, ``S1_CONTROL=contract``) would change if it
  recomposed the stored page today: day headings, the verbatim fact lines (meals / overnight ...), day prose, day count,
  seo_title (word-boundary fit), seo_meta (code template), slug (not applicable: an S1 Master has no slug column).
  Field names and counts only, never the text. ``applies`` = the train composes this writer's pages (v2 only);
- ``not_run``: the checks that need a model (Jev grounding, the LLM brand audit, the judge; Jev for a meal / clock
  claim of the pre-audit when it asked). No model is ever called: ``grounding.judge_units`` answers no verdict and
  ``LLMClient`` raises; an attempt is listed under ``errors``, never passed silently.

Then a summary (counts per verdict today and on the stored codes, the Masters where the two differ, finding code,
rule code and compose field, split by writer group; the tour ids whose verdict today is not ``publish``) and a
population block (the master tenant's raw_tours by source / Master / pipeline
state, the live sources without an active Master): counts and ids only.

Output: one ``AUDIT_SUMMARY`` line (counts), then two gzip+base64 blocks (``=== BEGIN <name>`` ... ``=== END <name>``):
``s1_master_audit.jsonl`` (one line per Master) and ``s1_master_audit_summary.json``; the local helper decodes them
into the gitignored .tmp-session and writes the public-safe README.

Env: LIMIT (default 0 = every Master; a trial run takes the first N by tour_id).
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import gzip
import json
import os
import re
import sys
from collections import Counter
from contextlib import ExitStack
from typing import Any, Optional
from unittest.mock import patch

sys.path.insert(0, os.getcwd())
if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

SCHEMA = "aa1049-s1-master-audit/2"
MASTER_TENANT = "00000000-0000-0000-0000-000000000001"   # aa_internal (admin_pipeline TENANT_SLUG_MAP)
V2 = "v2"
GROUPS = ("v2", "older")
LINES_BLOCK = "s1_master_audit.jsonl"
SUMMARY_BLOCK = "s1_master_audit_summary.json"
# the checks no deterministic code can make; each Master lists them (and the audit never calls a model for them)
ALWAYS_NOT_RUN = ("jev_grounding", "llm_brand_audit", "s1_judge")
NOT_RUN_WHY = {
    "jev_grounding": "Jev sentence grounding of the stored text (the contract reads the stored grounding instead)",
    "llm_brand_audit": "the LLM brand audit (the deterministic pre-audit runs; the stored audit status is read)",
    "s1_judge": "the S1 judge score (the stored score is read)",
    "jev_meal_claims": "Jev on a meal / clock claim the source day does not back (the code is listed as needs_model)",
}
COMPOSE_FIELDS = ("day_headings", "fact_lines", "day_prose", "day_count", "seo_title", "seo_meta")
PAGE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title", "seo_meta")
_HEAD_RE = re.compile(r"^Day\s+(\d{1,3}(?:\s*-\s*\d{1,3})?)\s*[—–-]+\s*(.*)$")

BRAND_SQL = """
    SELECT forbidden_words FROM shared.tenant_brand_rules
     WHERE tenant_id = $1::uuid AND is_active = true
     ORDER BY (brand_name = 'default') DESC, version DESC LIMIT 1
"""

# one row per tour of the master tenant (counts only): its source state, its Master state, its latest version
POPULATION_SQL = """
    SELECT (rt.deleted_at IS NOT NULL) AS source_deleted, rt.source_status::text AS source_status,
           rt.lifecycle_stage::text AS lifecycle_stage, rt.pipeline_status::text AS pipeline_status,
           COALESCE(pt.master_status::text, 'none') AS master_status, pt.master_status_reason AS master_status_reason,
           (pt.deleted_at IS NOT NULL) AS master_deleted,
           COALESCE(lv.status, 'none') AS latest_version_status, COALESCE(lv.writer, 'none') AS latest_version_writer,
           count(*)::int AS n
      FROM silver_aa_internal.raw_tours rt
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
      LEFT JOIN LATERAL (
          SELECT gc.status::text AS status,
                 COALESCE(gc.metadata->>'writer_version', gc.model_editorial, 'unknown') AS writer
            FROM silver_aa_internal.generated_content gc
           WHERE gc.tour_id = rt.tour_id AND gc.tenant_id = rt.tenant_id
           ORDER BY gc.version_num DESC
           LIMIT 1
      ) lv ON true
     WHERE rt.tenant_id = $1::uuid
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
"""

# the live sources (not deleted, source active) that have no active Master: ids and states only
UNMASTERED_SQL = """
    SELECT rt.tour_id::text AS tour_id, rt.pipeline_status::text AS pipeline_status,
           rt.lifecycle_stage::text AS lifecycle_stage, COALESCE(pt.master_status::text, 'none') AS master_status,
           pt.master_status_reason AS master_status_reason,
           (SELECT gc.status::text FROM silver_aa_internal.generated_content gc
             WHERE gc.tour_id = rt.tour_id AND gc.tenant_id = rt.tenant_id
             ORDER BY gc.version_num DESC LIMIT 1) AS latest_version_status
      FROM silver_aa_internal.raw_tours rt
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
     WHERE rt.tenant_id = $1::uuid AND rt.deleted_at IS NULL AND rt.source_status::text = 'active'
       AND (pt.tour_id IS NULL OR pt.master_status::text <> 'active' OR pt.deleted_at IS NOT NULL)
     ORDER BY rt.tour_id
"""

ACTIVE_COUNT_SQL = """
    SELECT count(*) FILTER (WHERE pt.tenant_id = $1::uuid)::int AS master_tenant,
           count(*) FILTER (WHERE pt.tenant_id <> $1::uuid)::int AS other_tenants
      FROM gold_aa_internal.published_tours pt
     WHERE pt.master_status::text = 'active' AND pt.deleted_at IS NULL
"""


def masters_sql() -> str:
    """The active Masters of the master tenant with their published version (the contract's own row read)."""
    from services.quality.final_contract_store import S1_ROW_COLUMNS, S1_ROW_FROM
    return (f"SELECT pt.id::text AS published_tour_id, gc.model_editorial, gc.created_at,"
            f" rt.exclusions, rt.group_size, rt.best_time_to_go, (rt.tour_id IS NULL) AS source_missing,"
            f" (rt.deleted_at IS NOT NULL) AS source_deleted, rt.source_status::text AS source_status,"
            f" rt.lifecycle_stage::text AS lifecycle_stage, {S1_ROW_COLUMNS} {S1_ROW_FROM}"
            " JOIN gold_aa_internal.published_tours pt ON pt.generated_content_id = gc.id"
            " WHERE pt.tenant_id = $1::uuid AND pt.deleted_at IS NULL AND pt.master_status = 'active'"
            " ORDER BY gc.tour_id")


# ── small helpers ─────────────────────────────────────────────────────────────────────────────────


def _j(v: Any, default):
    if isinstance(v, (bytes, str)):
        try:
            v = json.loads(v)
        except ValueError:
            return default
    if v is None:
        return default
    return v if isinstance(v, type(default)) else default


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def writer_of(row: Any) -> tuple[str, str]:
    """(writer label, group): ``metadata.writer_version`` when stamped, else ``model_editorial``."""
    wv = _j(_get(row, "metadata"), {}).get("writer_version")
    if wv:
        return str(wv), (V2 if wv == V2 else "older")
    return str(_get(row, "model_editorial") or "unknown"), "older"


def page_of(row: Any) -> dict:
    """The stored page fields (highlights as a list)."""
    return {"name": _get(row, "aa_name"), "subtitle": _get(row, "aa_subtitle"), "summary": _get(row, "aa_summary"),
            "description": _get(row, "aa_description") or "", "highlights": _j(_get(row, "aa_highlights"), []),
            "itineraries": _get(row, "aa_itineraries") or "", "seo_title": _get(row, "seo_title") or "",
            "seo_meta": _get(row, "seo_meta") or ""}


def tour_of(row: Any) -> dict:
    """The source tour dict as the A1 job builds it from raw_tours (export_aa1049_s1.master_of)."""
    return {"name": _get(row, "src_name"), "subtitle": _get(row, "src_subtitle"), "summary": _get(row, "src_summary"),
            "description": _get(row, "src_description"), "highlights": _j(_get(row, "src_highlights"), []),
            "itineraries": _get(row, "src_itineraries"), "country": _get(row, "country"),
            "duration": _get(row, "duration"), "price": _get(row, "price_raw"),
            "inclusions": _get(row, "inclusions"), "exclusions": _get(row, "exclusions")}


def agency_of(row: Any) -> dict:
    return {"group_size": _get(row, "group_size"), "best_time_to_go": _get(row, "best_time_to_go")}


def writer_items(itineraries: Any) -> list[dict]:
    """The stored canonical itinerary as the writer's array: one {day, title, body} per ``Day N — Title`` block
    (the replay cassette's split, scripts/quality/replay_aa1049_s1.py)."""
    out: list[dict] = []
    for line in str(itineraries or "").splitlines():
        m = _HEAD_RE.match(line.strip())
        if m:
            out.append({"day": m.group(1).replace(" ", ""), "title": m.group(2).strip(), "body": ""})
        elif out:
            out[-1]["body"] = (out[-1]["body"] + "\n" + line).strip()
    return out


def day_blocks(itineraries: Any) -> list[dict]:
    """{head, facts, prose} per ``Day N`` block: the heading line, the fact-only lines, the other lines."""
    from services.content_generation.s1_train import _FACT_ONLY_LINE_RE
    out: list[dict] = []
    for line in str(itineraries or "").splitlines():
        s = line.strip()
        if _HEAD_RE.match(s):
            out.append({"head": s, "facts": [], "prose": []})
        elif s and out:
            out[-1]["facts" if _FACT_ONLY_LINE_RE.match(s) else "prose"].append(s)
    return out


# ── the checks of one Master ──────────────────────────────────────────────────────────────────────


class _NoModel:
    """Every model path of the checks, answered without a model: Jev gives no verdict (recorded), LLMClient raises."""

    def __init__(self):
        self.jev_asked = 0
        self.llm_attempts = 0

    def judge_units(self, units, sources, *, tenant_id=None):
        if sources:
            self.jev_asked += 1
        return {}

    def generate(self, *_a, **_k):
        self.llm_attempts += 1
        raise RuntimeError("audit: no model call")

    def init(self, *_a, **_k):
        self.llm_attempts += 1
        raise RuntimeError("audit: no model client")


def _no_model_patches(st: ExitStack, nm: _NoModel) -> None:
    from services.content_generation import grounding
    from shared.llm_client.client import LLMClient
    st.enter_context(patch.object(grounding, "judge_units", nm.judge_units))
    st.enter_context(patch.object(LLMClient, "generate", nm.generate))
    st.enter_context(patch.object(LLMClient, "__init__", nm.init))


def today_codes_of(stored: list[str], validate_today: list[str], writer_version: Optional[str]) -> list[str]:
    """The failure codes the live S1 exit would hand the contract today (graph.final_contract_node reads
    ``state["failure_codes"]`` after validate ran on the text): the hard-block codes come from TODAY's validate run
    (they are validate's own codes: length / meta / missing field / forbidden word / day codes), the other stored
    codes (grounding, rewrite-class) are kept as stored (check_s1 reads only the hard-block ones)."""
    from services.content_generation.graph import hard_block_codes
    hard = hard_block_codes({"writer_version": writer_version})
    return sorted({c for c in stored if c not in hard} | set(validate_today))


def _verdict_of(row: Any, brand_words: list[str], failure_codes: Optional[list[str]]) -> dict:
    from services.quality import final_contract as fc
    inp = fc.s1_input_from_row(row)
    inp.brand_forbidden_words = list(brand_words)
    if failure_codes is not None:
        inp.failure_codes = list(failure_codes)
    return fc.check_s1(inp, exit="report", actual="published").record()


def _blocks(rec: dict) -> list[str]:
    return sorted({f["code"] for f in rec.get("findings") or [] if f.get("severity") == "block"})


def contract_of(row: Any, brand_words: list[str], validate_today: Optional[list[str]] = None) -> dict:
    """The S1 final contract recomputed today, twice:

    - ``verdict_today``: with the failure codes of TODAY's validate run on the stored text (``today_codes_of``),
      the input the live graph exit gives check_s1 (graph.final_contract_node, s1_input_from_state);
    - ``verdict_stored_codes``: with the stored ``quality_scores.failure_codes`` (the record_s1_exit input).

    ``verdict`` (what the counts and the not-publish list read) is ``verdict_today``; when validate could not run
    (``validate_today`` None) it falls back to the stored-codes verdict and ``verdict_basis`` says so."""
    keep = ("code", "severity", "class_id", "field", "count", "detail")
    stored_rec = _verdict_of(row, brand_words, None)
    if validate_today is not None:
        md = _j(_get(row, "metadata"), {})
        stored_codes = [str(c) for c in _j(_get(row, "failure_codes"), [])]
        today_rec = _verdict_of(row, brand_words, today_codes_of(stored_codes, list(validate_today),
                                                                 md.get("writer_version")))
        basis = "today_validate"
    else:
        today_rec, basis = stored_rec, "stored_codes"
    stored = (_j(_get(row, "metadata"), {}).get("final_contract") or {}).get("verdict")
    verdict = today_rec["verdict"]
    return {"verdict": verdict, "verdict_basis": basis,
            "verdict_today": today_rec["verdict"] if basis == "today_validate" else None,
            "verdict_stored_codes": stored_rec["verdict"],
            "verdicts_differ": basis == "today_validate" and today_rec["verdict"] != stored_rec["verdict"],
            "block_codes_stored_codes": _blocks(stored_rec),
            "stored_verdict": stored,
            "verdict_changed": stored is not None and stored != verdict,
            "findings": [{k: f[k] for k in keep if k in f} for f in today_rec.get("findings") or []]}


def brand_words_of(page: dict, brand_words: list[str]) -> dict:
    from services.content_generation.forbidden_words import VALIDATE_FORBIDDEN, forbidden_in
    brand = [w.lower().strip() for w in brand_words if w and str(w).strip()]
    core = list(VALIDATE_FORBIDDEN)
    hits = []
    for f in PAGE_FIELDS:
        for src, words in (("brand", brand), ("core", core)):
            found = sorted(forbidden_in(page.get(f), words))
            if found:
                hits.append({"field": f, "source": src, "words": found})
    return {"brand_words": len(brand), "hits": hits}


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:120]}"


def rules_of(row: Any, page: dict, tour: dict, tour_w: dict, facts: dict, brand_words: list[str],
             writer_version: Optional[str], nm: "_NoModel") -> tuple[dict, list[str], dict]:
    """The deterministic S1 writing rules over the stored text; returns (rules, extra not_run names, errors). Each
    rule set runs on its own: one that fails is an ``errors["rules.<name>"]`` entry and the others still report."""
    from services.content_generation import graph
    from services.content_generation.brand_audit_node import pre_audit_checks
    from services.content_generation.grounding import check_grounding
    from services.quality import s1_checks

    out: dict = {}
    extra: list[str] = []
    errors: dict = {}
    try:
        stored = sorted({str(c) for c in _j(_get(row, "failure_codes"), [])})
        # a stored NULL text field reads as "" (validate's own "missing field" test treats both alike; a None
        # subtitle crashes its .lower(), the main bug the S1 train notes record)
        gen = {k: ("" if v is None and k != "highlights" else v) for k, v in page.items()}
        state = {"generated": gen, "tour": dict(tour), "writer_version": writer_version,
                 "brand_forbidden_words": list(brand_words), "seo": {}, "retry_count": 0}
        today = graph.validate_node(state)["failure_codes"]
        out["validate"] = {"codes": sorted(set(today)), "stored_codes": stored,
                           "new": sorted(set(today) - set(stored)), "cleared": sorted(set(stored) - set(today))}
    except Exception as e:  # noqa: BLE001 — reported, the other rule sets still run
        errors["rules.validate"] = _err(e)
    try:
        jev_before = nm.jev_asked
        codes = sorted(pre_audit_checks(dict(page), dict(tour)))
        asked = nm.jev_asked > jev_before
        needs = ["ITINERARY_MEAL_TIME_INVENTED"] if asked and "ITINERARY_MEAL_TIME_INVENTED" in codes else []
        if asked:
            extra.append("jev_meal_claims")
        out["pre_audit"] = {"codes": [c for c in codes if c not in needs], "needs_model": needs,
                            "stored_status": _get(row, "brand_audit_status"),
                            "stored_codes": sorted({str(c) for c in _j(_get(row, "brand_audit_codes"), [])})}
    except Exception as e:  # noqa: BLE001
        errors["rules.pre_audit"] = _err(e)
    try:
        found = Counter((f.get("code"), f.get("field")) for f in s1_checks.run_checks(tour_w, dict(page), facts))
        out["s1_checks"] = [{"code": c, "field": f, "count": n} for (c, f), n in sorted(found.items(), key=str)]
    except Exception as e:  # noqa: BLE001
        errors["rules.s1_checks"] = _err(e)
    try:
        g = check_grounding(dict(page), dict(tour), use_jev=False)
        by_field = Counter(v.get("field") for v in g.get("violations") or [])
        out["grounding_numeric"] = {"violations": sum(by_field.values()), "fields": dict(sorted(by_field.items()))}
    except Exception as e:  # noqa: BLE001
        errors["rules.grounding_numeric"] = _err(e)
    return out, extra, errors


def compose_of(page: dict, tour_w: dict, facts: dict, brand_words: list[str]) -> dict:
    """What the S1 train's compose_page would change on the stored page: field names and counts, never text."""
    from services.content_generation import s1_train
    from services.content_generation import s1_writer_v2 as v2
    from services.content_generation.forbidden_words import all_forbidden

    plan = v2.day_plan(tour_w, facts)
    forbidden = all_forbidden(brand_words)
    problems = v2.meta_problems({"seo_title": page.get("seo_title"), "seo_meta": page.get("seo_meta")},
                                tour_w, facts, forbidden)
    composed, rep = s1_train.compose_page(dict(page), {"itineraries": writer_items(page.get("itineraries"))},
                                          tour_w, facts, plan, forbidden, problems)
    a, b = day_blocks(page.get("itineraries")), day_blocks(composed.get("itineraries"))
    n = max(len(a), len(b))
    pad = {"head": None, "facts": None, "prose": None}
    pairs = [(a[i] if i < len(a) else pad, b[i] if i < len(b) else pad) for i in range(n)]
    fields = {
        "day_headings": sum(1 for x, y in pairs if x["head"] != y["head"]),
        "fact_lines": sum(1 for x, y in pairs if x["facts"] != y["facts"]),
        "day_prose": sum(1 for x, y in pairs if x["prose"] != y["prose"]),
        "day_count": int(len(a) != len(b)),
        "seo_title": int(str(composed.get("seo_title") or "") != str(page.get("seo_title") or "")),
        "seo_meta": int(str(composed.get("seo_meta") or "") != str(page.get("seo_meta") or "")),
    }
    keep = ("composed", "missing", "extra", "duplicate", "fact_lines", "writer_fact_lines_removed",
            "source_fact_lines", "seo_title", "meta")
    return {"fields": fields, "changed": [k for k in COMPOSE_FIELDS if fields[k]],
            "itineraries_changed": str(composed.get("itineraries") or "") != str(page.get("itineraries") or ""),
            "days": {"stored": len(a), "composed": len(b), "planned": len(plan["days"])},
            "plan_mode": plan["mode"], "meta_problems": len(problems), "slug": "not_applicable",
            "report": {k: rep[k] for k in keep if k in rep}}


def audit_one(row: Any, brand_words: list[str]) -> dict:
    """One Master's audit line. Each section that fails is an ``errors`` entry, never dropped."""
    from services.content_generation import s1_writer_v2 as v2
    from services.quality.s1_facts import build_fact_table

    writer, group = writer_of(row)
    out: dict = {"tour_id": _get(row, "tour_id"), "version_id": _get(row, "generated_content_id"),
                 "published_tour_id": _get(row, "published_tour_id"), "version_num": _get(row, "version_num"),
                 "status": _get(row, "status"), "writer": writer, "writer_group": group,
                 "country": _get(row, "country"), "human_edited": bool(_get(row, "human_edited")),
                 "source": {"missing": bool(_get(row, "source_missing")), "deleted": bool(_get(row, "source_deleted")),
                            "status": _get(row, "source_status"), "lifecycle": _get(row, "lifecycle_stage")},
                 "not_run": list(ALWAYS_NOT_RUN), "errors": {}}
    nm = _NoModel()
    page, tour = page_of(row), tour_of(row)
    with ExitStack() as st:
        _no_model_patches(st, nm)
        tour_w = facts = None
        try:
            tour_w = v2.with_agency_fields(tour, agency_of(row))
            facts = build_fact_table(tour_w)
        except Exception as e:  # noqa: BLE001 — a failed section is reported, the audit goes on
            out["errors"]["facts"] = _err(e)
        if facts is not None:
            # the rules first: today's validate codes are the contract's input (contract_of)
            try:
                out["rules"], extra, rule_errors = rules_of(row, page, tour, tour_w, facts, brand_words,
                                                            writer if group == V2 else None, nm)
                out["not_run"] += extra
                out["errors"].update(rule_errors)
            except Exception as e:  # noqa: BLE001
                out["errors"]["rules"] = _err(e)
        validate_today = ((out.get("rules") or {}).get("validate") or {}).get("codes")
        try:
            out["contract"] = contract_of(row, brand_words, validate_today)
        except Exception as e:  # noqa: BLE001
            out["errors"]["contract"] = _err(e)
        try:
            out["brand_words"] = brand_words_of(page, brand_words)
        except Exception as e:  # noqa: BLE001
            out["errors"]["brand_words"] = _err(e)
        if facts is not None:
            try:
                out["compose"] = {"applies": group == V2, **compose_of(page, tour_w, facts, brand_words)}
            except Exception as e:  # noqa: BLE001
                out["errors"]["compose"] = _err(e)
    if nm.llm_attempts:
        out["errors"]["model_call_refused"] = f"{nm.llm_attempts} attempt(s)"
    return out



# ── the summary ───────────────────────────────────────────────────────────────────────────────────


def _per_group() -> dict:
    return {g: Counter() for g in ("all",) + GROUPS}


def summarize(lines: list[dict]) -> dict:
    """Counts per verdict, finding code, rule code and compose field, split by writer group (pure)."""
    c = {k: _per_group() for k in ("masters", "verdict", "verdict_stored_codes", "verdicts_differ",
                                     "verdict_basis", "block_codes", "warn_codes", "validate_codes",
                                     "validate_new", "pre_audit_codes", "pre_audit_needs_model", "s1_check_codes",
                                     "compose_fields", "compose_any", "brand_word_fields", "core_word_fields",
                                     "grounding_numeric", "not_run", "errors", "writers", "verdict_changed",
                                     "source_state")}
    not_publish: list[dict] = []
    differ: list[dict] = []
    for ln in lines:
        groups = ("all", ln.get("writer_group") or "older")
        con = ln.get("contract") or {}
        rules = ln.get("rules") or {}
        comp = ln.get("compose") or {}
        for g in groups:
            c["masters"][g]["masters"] += 1
            c["writers"][g][ln.get("writer") or "unknown"] += 1
            c["verdict"][g][con.get("verdict") or "error"] += 1
            c["verdict_stored_codes"][g][con.get("verdict_stored_codes") or "error"] += 1
            c["verdict_basis"][g][con.get("verdict_basis") or "error"] += 1
            if con.get("verdicts_differ"):
                c["verdicts_differ"][g][f"{con.get('verdict_stored_codes')}->{con.get('verdict_today')}"] += 1
            if con.get("verdict_changed"):
                c["verdict_changed"][g][f"{con.get('stored_verdict')}->{con.get('verdict')}"] += 1
            for f in con.get("findings") or []:
                c["block_codes" if f.get("severity") == "block" else "warn_codes"][g][f["code"]] += 1
            v = rules.get("validate") or {}
            c["validate_codes"][g].update(v.get("codes") or [])
            c["validate_new"][g].update(v.get("new") or [])
            pa = rules.get("pre_audit") or {}
            c["pre_audit_codes"][g].update(pa.get("codes") or [])
            c["pre_audit_needs_model"][g].update(pa.get("needs_model") or [])
            c["s1_check_codes"][g].update({x["code"] for x in rules.get("s1_checks") or []})
            gn = (rules.get("grounding_numeric") or {}).get("violations") or 0
            if gn:
                c["grounding_numeric"][g]["masters"] += 1
                c["grounding_numeric"][g]["sentences"] += gn
            c["compose_fields"][g].update(comp.get("changed") or [])
            if comp.get("changed"):
                c["compose_any"][g]["masters"] += 1
            for h in (ln.get("brand_words") or {}).get("hits") or []:
                c["brand_word_fields" if h["source"] == "brand" else "core_word_fields"][g][h["field"]] += 1
            c["not_run"][g].update(ln.get("not_run") or [])
            c["errors"][g].update((ln.get("errors") or {}).keys())
            src = ln.get("source") or {}
            state = ("missing" if src.get("missing") else "deleted" if src.get("deleted")
                     else f"{src.get('status')}/{src.get('lifecycle')}")
            c["source_state"][g][state] += 1
        if con.get("verdict") != "publish":
            not_publish.append({"tour_id": ln.get("tour_id"), "writer_group": ln.get("writer_group"),
                                "verdict": con.get("verdict") or "error",
                                "verdict_stored_codes": con.get("verdict_stored_codes") or "error",
                                "block_codes": sorted({f["code"] for f in con.get("findings") or []
                                                       if f.get("severity") == "block"})})
        if con.get("verdicts_differ"):
            differ.append({"tour_id": ln.get("tour_id"), "writer_group": ln.get("writer_group"),
                           "verdict_today": con.get("verdict_today"),
                           "verdict_stored_codes": con.get("verdict_stored_codes"),
                           "block_codes_today": sorted({f["code"] for f in con.get("findings") or []
                                                        if f.get("severity") == "block"}),
                           "block_codes_stored_codes": con.get("block_codes_stored_codes") or []})
    out = {k: {g: dict(v[g].most_common()) for g in v} for k, v in c.items()}
    out["masters"] = {g: v.get("masters", 0) for g, v in out["masters"].items()}
    out["not_publish"] = sorted(not_publish, key=lambda r: (r["writer_group"], r["verdict"], r["tour_id"] or ""))
    out["verdicts_differ_list"] = sorted(differ, key=lambda r: (r["writer_group"], r["tour_id"] or ""))
    out["not_run_why"] = dict(NOT_RUN_WHY)
    return out


def population_of(rows: list[Any], unmastered: list[Any], active: Any) -> dict:
    """The master tenant's tours by source / Master / pipeline state (counts) and the unmastered live sources."""
    by: dict[str, Counter] = {k: Counter() for k in ("source", "master", "pipeline_unmastered", "latest_unmastered",
                                                     "writer_of_active_masters_latest")}
    total = 0
    for r in rows:
        n = int(r["n"])
        total += n
        src = "deleted" if r["source_deleted"] else f"{r['source_status']}/{r['lifecycle_stage']}"
        by["source"][src] += n
        mst = r["master_status"] + ("/deleted" if r["master_deleted"] else "")
        if r["master_status_reason"]:
            mst += f"/{r['master_status_reason']}"
        by["master"][mst] += n
        live_source = not r["source_deleted"] and r["source_status"] == "active"
        if live_source and (r["master_status"] != "active" or r["master_deleted"]):
            by["pipeline_unmastered"][r["pipeline_status"]] += n
            by["latest_unmastered"][r["latest_version_status"]] += n
        if r["master_status"] == "active" and not r["master_deleted"]:
            by["writer_of_active_masters_latest"][r["latest_version_writer"]] += n
    return {"raw_tours": total, **{k: dict(v.most_common()) for k, v in by.items()},
            "active_masters": {"master_tenant": _get(active, "master_tenant"),
                               "other_tenants": _get(active, "other_tenants")},
            "unmastered_live_sources": [{k: u[k] for k in ("tour_id", "pipeline_status", "lifecycle_stage",
                                                            "master_status", "master_status_reason",
                                                            "latest_version_status")} for u in unmastered]}


def block(name: str, payload: bytes) -> str:
    b64 = base64.b64encode(gzip.compress(payload, 9)).decode("ascii")
    return "\n".join([f"=== BEGIN {name}", *(b64[i:i + 76] for i in range(0, len(b64), 76)), f"=== END {name}"])


def render(lines: list[dict], summary: dict) -> str:
    """The stdout of a run: the AUDIT_SUMMARY line and the two blocks."""
    jsonl = "\n".join(json.dumps(ln, sort_keys=True, ensure_ascii=False, default=str) for ln in lines) + "\n"
    head = {"masters": summary["masters"], "verdict": summary["verdict"],
            "verdict_stored_codes": summary["verdict_stored_codes"], "verdicts_differ": summary["verdicts_differ"],
            "compose_any": summary["compose_any"],
            "errors": summary["errors"].get("all", {}), "not_publish": len(summary["not_publish"]),
            "raw_tours": (summary.get("population") or {}).get("raw_tours")}
    return "\n".join([
        "AUDIT_SUMMARY " + json.dumps(head, sort_keys=True, default=str),
        block(LINES_BLOCK, jsonl.encode("utf-8")),
        block(SUMMARY_BLOCK, json.dumps(summary, indent=1, sort_keys=True, default=str).encode("utf-8")),
    ])


async def _fetch(limit: int) -> tuple[list, Any, list, list, Any]:
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(masters_sql(), MASTER_TENANT)
            brand = await conn.fetchrow(BRAND_SQL, MASTER_TENANT)
            pop = await conn.fetch(POPULATION_SQL, MASTER_TENANT)
            unm = await conn.fetch(UNMASTERED_SQL, MASTER_TENANT)
            active = await conn.fetchrow(ACTIVE_COUNT_SQL, MASTER_TENANT)
    finally:
        await conn.close()
    return (rows[:limit] if limit else rows), brand, pop, unm, active


def quiet_logs() -> None:
    """The graph nodes log one info line per call: keep warnings only, on stderr, so stdout is the result."""
    import logging

    import structlog
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
                        logger_factory=structlog.PrintLoggerFactory(sys.stderr))


def main() -> int:
    from services.quality.final_contract import CONTRACT_VERSION

    quiet_logs()
    limit = int(os.environ.get("LIMIT", "0") or 0)
    rows, brand, pop, unm, active = asyncio.run(_fetch(limit))
    brand_words = [str(w) for w in _j(_get(brand, "forbidden_words"), [])]
    lines = [audit_one(r, brand_words) for r in rows]
    summary = {"schema": SCHEMA, "contract_version": CONTRACT_VERSION, "limit": limit,
               "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "brand_rule": {"active": brand is not None, "forbidden_words": len(brand_words)},
               **summarize(lines), "population": population_of(list(pop), list(unm), active)}
    print(render(lines, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
