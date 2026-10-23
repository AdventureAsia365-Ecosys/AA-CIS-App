#!/usr/bin/env python3
"""AA-645 (S249) — offline re-measure of the A1 brand-fit judge: rubric v1 vs rubric v2 on the A/B run-1 pages. PAID.

Question (S248 platform-impact memo section 8, Nghiep d02): does rubric v2 (`services/content_generation/brand_fit_v2`,
the white-label voice within the source) stop penalising the source-faithful writer-v2 pages and reward fidelity, so
the A1 brand-fit gate can be switched back on for writer v2 (`A1_V2_BRAND_FIT_GATE=enforce`)?

Pass criterion (fixed BEFORE the run, run sheet): on rubric v2,
  retry % of w|sonnet-5-5 <= retry % of aplus|haiku + 5 points   AND   page-level Spearman(brand fit, panel serious
  errors per claim) <= 0.
`brandfit_analyze.py` computes it from this script's output.

Same path as A1 (the S248 `brandfit.py` it replaces, extended to two rubrics):
  * brand profile = the master tenant's A1 brand rule (`admin_pipeline._resolve_brand_rule`, as s1_batch does) — v1
    reads it, v2 ignores it (white-label);
  * `brand_fit.score_brand_fit(state, page, mission_absent_cap=6.0, stage="adhoc_aa645", model_override=<the live
    s1_judge route's model_id>, rubric=...)` — the AA-714 near-band median of 3 included; v2 adds `source=<the raw
    tour of the page>`; A1 gate = `judge_node.a1_judge_score` (brand fit alone) < 7.0;
  * one llm_call_log row per billed call through `judge_node._judge_rows` / `_write_judge_rows`
    (stage adhoc_aa645, role judge, quality_signal.measurement = "aa645_brandfit_rubric", .aa645_key, .rubric), a
    billed failure through `record_billed_failure_sync`. No route fallback, no shadow (model_override). The Jev
    tie-break is not called: pages within +/-0.5 of 7.0 are reported as `tie_band`.

Runs on TODAY's image (no deploy needed): when the deployed image has no `brand_fit_v2`, `--overlay DIR` loads this
branch's `services/content_generation/brand_fit_v2.py` and `brand_fit.py` from DIR into the process (sys.modules),
before anything imports them — the exact code the PR ships, not a copy of its prompt. Nothing else is overlaid. The
result header names the code source (`image` | `overlay`) and the sha256 of both files. After the deploy the image's
modules are used (the overlay is ignored unless `--force-overlay`).

Input: a tarball (or a plain JSON file) holding `pages.json` — the A/B run-1 units (s1_screen output: key, sample_id,
arm, model, tour, output). Output: JSON between BRANDFIT-BEGIN / BRANDFIT-END on stdout and the same JSON at
`--s3-out` (rewritten every 10 units; `--resume` skips units already ok there).

  cd /app && PYTHONPATH=/tmp/aa645 python3 -m scripts.aa645.brandfit_measure --pages /tmp/aa645/bf-pages.tgz \\
      --overlay /tmp/aa645/overlay --rubrics v1,v2 --max-cost-usd 1.2 --s3-out s3://<bronze>/scripts/out/aa645/...
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

sys.path.insert(0, os.getcwd())

STAGE = "adhoc_aa645"
ROLE = "judge"
MEASUREMENT = "aa645_brandfit_rubric"
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ARMS = "aplus|haiku,w|sonnet-5-5,w|gpt-6-luna,e|sonnet-5-5,e|gpt-6-luna"
DEFAULT_RUBRICS = "v1,v2"
MIN_QUALITY = 7.0
TIE_BAND = 0.5
MISSION_ABSENT_CAP = 6.0            # judge_node._MISSION_ABSENT_CAP (A1 passes it; the A1 gate ignores it)
MAX_CALLS_PER_UNIT = 3              # 1 + brand_fit._REPEAT_EXTRA
# s1_judge gpt-5.6-luna: ~$0.002 a v1 call (60-day mean); a v2 prompt carries the source fact sheet (median ~12.5k
# characters on the 125 pages vs ~5k for v1) — start pessimistic, the running mean takes over after the first calls.
INITIAL_EST_CALL_USD = {"v1": 0.003, "v2": 0.006}
OVERLAY_MODULES = ("services/content_generation/brand_fit_v2.py", "services/content_generation/brand_fit.py")


# --------------------------------------------------------------------------------------------- code source
def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _image_has_v2() -> bool:
    try:
        return importlib.util.find_spec("services.content_generation.brand_fit_v2") is not None
    except (ImportError, ValueError):
        return False


def apply_overlay(root: Optional[str], force: bool = False) -> dict:
    """Load this branch's rubric code into the process when the image lacks it (or `force`). Must run before any
    import of services.content_generation.brand_fit / judge_node. Returns {"source", "files": {path: sha}}."""
    if _image_has_v2() and not force:
        import services.content_generation.brand_fit as bf
        import services.content_generation.brand_fit_v2 as v2
        return {"source": "image", "files": {p: _sha(m.__file__) for p, m in zip(OVERLAY_MODULES, (v2, bf))}}
    if not root:
        raise SystemExit("the deployed image has no services/content_generation/brand_fit_v2.py: pass --overlay DIR "
                         "(this branch's files) or run after the deploy")
    if "services.content_generation.brand_fit" in sys.modules and not force:
        raise SystemExit("brand_fit was imported before the overlay — the overlay must load first")
    import services.content_generation as pkg
    files = {}
    for rel in OVERLAY_MODULES:                       # brand_fit_v2 first: brand_fit imports it lazily by name
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            raise SystemExit(f"overlay file missing: {path}")
        name = rel[:-3].replace("/", ".")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        setattr(pkg, name.rsplit(".", 1)[1], mod)
        files[rel] = _sha(path)
    return {"source": "overlay", "files": files}


# --------------------------------------------------------------------------------------------- inputs
def _read_units(path: str) -> list:
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and os.path.basename(m.name) == "pages.json"),
                          None)
            if member is None:
                raise SystemExit(f"{path}: no pages.json inside")
            return json.load(tf.extractfile(member))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_units(path: str, arms: set[str], rubrics: list[str], limit: int = 0) -> list[dict]:
    """(page, rubric) units from the A/B run-1 pages.json, filtered to `arms` ("arm|model"), pages with an output and
    a source tour only; ordered by sample, then rubric, then arm, so a cost-cap stop leaves arms and rubrics
    balanced."""
    by_sample: dict[str, list[dict]] = {}
    for d in _read_units(path):
        if not isinstance(d, dict) or "arm" not in d:
            continue
        if f"{d['arm']}|{d['model']}" not in arms or d.get("error") or not d.get("output") or not d.get("tour"):
            continue
        by_sample.setdefault(d["sample_id"], []).append(d)
    samples = sorted(by_sample)[:limit] if limit else sorted(by_sample)
    order = sorted(arms)
    out = []
    for s in samples:
        pages = sorted(by_sample[s], key=lambda d: order.index(f"{d['arm']}|{d['model']}"))
        for rubric in rubrics:
            out += [{"page": p, "rubric": rubric, "unit_key": f"{p['key']}|{rubric}"} for p in pages]
    return out


async def _load_brand_db() -> dict:
    """The A1 brand rule fields the graph state carries (s1_batch._load_prompt_state)."""
    import asyncpg
    from api.routers.admin_pipeline import _resolve_brand_rule

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        br = await _resolve_brand_rule(conn, MASTER_TENANT_ID, None, None)
    finally:
        await conn.close()
    if not br:
        return {}

    def js(v):
        if isinstance(v, (list, dict)) or v is None:
            return v
        try:
            return json.loads(v)
        except ValueError:
            return v
    return {"brand_name": br["brand_name"], "brand_forbidden_words": list(js(br["forbidden_words"]) or []),
            "brand_core_idea": br.get("core_idea") or "", "brand_customer_segment": br.get("customer_segment") or "",
            "brand_customer_mindset": br.get("customer_mindset") or "",
            "brand_voice_examples": list(js(br["voice_examples"]) or []),
            "brand_good_examples": br.get("good_examples") or ""}


def load_brand(args) -> dict:
    if args.brand_json:                         # offline test only
        with open(args.brand_json, encoding="utf-8") as f:
            return json.load(f)
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL missing (the A1 brand rule is read from the DB)")
    return asyncio.run(_load_brand_db())


def judge_model(args) -> tuple[str, dict]:
    """The live s1_judge route (what A1 calls today)."""
    if args.model and args.brand_json:          # offline test: no route read
        return args.model, {}
    from shared.llm_client.role_config import get_stage_config_sync
    cfg = get_stage_config_sync("s1_judge")
    route = {"model_id": cfg.model_id, "fallback_model_ids": list(cfg.fallback_model_ids or ()),
             "shadow_model_id": cfg.shadow_model_id, "account_route": cfg.account_route}
    return (args.model or cfg.model_id), route


# --------------------------------------------------------------------------------------------- spend
class Spend:
    """Thread-safe spend + reservation per rubric, so concurrent units never overshoot the cap."""

    def __init__(self, cap: float) -> None:
        self.cap, self.usd, self.reserved = cap, 0.0, 0.0
        self.calls: dict[str, int] = {}
        self.by_rubric: dict[str, float] = {}
        self.lock = threading.Lock()

    def est_call(self, rubric: str) -> float:
        n, usd = self.calls.get(rubric, 0), self.by_rubric.get(rubric, 0.0)
        base = INITIAL_EST_CALL_USD.get(rubric, 0.006)
        return max(base, usd / n) if n else base

    def reserve(self, rubric: str) -> Optional[float]:
        with self.lock:
            r = MAX_CALLS_PER_UNIT * self.est_call(rubric)
            if self.usd + self.reserved + r > self.cap:
                return None
            self.reserved += r
            return r

    def settle(self, rubric: str, reserved: float, usd: float, calls: int) -> None:
        with self.lock:
            self.reserved -= reserved
            self.usd += usd
            self.calls[rubric] = self.calls.get(rubric, 0) + calls
            self.by_rubric[rubric] = self.by_rubric.get(rubric, 0.0) + usd


# --------------------------------------------------------------------------------------------- judge
def score_page(unit: dict, brand: dict, model: str) -> dict:
    """One (page, rubric) through the A1 judge; writes its llm_call_log rows. Never raises."""
    from services.content_generation import brand_fit, judge_node
    from shared.llm_client.call_log import record_billed_failure_sync

    page, rubric = unit["page"], unit["rubric"]
    key = unit["unit_key"]
    state = {**brand, "tour": {"tour_id": page.get("tour_id")}, "retry_count": 0}
    t0 = time.monotonic()
    base = {"key": key, "page_key": page["key"], "rubric": rubric, "sample_id": page["sample_id"],
            "arm": page["arm"], "model": page["model"], "arm_model": f"{page['arm']}|{page['model']}",
            "tour_id": page.get("tour_id")}
    kwargs = {"mission_absent_cap": MISSION_ABSENT_CAP, "stage": STAGE, "model_override": model}
    if rubric == "v2":
        kwargs.update(rubric="v2", source=page["tour"])
    try:
        result = brand_fit.score_brand_fit(state, page["output"], **kwargs)
    except Exception as e:  # noqa: BLE001 — one page must not stop the run
        billed = brand_fit.billed_calls(e)
        record_billed_failure_sync(stage=STAGE, role=ROLE, billed=billed, error=str(e)[:300],
                                   quality_signal_extra={"aa645_key": key, "measurement": MEASUREMENT,
                                                         "rubric": rubric})
        return {**base, "ok": False, "error": str(e)[:300], "cost_usd": brand_fit.billed_cost(e),
                "calls": len(billed), "latency_s": round(time.monotonic() - t0, 1)}

    score = judge_node.a1_judge_score(result)                       # A1 gate: brand fit alone (AA-698)
    runs = [r for r in (result.runs or ()) if isinstance(r, dict) and "brand_fit" in r]
    n_calls = max(1, len(runs)) + len(result.billed_failed or ())
    v2_fields = {}
    if getattr(result, "rubric", "v1") == "v2":
        from services.content_generation.brand_fit_v2 import RUBRIC_V2_VERSION
        unsupported = list(result.unsupported or ())
        v2_fields = {"rubric_version": RUBRIC_V2_VERSION, "raw_brand_fit": result.raw_brand_fit,
                     "unsupported": len(unsupported), "unsupported_quotes": [c["quote"] for c in unsupported][:8],
                     "unverified_unsupported": result.unverified_unsupported, "leaks": list(result.leaks or ()),
                     "caps": list(result.caps or ())}
    elif rubric == "v2":
        return {**base, "ok": False, "error": "rubric v2 requested but the verdict is v1 (code source?)",
                "cost_usd": result.cost_usd, "calls": n_calls}
    signal = {"judge_score": score, "brand_fit_score": result.brand_fit_score,
              "cross_brand_distinct": result.cross_brand_distinct, "mission_present": result.mission_present,
              "passed": score >= MIN_QUALITY, "gate": "brand_fit", "measurement": MEASUREMENT,
              "aa645_key": key, "rubric": v2_fields.get("rubric_version", "v1"),
              **{k: v for k, v in v2_fields.items() if k in ("unsupported", "caps", "leaks", "raw_brand_fit")}}
    judge_node._write_judge_rows(judge_node._judge_rows(STAGE, result, signal))
    feedback = result.feedback or ""
    return {**base, "ok": True, "brand_fit": result.brand_fit_score, "distinct": result.cross_brand_distinct,
            "mission_present": result.mission_present, "judge_score": score,
            "retry": score < MIN_QUALITY, "tie_band": abs(score - MIN_QUALITY) <= TIE_BAND,
            "brand_fits": [r["brand_fit"] for r in runs] or [result.brand_fit_score],
            "feedback": feedback[:500], "feedback_says_generic": "generic" in feedback.lower(),
            "model_used": result.model_used, "cost_usd": round(result.cost_usd or 0.0, 6), "calls": n_calls,
            "latency_s": round(time.monotonic() - t0, 1), **v2_fields}


# --------------------------------------------------------------------------------------------- summary
def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def summarise(rows: list[dict]) -> dict:
    """Per rubric -> arm: n, mean / median brand fit, retry rate, tie band, generic feedback, v2 caps; calls, $."""
    out: dict[str, dict] = {}
    for rubric in sorted({r["rubric"] for r in rows}):
        rr = [r for r in rows if r["rubric"] == rubric]
        by: dict[str, dict] = {}
        for arm in sorted({r["arm_model"] for r in rr}):
            all_ = [r for r in rr if r["arm_model"] == arm]
            ok = [r for r in all_ if r.get("ok")]
            fits = [r["brand_fit"] for r in ok]
            by[arm] = {
                "n": len(ok), "failed": len(all_) - len(ok),
                "brand_fit_mean": round(sum(fits) / len(fits), 3) if fits else None,
                "brand_fit_median": _median(fits),
                "retry_rate": round(sum(r["retry"] for r in ok) / len(ok), 3) if ok else None,
                "tie_band": sum(r["tie_band"] for r in ok),
                "feedback_says_generic": sum(r["feedback_says_generic"] for r in ok),
                "capped": sum(1 for r in ok if r.get("caps")),
                "unsupported_mean": (round(sum(r.get("unsupported", 0) for r in ok) / len(ok), 3)
                                     if ok and rubric == "v2" else None),
                "calls": sum(r.get("calls", 0) for r in all_),
                "cost_usd": round(sum(r.get("cost_usd") or 0 for r in all_), 4),
            }
        out[rubric] = by
    return out


# --------------------------------------------------------------------------------------------- s3
def _s3_split(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"--s3-out must be s3://bucket/key, got {uri!r}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def s3_put(uri: Optional[str], body: str) -> None:
    if not uri:
        return
    import boto3
    bucket, key = _s3_split(uri)
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json")


def s3_get(uri: Optional[str]) -> Optional[dict]:
    if not uri:
        return None
    import boto3
    bucket, key = _s3_split(uri)
    try:
        return json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001 — nothing to resume from
        return None


# --------------------------------------------------------------------------------------------- main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pages", required=True, help="tarball (or JSON file) holding the A/B run-1 pages.json")
    ap.add_argument("--s3-out", help="s3://bucket/key for the result JSON (also rewritten every 10 units)")
    ap.add_argument("--rubrics", default=DEFAULT_RUBRICS, help="comma list of v1,v2")
    ap.add_argument("--arms", default=DEFAULT_ARMS, help="comma list of arm|model")
    ap.add_argument("--max-cost-usd", type=float, default=1.2)
    ap.add_argument("--limit", type=int, default=0, help="first N samples only (trial)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--model", help="override the s1_judge route model (default: the live route's model_id)")
    ap.add_argument("--overlay", help="dir holding this branch's services/content_generation/brand_fit{,_v2}.py")
    ap.add_argument("--force-overlay", action="store_true", help="use the overlay even when the image has v2")
    ap.add_argument("--resume", action="store_true", help="skip units already ok in the --s3-out JSON")
    ap.add_argument("--single-call", action="store_true",
                    help="skip the AA-714 near-band median-of-3 (first call only; not the production path)")
    ap.add_argument("--dry-run", action="store_true", help="load pages + brand + route, build 1 prompt per rubric, $0")
    ap.add_argument("--brand-json", help=argparse.SUPPRESS)       # offline test only
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rubrics = [r.strip() for r in args.rubrics.split(",") if r.strip()]
    if not rubrics or set(rubrics) - {"v1", "v2"}:
        raise SystemExit(f"--rubrics must be a subset of v1,v2, got {args.rubrics!r}")
    code = apply_overlay(args.overlay, args.force_overlay) if "v2" in rubrics else {"source": "image", "files": {}}

    from services.content_generation import brand_fit
    if "model_override" not in inspect.signature(brand_fit.score_brand_fit).parameters:
        raise SystemExit("deployed brand_fit.score_brand_fit has no model_override (AA-801 c) — image too old")
    if "v2" in rubrics and "rubric" not in inspect.signature(brand_fit.score_brand_fit).parameters:
        raise SystemExit("brand_fit.score_brand_fit has no rubric parameter — overlay not applied")

    if args.single_call:
        brand_fit._REPEAT_BAND = (99.0, 99.0)       # read at call time in score_brand_fit -> never repeats
    arms = {a.strip() for a in args.arms.split(",") if a.strip()}
    units = load_units(args.pages, arms, rubrics, args.limit)
    brand = load_brand(args)
    model, route = judge_model(args)
    signals = brand_fit.has_brand_signals(brand)
    rubric_version = None
    if "v2" in rubrics:
        from services.content_generation import brand_fit_v2
        rubric_version = brand_fit_v2.RUBRIC_V2_VERSION
        if brand_fit_v2.fingerprint() != brand_fit_v2.RUBRIC_V2_FINGERPRINT:
            raise SystemExit("brand_fit_v2 fingerprint differs from its pin — the prompt changed without a "
                             "version bump")
    head = {"stage": STAGE, "role": ROLE, "measurement": MEASUREMENT, "judge_model": model, "s1_judge_route": route,
            "rubrics": rubrics, "rubric_v2_version": rubric_version, "code": code,
            "brand_name": brand.get("brand_name"), "brand_has_signals": signals,
            "units_selected": len(units), "pages_selected": len({u["page"]["key"] for u in units}),
            "arms": sorted(arms), "max_cost_usd": args.max_cost_usd, "min_quality": MIN_QUALITY, "tie_band": TIE_BAND,
            "judge_mode": "single_call" if args.single_call else "prod_aa714_near_band_median3"}
    print(f"START units={len(units)} pages={head['pages_selected']} rubrics={','.join(rubrics)} model={model} "
          f"brand_signals={signals} code={code['source']} rubric_v2={rubric_version} cap=${args.max_cost_usd}",
          flush=True)

    if not signals:
        # judge_node skips the judge entirely without brand signals (both rubrics): it never triggers a retry.
        return _emit({**head, "verdict": "judge_skipped_in_prod_no_brand_signals", "rows": [], "by_rubric": {}}, args)
    if args.dry_run:
        samples = {}
        for rubric in rubrics:
            u = next((x for x in units if x["rubric"] == rubric), None)
            if not u:
                continue
            if rubric == "v2":
                from services.content_generation import brand_fit_v2 as v2
                prompt = v2.build_user_prompt(u["page"]["output"], v2.source_sheet(u["page"]["tour"]))
            else:
                prompt = brand_fit._build_judge_prompt({**brand}, u["page"]["output"])
            samples[rubric] = {"key": u["unit_key"], "chars": len(prompt), "prompt": prompt[:6000]}
        return _emit({**head, "dry_run": True, "sample_prompts": samples, "rows": [], "by_rubric": {}}, args)

    done: dict[str, dict] = {}
    if args.resume:
        prev = s3_get(args.s3_out) or {}
        done = {r["key"]: r for r in prev.get("rows", []) if r.get("ok")}
        print(f"RESUME ok_rows={len(done)}", flush=True)

    spend = Spend(args.max_cost_usd)
    rows: list[dict] = list(done.values())
    skipped: list[str] = []
    lock = threading.Lock()
    todo = [u for u in units if u["unit_key"] not in done]

    def work(u: dict, reserved: float) -> None:
        r = score_page(u, brand, model)
        spend.settle(u["rubric"], reserved, float(r.get("cost_usd") or 0.0), int(r.get("calls") or 0))
        with lock:
            rows.append(r)
            n = len(rows)
        print(f"{n:>3}/{len(units)} {r['key']} ok={r['ok']} fit={r.get('brand_fit')} caps={r.get('caps')} "
              f"calls={r.get('calls')} spend=${spend.usd:.4f}", flush=True)
        if n % 10 == 0:
            try:
                s3_put(args.s3_out, json.dumps({**head, "partial": True, "rows": rows}, default=str))
            except Exception as e:  # noqa: BLE001
                print(f"S3_PARTIAL_FAILED {e}", flush=True)

    sem = threading.Semaphore(args.concurrency)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for u in todo:
            sem.acquire()
            reserved = spend.reserve(u["rubric"])
            if reserved is None:
                sem.release()
                skipped.append(u["unit_key"])
                continue
            ctx = contextvars.copy_context()

            def run(u=u, reserved=reserved):
                try:
                    work(u, reserved)
                finally:
                    sem.release()
            pool.submit(ctx.run, run)

    try:
        from shared.llm_client.call_log import wait_for_pending_writes
        wait_for_pending_writes()
    except Exception:  # noqa: BLE001
        pass
    rows.sort(key=lambda r: (r["sample_id"], r["rubric"], r["arm_model"]))
    out = {**head, "spend_usd": round(spend.usd, 4), "calls": dict(spend.calls), "skipped_cost_cap": skipped,
           "rows": rows, "by_rubric": summarise(rows)}
    return _emit(out, args)


def _emit(out: dict, args) -> int:
    body = json.dumps(out, default=str)
    print("BRANDFIT-BEGIN")
    print(body)
    print("BRANDFIT-END", flush=True)
    try:
        s3_put(args.s3_out, body)
        if args.s3_out:
            print(f"S3_WRITTEN {args.s3_out}", flush=True)
    except Exception as e:  # noqa: BLE001 — stdout still carries the result
        print(f"S3_WRITE_FAILED {e}", flush=True)
    print(f"DONE spend=${out.get('spend_usd', 0)} rows={len(out.get('rows', []))} "
          f"skipped={len(out.get('skipped_cost_cap', []))}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
