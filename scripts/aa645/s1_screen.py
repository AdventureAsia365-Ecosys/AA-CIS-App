#!/usr/bin/env python3
"""AA-645 / M2a S1-first step 2 (S248) — the S1 writer screen and A/B runner. PAID: every model call goes through
`LLMClient.generate()` and is logged to `shared.llm_call_log` with `record_call_sync(stage="adhoc_aa645",
role="writer")`, `quality_signal.aa645_run` = the run id.

Two uses, one runner:

* **3-model screen** (S248, ~$1.5–2.5): arm W (`s1_writer_v2`) × 3 models × 12 tours OUTSIDE the golden set,
  stratified by duration kind, with the two clean exemplar candidates (af0ded16, dfdb4017; house-style W2) forced in.
  `--source db --sample 12 --include-prefixes af0ded16,dfdb4017 --exclude-ids-file golden_ids.json`.
* **A/B run 1 / run 2** (S250/S251): arms A+ / A' / W / E on golden tours (`--source fixture --fixture <golden>
  --slice 0:25`), one chosen W model. A' is a second, independent A+ run (`--arms aplus,aplus2`).

Per tour × model × arm: at most 3 calls (writer, meta, one meta retry). Every page is checked by code
(`services.quality.s1_checks.run_checks`) and gets a figure-coverage number; the live stored page of a tour (when it
has one) is checked too as arm `stored` ($0). Output (`--out`, and `s3://<bronze>/scripts/out/aa645/<run>/` with
`--s3`):

* `pages/<key>.json` — one per unit, written as soon as it finishes (resume with `--run-id <id> --resume`);
* `pages.json` — every unit, the `fixture_report.py --pages` shape ({sample_id, tour, output} + arm/model/calls);
* `summary.json` — per arm × model: units, errors, calls/tour, $/tour, json failures, meta first-try / final in band,
  gate codes (pages with ≥ 1), figure coverage;
* `panel/packet.json` + `panel/key.json` — the blind panel packet (rubric v1 + the rank question), see
  `score_screen.py`.

Runs ON THE DEPLOYED IMAGE (it imports services.* from /app; the image must carry s1_writer_v2), container
DATABASE_URL (cis_app, SELECT only; ssl=require), shipped through S3:

  cd /app && PYTHONPATH=/tmp/aa645 python3 -m scripts.aa645.s1_screen --source db --sample 12 \\
      --include-prefixes af0ded16,dfdb4017 --exclude-ids-file /tmp/aa645/golden_ids.json \\
      --models haiku-5-5 --arms w --max-cost-usd 1.0 --s3 --run-id <uuid>

Never calls DataForSEO (keywords come from the cached silver_aa_internal.seo_context), never writes a tour,
a version or a job.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

STAGE = "adhoc_aa645"
S3_PREFIX = "scripts/out/aa645"
DEFAULT_MODELS = "haiku-5-5,gpt-6-luna,sonnet-5-5"
ARMS_ALL = ("aplus", "aplus2", "w", "e")      # aplus2 = A' (A+ run again: the A/A noise check of run 1)
GATE_CODES = ("DAY_", "META_CUT", "META_LENGTH", "TEXT_TRUNCATED", "NUMBER_NOT_IN_SOURCE", "INCLUSION_CONTRADICTED")
REPORT_CODES = ("NUMBER_NOUN_MISMATCH", "NAME_NOT_IN_SOURCE", "DAY_PLACE_MISMATCH")
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
# One unit's worst case before it starts (a Sonnet writer + meta + retry on a long tour), per model family.
UNIT_MARGIN_USD = {"sonnet": 0.20, "opus": 0.60}
UNIT_MARGIN_DEFAULT = 0.05

CANDIDATES_SQL = """
SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description,
       rt.src_highlights, rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
       rt.group_size, rt.best_time_to_go, sc.top_keywords, sc.people_also_ask,
       gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights, gc.aa_itineraries,
       gc.seo_title, gc.seo_meta
FROM silver_aa_internal.raw_tours rt
LEFT JOIN silver_aa_internal.seo_context sc ON sc.tour_id = rt.tour_id
LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id AND pt.deleted_at IS NULL
LEFT JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
WHERE rt.deleted_at IS NULL AND rt.source_status = 'active'
  AND coalesce(trim(rt.src_itineraries), '') <> ''
  AND NOT (rt.tour_id::text = ANY($1::text[]))
ORDER BY md5(rt.tour_id::text || $2)
LIMIT $3
"""
FORCED_SQL = """
SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description,
       rt.src_highlights, rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
       rt.group_size, rt.best_time_to_go, sc.top_keywords, sc.people_also_ask,
       gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights, gc.aa_itineraries,
       gc.seo_title, gc.seo_meta
FROM silver_aa_internal.raw_tours rt
LEFT JOIN silver_aa_internal.seo_context sc ON sc.tour_id = rt.tour_id
LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id AND pt.deleted_at IS NULL
LEFT JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
WHERE rt.deleted_at IS NULL AND rt.source_status = 'active' AND rt.tour_id::text LIKE ANY($1::text[])
ORDER BY rt.tour_id
"""


# ------------------------------------------------------------------ inputs

def _json_value(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _texts(value, keys: tuple[str, ...]) -> list[str]:
    """A cached SEO list as plain strings (an item may be a string or a {keyword | question: …} object)."""
    out = []
    for v in value if isinstance(value, list) else []:
        t = next((v.get(k) for k in keys if isinstance(v, dict) and v.get(k)), None) if isinstance(v, dict) else v
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def sample_from_row(r: dict) -> dict:
    """One DB row → {sample_id, tour (source + agency fields), seo, stored (the live page or None)}."""
    from services.content_generation.s1_writer_v2 import with_agency_fields

    hl = _json_value(r.get("src_highlights"))
    tour = {"name": r["src_name"], "subtitle": r.get("src_subtitle"), "summary": r.get("src_summary"),
            "description": r.get("src_description"), "highlights": hl if isinstance(hl, list) else (hl or []),
            "itineraries": r.get("src_itineraries"), "country": r.get("country"), "duration": r.get("duration"),
            "price": r.get("price_raw"), "inclusions": r.get("inclusions"), "exclusions": r.get("exclusions")}
    stored = None
    if r.get("aa_name"):
        ahl = _json_value(r.get("aa_highlights"))
        stored = {"name": r["aa_name"], "subtitle": r.get("aa_subtitle"), "summary": r.get("aa_summary"),
                  "description": r.get("aa_description") or "", "highlights": ahl if isinstance(ahl, list) else [],
                  "itineraries": r.get("aa_itineraries"), "seo_title": r.get("seo_title"),
                  "seo_meta": r.get("seo_meta")}
    kws = _texts(_json_value(r.get("top_keywords")), ("keyword", "text"))
    paa = _texts(_json_value(r.get("people_also_ask")), ("question", "title", "text"))
    # The shape s1_batch builds (today's A+ prompt reads keywords.top_keywords and people_also_ask; W / E read the
    # keywords only, filtered by relevant_keywords).
    seo = {"top_keywords": kws, "keywords": {"top_keywords": kws},
           "people_also_ask": paa}
    return {"sample_id": r["tour_id"], "tour_id": r["tour_id"], "tour": with_agency_fields(tour, r),
            "seo": seo, "stored": stored}


def stratified(samples: list[dict], n: int, forced: list[dict], seed: str) -> list[dict]:
    """`forced` first, then round-robin over duration kinds (hours / one_day / multi_day ≤ 7 / multi_day > 7) of
    the md5-ordered candidates, so a 12-tour screen covers short and long tours."""
    from services.quality.s1_facts import build_fact_table

    def stratum(s):
        d = build_fact_table(s["tour"])["duration"]
        if d["value"] == "multi_day":
            return "multi_long" if (d.get("day_count") or 0) > 7 else "multi_short"
        return d["value"]

    chosen = list(forced)[:n]
    seen = {s["sample_id"] for s in chosen}
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        if s["sample_id"] not in seen:
            buckets[stratum(s)].append(s)
    order = sorted(buckets, key=lambda k: (k == "unknown", k))
    random.Random(seed).shuffle(order)
    while len(chosen) < n and any(buckets.values()):
        for k in order:
            if buckets[k] and len(chosen) < n:
                chosen.append(buckets[k].pop(0))
    return chosen


async def load_db(args) -> tuple[list[dict], dict]:
    """Candidates from the DB (SELECT only) + the A1 brand rule A+ needs."""
    import asyncpg

    excl = load_ids(args.exclude_ids_file)
    prefixes = [p.strip() + "%" for p in (args.include_prefixes or "").split(",") if p.strip()]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        rows = [dict(r) for r in await conn.fetch(CANDIDATES_SQL, excl, args.seed, args.pool)]
        forced = [dict(r) for r in await conn.fetch(FORCED_SQL, prefixes)] if prefixes else []
        brand = await load_brand(conn) if args.aplus_brand else {}
    finally:
        await conn.close()
    forced = [r for r in forced if r["tour_id"] not in excl]
    cands = [sample_from_row(r) for r in rows]
    picked = stratified(cands, args.sample, [sample_from_row(r) for r in forced], args.seed)
    return picked, brand


async def load_brand(conn) -> dict:
    """The A1 brand rule fields today's writer reads (s1_batch._load_prompt_state, without the SEO fetch)."""
    from api.routers.admin_pipeline import _resolve_brand_rule

    br = await _resolve_brand_rule(conn, MASTER_TENANT_ID, None, None)
    if not br:
        return {}
    voice = _json_value(br["voice_examples"]) or []
    fw = _json_value(br["forbidden_words"]) or []
    return {"brand_system_prompt": br["system_prompt"] or "", "brand_style_guide": br["style_guide"] or "",
            "brand_forbidden_words": list(fw), "brand_core_idea": br.get("core_idea") or "",
            "brand_customer_segment": br.get("customer_segment") or "",
            "brand_customer_mindset": br.get("customer_mindset") or "",
            "brand_voice_examples": list(voice), "brand_good_examples": br.get("good_examples") or ""}


def load_ids(path: Optional[str]) -> list[str]:
    if not path:
        return []
    data = json.load(open(path))
    return [str(x) for x in (data.get("ids") if isinstance(data, dict) else data)]


def load_fixture(args) -> list[dict]:
    """Golden S1 samples (AA-729 fixture) for the A/B runs: --slice a:b of the S1 samples in fixture order."""
    fx = json.load(open(args.fixture))
    s1 = [s for s in fx["samples"] if s.get("kind") == "s1"]
    a, _, b = (args.slice or ":").partition(":")
    s1 = s1[int(a or 0): int(b) if b else None]
    return [{"sample_id": s["sample_id"], "tour_id": (s.get("refs") or {}).get("tour_id"), "tour": s["input"]["tour"],
             "seo": s["input"].get("seo") or {}, "stored": s.get("output")} for s in s1]


def load_pages_file(path: str, n: int, seed: str) -> list[dict]:
    """A held-out page list (root audit heldout/to_pages.py output): {sample_id, tour, output}."""
    pages = json.load(open(path))
    out = [{"sample_id": p["sample_id"], "tour_id": p["sample_id"], "tour": p["tour"], "seo": p.get("seo") or {},
            "stored": p.get("output")} for p in pages]
    return stratified(out, n, [], seed) if n else out


# ------------------------------------------------------------------ spend

class Spend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usd = 0.0
        self.calls = 0

    def add(self, usd: float) -> None:
        with self._lock:
            self.usd += float(usd or 0.0)
            self.calls += 1


SPEND = Spend()


def unit_margin(model: str) -> float:
    return next((v for k, v in UNIT_MARGIN_USD.items() if k in model), UNIT_MARGIN_DEFAULT)


# ------------------------------------------------------------------ one unit (tour × model × arm)

def llm_call(client, run_id: str, unit: dict, call: str, system: str, user: str, max_tokens: int) -> dict:
    """One model call through the gateway, logged as adhoc_aa645 / writer. Returns the call record with the reply
    text; a gateway error is recorded (the gateway logs a billed-but-unusable attempt itself)."""
    from shared.llm_client.call_log import record_call_sync
    from shared.llm_client.models import LLMRequest
    from services.content_generation.s1_writer_v2 import parse_json

    rec = {"call": call, "ok": False}
    t0 = time.monotonic()
    try:
        resp = client.generate(LLMRequest(system_prompt=system, user_prompt=user, model_tier=unit["model"],
                                          stage=STAGE, max_tokens=max_tokens))
    except Exception as e:  # noqa: BLE001 — recorded per unit, the run goes on
        rec.update(error=str(e)[:500], latency_s=round(time.monotonic() - t0, 1))
        return rec
    parsed = parse_json(resp.content)
    SPEND.add(resp.cost_usd)
    record_call_sync(
        stage=STAGE, role="writer", model=resp.model_used, tokens_in=resp.input_tokens,
        tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
        quality_signal={"aa645_run": run_id, "arm": unit["arm"], "model_key": unit["model"], "call": call,
                        "sample": unit["sample_id"], "json_parsed": parsed is not None},
        stop_reason=resp.stop_reason, account=resp.satellite_account, fallback_used=resp.fallback_used,
        provider=resp.provider if isinstance(resp.provider, str) else None)
    rec.update(ok=parsed is not None, parsed=parsed, model_used=resp.model_used, tokens_in=resp.input_tokens,
               tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, stop_reason=resp.stop_reason,
               latency_s=round(time.monotonic() - t0, 1))
    if parsed is None:
        rec["raw_tail"] = (resp.content or "")[-300:]
    return rec


COVERAGE_SKIP_UNITS = frozenset({"day", "night", "week"})   # the trip length: structure, written as words


def figure_coverage(facts: dict, page: dict) -> Optional[float]:
    """Share of the source's operational figures (km, hours, metres, … — not the trip length, not the name) whose
    number appears in the page: the "fact coverage" side metric of the A/B (q4-q8 §2.2), code-only. None when the
    source has none."""
    from services.quality.s1_facts import field_text

    nums = {n for e in facts.get("numbers") or [] if e.get("role", "figure") == "figure" and e.get("unit")
            and e.get("unit") not in COVERAGE_SKIP_UNITS and e.get("field") not in ("name", "duration")
            for n in e["numbers"]}
    if not nums:
        return None
    text = " ".join(field_text(page.get(f)) for f in ("subtitle", "summary", "description", "highlights",
                                                      "itineraries", "seo_meta"))
    have = set(re.findall(r"\d+(?:[.,]\d+)?", text))
    return round(len({n for n in nums if n in have}) / len(nums), 3)


def run_unit(client, run_id: str, unit: dict, sample: dict, ctx: dict) -> dict:
    """Writer → page → forbidden strip → meta (+ one retry) → code checks. Never raises."""
    from services.content_generation import s1_writer_v2 as v2
    from services.content_generation.forbidden_strip import strip_forbidden
    from services.quality.s1_checks import run_checks
    from services.quality.s1_facts import build_fact_table

    arm = "aplus" if unit["arm"] == "aplus2" else unit["arm"]
    tour = sample["tour"]
    facts = build_fact_table(tour)
    plan = v2.day_plan(tour, facts)
    out = {**unit, "tour_id": sample.get("tour_id"), "calls": [], "error": None, "plan_days": len(plan["days"]),
           "plan_mode": plan["mode"]}
    aplus_state = {**ctx["brand"], "seo": sample.get("seo") or {}} if arm == "aplus" else None
    system, user = v2.arm_prompts(arm, tour, facts, plan, forbidden=ctx["forbidden"], background=ctx["background"],
                                  examples=ctx["examples"], aplus_state=aplus_state)
    w = llm_call(client, run_id, unit, "write", system, user, v2.WRITER_MAX_TOKENS)
    out["calls"].append({k: v for k, v in w.items() if k != "parsed"})
    if not w["ok"]:
        out["error"] = w.get("error") or "writer reply is not JSON"
        return finish(out)
    page = v2.page_from_writer(arm, tour, facts, plan, w["parsed"])
    page, report = strip_forbidden(page, ctx["brand"].get("brand_forbidden_words"))
    out["forbidden_strip"] = {"replaced": len(report["replaced"]), "dropped": len(report["dropped"]),
                              "unresolved": len(report["unresolved"])}
    keywords = v2.relevant_keywords(sample.get("seo"), tour)
    msys, muser = v2.meta_system_prompt(ctx["forbidden"]), v2.meta_user_prompt(page, tour, facts, keywords)
    m1 = llm_call(client, run_id, unit, "meta", msys, muser, v2.META_MAX_TOKENS)
    out["calls"].append({k: v for k, v in m1.items() if k != "parsed"})
    meta_json = m1.get("parsed")
    problems = v2.meta_problems(meta_json, tour, facts, ctx["forbidden"]) if m1["ok"] else ["no answer"]
    out["meta_first_problems"] = problems
    if problems and m1["ok"]:
        m2 = llm_call(client, run_id, unit, "meta_retry", msys, muser + v2.meta_retry_prompt(meta_json, problems),
                      v2.META_MAX_TOKENS)
        out["calls"].append({k: v for k, v in m2.items() if k != "parsed"})
        if m2["ok"]:
            meta_json = m2["parsed"]
            problems = v2.meta_problems(meta_json, tour, facts, ctx["forbidden"])
    out["meta_final_problems"] = problems
    out["keywords"] = keywords
    out["output"] = v2.finalize_meta(page, meta_json)
    out["findings"] = run_checks(tour, out["output"], facts)
    out["figure_coverage"] = figure_coverage(facts, out["output"])
    out["ask_supplier"] = v2.ask_supplier(facts)
    return finish(out)


def finish(out: dict) -> dict:
    out["cost_usd"] = round(sum(c.get("cost_usd") or 0 for c in out["calls"]), 6)
    out["n_calls"] = len(out["calls"])
    return out


def stored_unit(sample: dict) -> Optional[dict]:
    """The live stored page of the tour, checked by code ($0): the today's-pipeline reference."""
    from services.quality.s1_checks import run_checks
    from services.quality.s1_facts import build_fact_table

    if not sample.get("stored"):
        return None
    facts = build_fact_table(sample["tour"])
    return {"key": f"{sample['sample_id']}__stored", "sample_id": sample["sample_id"], "tour_id": sample.get("tour_id"),
            "arm": "stored", "model": "live", "calls": [], "n_calls": 0, "cost_usd": 0.0, "error": None,
            "output": sample["stored"], "findings": run_checks(sample["tour"], sample["stored"], facts),
            "figure_coverage": figure_coverage(facts, sample["stored"])}


# ------------------------------------------------------------------ summary + panel packet

def gate_hit(code: str) -> bool:
    return code.startswith(GATE_CODES)


def summarize(units: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        groups[f"{u['arm']}|{u['model']}"].append(u)
    out = {}
    for key, us in sorted(groups.items()):
        ok = [u for u in us if not u.get("error")]
        gates, reports = Counter(), Counter()
        for u in ok:
            codes = {f["code"] for f in u.get("findings") or [] if f["code"] != "SOURCE_DAY_CONTRADICTION"}
            gates.update({("DAY_*" if c.startswith("DAY_") else c) for c in codes if gate_hit(c)})
            reports.update({c for c in codes if c in REPORT_CODES})
        cov = [u["figure_coverage"] for u in ok if u.get("figure_coverage") is not None]
        paid = [u for u in us if u["arm"] != "stored"]
        out[key] = {
            "units": len(us), "errors": len(us) - len(ok),
            "pages_with_any_gate_code": sum(1 for u in ok if any(gate_hit(f["code"]) for f in u.get("findings") or [])),
            "gate_codes_pages": dict(sorted(gates.items())), "report_codes_pages": dict(sorted(reports.items())),
            "figure_coverage_mean": round(sum(cov) / len(cov), 3) if cov else None,
            "calls_per_tour": round(sum(u["n_calls"] for u in paid) / len(paid), 2) if paid else 0,
            "usd_per_tour": round(sum(u["cost_usd"] for u in paid) / len(paid), 4) if paid else 0,
            "usd_total": round(sum(u["cost_usd"] for u in paid), 4),
            "meta_first_try_ok": sum(1 for u in ok if u.get("meta_first_problems") == []),
            "meta_final_ok": sum(1 for u in ok if u.get("meta_final_problems") == []),
            "writer_stop_max_tokens": sum(1 for u in us for c in u["calls"]
                                          if c["call"] == "write" and c.get("stop_reason") in ("max_tokens", "length")),
        }
    return out


PANEL_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries", "seo_title", "seo_meta")


def panel_packet(units: list[dict], samples: dict, seed: str, include_stored: bool) -> tuple[dict, dict]:
    """Blind packet: per tour a group with the FULL source and its versions under shuffled page ids (p01…); the key
    (page id → arm, model, sample) is a separate file the panel never sees."""
    rnd = random.Random(seed)
    by_sample: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        if u.get("error") or (u["arm"] == "stored" and not include_stored):
            continue
        by_sample[u["sample_id"]].append(u)
    groups, key, n = [], {}, 0
    sample_ids = sorted(by_sample)
    rnd.shuffle(sample_ids)
    for gi, sid in enumerate(sample_ids, 1):
        versions = list(by_sample[sid])
        rnd.shuffle(versions)
        pages = []
        for u in versions:
            n += 1
            pid = f"p{n:03d}"
            key[pid] = {"key": u["key"], "sample_id": sid, "arm": u["arm"], "model": u["model"]}
            pages.append({"page_id": pid, "page": {f: (u.get("output") or {}).get(f) for f in PANEL_FIELDS}})
        groups.append({"group_id": f"g{gi:02d}", "source": samples[sid]["tour"], "pages": pages})
    packet = {"rubric": "s1-panel-v1 (root docs/audits/2026-10-23-S248-s1-step01/panel/rubric.md)",
              "task": ("For every page: the rubric v1 entry (verdict, agency_use, checkable_claims, errors). For every "
                       "group: rank its pages, 1 = the page an agency should publish first; ties allowed."),
              "return_shape": {"pages": [{"page_id": "p001", "verdict": "...", "agency_use": "...",
                                          "checkable_claims": 0, "errors": []}],
                               "ranks": [{"group_id": "g01", "order": [["p002"], ["p001", "p003"]]}]},
              "groups": groups}
    return packet, key


# ------------------------------------------------------------------ output

class Sink:
    """Local out dir + optional S3 mirror (bucket aa-cis-bronze-<account>, account read at runtime)."""

    def __init__(self, out_dir: str, run_id: str, s3: bool) -> None:
        self.out_dir, self.run_id = out_dir, run_id
        os.makedirs(os.path.join(out_dir, "pages"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "panel"), exist_ok=True)
        self.s3 = self.bucket = None
        if s3:
            import boto3
            account = boto3.client("sts").get_caller_identity()["Account"]
            self.s3, self.bucket = boto3.client("s3"), f"aa-cis-bronze-{account}"
        self._lock = threading.Lock()

    def put(self, rel: str, obj: Any) -> None:
        body = json.dumps(obj, indent=1, ensure_ascii=False, default=str)
        with self._lock:
            with open(os.path.join(self.out_dir, rel), "w") as fh:
                fh.write(body)
        if self.s3:
            self.s3.put_object(Bucket=self.bucket, Key=f"{S3_PREFIX}/{self.run_id}/{rel}", Body=body.encode())

    def existing(self) -> dict[str, dict]:
        found = {}
        pdir = os.path.join(self.out_dir, "pages")
        for name in os.listdir(pdir):
            if name.endswith(".json"):
                found[name[:-5]] = json.load(open(os.path.join(pdir, name)))
        if self.s3:
            pages = self.s3.get_paginator("list_objects_v2")
            for page in pages.paginate(Bucket=self.bucket, Prefix=f"{S3_PREFIX}/{self.run_id}/pages/"):
                for obj in page.get("Contents") or []:
                    k = obj["Key"].rsplit("/", 1)[-1][:-5]
                    if k not in found:
                        body = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()
                        found[k] = json.loads(body)
        return found


def safe_key(*parts: str) -> str:
    return "__".join(re.sub(r"[^A-Za-z0-9.-]+", "-", p) for p in parts)


# ------------------------------------------------------------------ main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", choices=("db", "fixture", "pages"), required=True)
    ap.add_argument("--fixture", help="golden AA-729 fixture (--source fixture)")
    ap.add_argument("--slice", help="a:b of the fixture's S1 samples (e.g. 0:25 for run 1, 25:50 for run 2)")
    ap.add_argument("--pages", help="held-out page list (--source pages)")
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--pool", type=int, default=300, help="DB candidates read before stratifying")
    ap.add_argument("--seed", default="s248-aa645")
    ap.add_argument("--include-prefixes", default="", help="tour id prefixes forced into the sample")
    ap.add_argument("--exclude-ids-file", help="JSON list of tour ids to leave out (the golden set)")
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--arms", default="w", help="comma list of aplus,aplus2,w,e")
    ap.add_argument("--limit", type=int, default=0, help="first N tours only (the 3-tour trial)")
    ap.add_argument("--background", choices=("allow", "forbid"), default="allow")
    ap.add_argument("--examples", help="JSON list of reference examples (private root repo)")
    ap.add_argument("--aplus-brand", action="store_true", help="load the A1 brand rule for A+ (DB)")
    ap.add_argument("--max-cost-usd", type=float, default=3.0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--run-id")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", default="/tmp/aa645")
    ap.add_argument("--s3", action="store_true")
    ap.add_argument("--panel-include-stored", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="select + build prompts, no model call ($0)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from services.content_generation.forbidden_words import all_forbidden

    run_id = str(uuid.UUID(args.run_id)) if args.run_id else str(uuid.uuid4())
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ARMS_ALL]
    if bad:
        raise SystemExit(f"unknown arm(s) {bad}; one of {ARMS_ALL}")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    brand: dict = {}
    if args.source == "db":
        samples, brand = asyncio.run(load_db(args))
    elif args.source == "fixture":
        samples = load_fixture(args)
    else:
        samples = load_pages_file(args.pages, args.sample, args.seed)
    if args.limit:
        samples = samples[: args.limit]
    ctx = {"brand": brand, "forbidden": all_forbidden(brand.get("brand_forbidden_words")),
           "background": args.background, "examples": json.load(open(args.examples)) if args.examples else None}
    sink = Sink(os.path.join(args.out, run_id), run_id, args.s3)
    by_id = {s["sample_id"]: s for s in samples}
    sink.put("samples.json", [{"sample_id": s["sample_id"], "tour_id": s.get("tour_id"),
                               "name": s["tour"].get("name"), "duration": s["tour"].get("duration")} for s in samples])
    done = sink.existing() if args.resume else {}
    units = [{"key": safe_key(s["sample_id"], a, m), "sample_id": s["sample_id"], "arm": a, "model": m}
             for s in samples for m in models for a in arms]
    todo = [u for u in units if u["key"] not in done]
    print(f"run={run_id} samples={len(samples)} units={len(units)} todo={len(todo)} cap=${args.max_cost_usd}",
          flush=True)
    if args.dry_run:
        from services.content_generation import s1_writer_v2 as v2
        from services.quality.s1_facts import build_fact_table
        for s in samples:
            f = build_fact_table(s["tour"])
            p = v2.day_plan(s["tour"], f)
            print(s["sample_id"], p["mode"], len(p["days"]), "kw:", v2.relevant_keywords(s.get("seo"), s["tour"]))
        return 0

    from shared.llm_client.client import LLMClient
    from shared.llm_client.call_log import wait_for_pending_writes

    client = LLMClient()
    results = dict(done)
    skipped = []

    def go(u):
        if SPEND.usd + unit_margin(u["model"]) > args.max_cost_usd:
            return {**u, "error": "skipped_cost_cap", "calls": [], "n_calls": 0, "cost_usd": 0.0}
        return run_unit(client, run_id, u, by_id[u["sample_id"]], ctx)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(go, u): u for u in todo}
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("error") == "skipped_cost_cap":
                skipped.append(r["key"])
                continue
            results[r["key"]] = r
            sink.put(f"pages/{r['key']}.json", r)
            print(f"{r['key']} calls={r['n_calls']} ${r['cost_usd']:.4f} err={r.get('error')} "
                  f"spend=${SPEND.usd:.4f}", flush=True)
    wait_for_pending_writes()
    for s in samples:
        st = stored_unit(s)
        if st:
            results[st["key"]] = st
    all_units = sorted(results.values(), key=lambda u: u["key"])
    sink.put("pages.json", [{**u, "tour": by_id[u["sample_id"]]["tour"]} for u in all_units
                            if u["sample_id"] in by_id])
    summary = {"run_id": run_id, "stage": STAGE, "spend_usd_this_process": round(SPEND.usd, 4),
               "calls_this_process": SPEND.calls, "skipped_cost_cap": skipped, "args": vars(args),
               "by_arm_model": summarize(all_units)}
    sink.put("summary.json", summary)
    packet, key = panel_packet(all_units, by_id, args.seed, args.panel_include_stored)
    sink.put("panel/packet.json", packet)
    sink.put("panel/key.json", key)
    print(json.dumps(summary["by_arm_model"], indent=1), flush=True)
    print(f"DONE run={run_id} spend=${SPEND.usd:.4f} calls={SPEND.calls} skipped={len(skipped)} "
          f"out={sink.out_dir}" + (f" s3://{sink.bucket}/{S3_PREFIX}/{run_id}/" if sink.s3 else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
