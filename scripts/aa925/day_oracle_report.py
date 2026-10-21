#!/usr/bin/env python3
"""AA-925 (Q-2) — run the S1 day-count oracle and the S2 cut signature over every stored A1 tour. Read-only, $0.

Two sets, read with the container's DATABASE_URL (role cis_app: SELECT on the three tables), ssl=require:

  Set A  the published master version of each live tour: gold_aa_internal.published_tours (deleted_at IS NULL)
         → silver_aa_internal.generated_content (gc.id = pt.generated_content_id) → raw_tours (live).
  Set B  the latest platform version of every other live tour (not the published one), by gc.status.

Per row: services/quality/day_oracle.check(source itinerary, duration, written itinerary) and
seo_meta_utils.meta_cut_signature(seo_meta) — the same functions graph.validate_node runs. Output (JSON):
counts per code × set × status × country, the leak count (an oracle code fires while validate's own
ITINERARY_DAY_COUNT_MISMATCH does not), DAY_CHECK_SKIPPED, the cut-signature count, and up to 30 random samples
per code (tour id, day numbers + titles, duration, the finding — no body text). Written to
s3://<bronze>/scripts/out/aa925/<run>/day-oracle.json (bucket name built at runtime) and a local copy, plus a
short stdout summary. The samples are for the private root audit folder; the public PR gets counts only.

One-off ECS task on the current api task definition (the image has no scripts/):

  cd /app && PYTHONPATH=/tmp/aa925 python3 -m scripts.aa925.day_oracle_report --oracle-dir /tmp/aa925

``--oracle-dir`` (before the PR is merged): load the PR's day_parse / prompts / seo_meta_utils /
services/quality/day_oracle from that extracted archive by file path (importlib), registered under their
package names, so the image's older modules are not used. Without it (after the deploy) the image's own
modules run. ``--wilson K N`` prints the Wilson 95% lower bound of a precision read and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import math
import os
import random
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

S3_PREFIX = "scripts/out/aa925"
MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
SAMPLES_PER_CODE = 30
CUT_CODE = "META_CUT_SIGNATURE"
LEAK_EXEMPT = frozenset({"SOURCE_DAY_CONTRADICTION", "DAY_CHECK_SKIPPED"})

_COLS = """
    gc.id::text AS gc_id, gc.tour_id::text AS tour_id, gc.status::text AS status, gc.version_num,
    gc.aa_subtitle, gc.aa_summary, gc.aa_highlights, gc.aa_itineraries, gc.seo_meta,
    rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights, rt.src_itineraries,
    rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions
"""
SET_A_SQL = f"""
    SELECT {_COLS}
    FROM gold_aa_internal.published_tours pt
    JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id AND rt.deleted_at IS NULL
    WHERE pt.deleted_at IS NULL
"""
SET_B_SQL = f"""
    SELECT * FROM (
        SELECT DISTINCT ON (gc.tour_id) {_COLS}
        FROM silver_aa_internal.generated_content gc
        JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id AND rt.deleted_at IS NULL
        WHERE gc.tenant_id = $1::uuid
        ORDER BY gc.tour_id, gc.version_num DESC
    ) latest
    WHERE latest.gc_id NOT IN (SELECT pt.generated_content_id::text FROM gold_aa_internal.published_tours pt
                               WHERE pt.deleted_at IS NULL)
"""


def wilson_lower(k: int, n: int, z: float = 1.96) -> Optional[float]:
    """Wilson score interval lower bound for k successes in n (D5 reports it next to the precision)."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((centre - margin) / denom, 4)


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_detectors(oracle_dir: Optional[str]):
    """(day_oracle, day_parse, prompts, seo_meta_utils, itinerary_utils) — from the archive when given."""
    import services.content_generation  # noqa: F401 — the parent package of the modules loaded below

    if oracle_dir:
        base = os.path.abspath(oracle_dir)
        cg = os.path.join(base, "services", "content_generation")
        # order matters: prompts and day_oracle import day_parse by its package name
        day_parse = _load("services.content_generation.day_parse", os.path.join(cg, "day_parse.py"))
        prompts = _load("services.content_generation.prompts", os.path.join(cg, "prompts.py"))
        seo = _load("services.content_generation.seo_meta_utils", os.path.join(cg, "seo_meta_utils.py"))
        if "services.quality" not in sys.modules:
            _load("services.quality", os.path.join(base, "services", "quality", "__init__.py"))
        oracle = _load("services.quality.day_oracle", os.path.join(base, "services", "quality", "day_oracle.py"))
        print(f"detectors loaded from {base}", flush=True)
    else:
        from services.content_generation import day_parse, prompts
        from services.content_generation import seo_meta_utils as seo
        from services.quality import day_oracle as oracle
    from services.content_generation import itinerary_utils
    return oracle, day_parse, prompts, seo, itinerary_utils


def mismatch_fires(prompts, itinerary_utils, row: dict) -> bool:
    """graph.validate_node's ITINERARY_DAY_COUNT_MISMATCH: a source day absent from the output (skipped on
    the splitter's fallback) — recomputed with the same functions on the stored text."""
    parsed = prompts.parse_source_day_word_counts(row.get("src_itineraries") or "", row.get("duration") or "")
    if parsed["used_fallback"]:
        return False
    out = set(itinerary_utils.generated_day_word_counts(row.get("aa_itineraries") or ""))
    return bool(set(parsed["day_word_counts"]) - out)


def evaluate(rows: list[dict], set_name: str, detectors, *, seed: int = 925) -> dict:
    oracle, day_parse, prompts, seo, itinerary_utils = detectors
    counts: Counter = Counter()
    by_country: dict[str, Counter] = defaultdict(Counter)
    by_status: dict[str, Counter] = defaultdict(Counter)
    leak: Counter = Counter()
    hits: dict[str, list[dict]] = defaultdict(list)
    tours_with_any = 0
    no_output_days = 0
    for row in rows:
        findings = oracle.check(row.get("src_itineraries") or "", row.get("duration"), row.get("aa_itineraries") or "")
        codes = {f["code"] for f in findings}
        cut = seo.meta_cut_signature(row.get("seo_meta") or "")
        if cut:
            codes.add(CUT_CODE)
            findings = findings + [{"code": CUT_CODE, "class_id": "struct.truncated", "detail": cut}]
        days = day_parse.output_days(row.get("aa_itineraries") or "")
        if not days:
            no_output_days += 1
        if codes - {CUT_CODE, "DAY_CHECK_SKIPPED"}:
            tours_with_any += 1
        mm = mismatch_fires(prompts, itinerary_utils, row) if codes else False
        for f in findings:
            code = f["code"]
            counts[code] += 1
            by_country[row.get("country") or "?"][code] += 1
            by_status[row.get("status") or "?"][code] += 1
            if code.startswith("DAY_") and code not in LEAK_EXEMPT and not mm:
                leak[code] += 1
            hits[code].append({
                "tour_id": row["tour_id"], "gc_id": row["gc_id"], "status": row.get("status"),
                "country": row.get("country"), "duration": row.get("duration"), "detail": f.get("detail"),
                "output_days": [[n, t] for n, t, _s in days][:40],
                "source_markers": [[a, b] for a, b, _i in day_parse.source_day_markers(row.get("src_itineraries")
                                                                                         or "")][:40],
                "seo_meta_ending": (row.get("seo_meta") or "")[-60:] if code == CUT_CODE else None,
                "itinerary_day_count_mismatch": mm,
            })
    rng = random.Random(seed)
    samples = {code: (lst if len(lst) <= SAMPLES_PER_CODE else rng.sample(lst, SAMPLES_PER_CODE))
               for code, lst in hits.items()}
    return {
        "set": set_name, "rows": len(rows), "tours_with_a_day_finding": tours_with_any,
        "no_output_days": no_output_days, "counts": dict(counts),
        "leak_vs_itinerary_day_count_mismatch": dict(leak),
        "by_status": {k: dict(v) for k, v in by_status.items()},
        "by_country": {k: dict(v) for k, v in sorted(by_country.items())},
        "samples": samples, "hits_total": {k: len(v) for k, v in hits.items()},
    }


async def fetch(sql: str, *args) -> list[dict]:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def summary_lines(report: dict) -> list[str]:
    lines = []
    for part in report["sets"]:
        lines.append(f"{part['set']}: {part['rows']} rows, {part['tours_with_a_day_finding']} with a day finding, "
                     f"{part['no_output_days']} with no canonical day blocks")
        for code, n in sorted(part["counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {code:28} {n:5}  leak(no ITINERARY_DAY_COUNT_MISMATCH)="
                         f"{part['leak_vs_itinerary_day_count_mismatch'].get(code, 0)}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--oracle-dir", help="extracted archive holding the PR's detector modules (before merge)")
    ap.add_argument("--sets", default="A,B", help="A, B or A,B")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--wilson", nargs=2, type=int, metavar=("K", "N"))
    a = ap.parse_args(argv)
    if a.wilson:
        k, n = a.wilson
        print({"k": k, "n": n, "precision": round(k / n, 4) if n else None, "wilson_lower_95": wilson_lower(k, n)})
        return 0
    detectors = load_detectors(a.oracle_dir)
    run_id = str(uuid.uuid4())
    parts = []
    wanted = {s.strip().upper() for s in a.sets.split(",")}
    if "A" in wanted:
        parts.append(evaluate(asyncio.run(fetch(SET_A_SQL)), "A_published", detectors))
    if "B" in wanted:
        parts.append(evaluate(asyncio.run(fetch(SET_B_SQL, MASTER_TENANT_ID)), "B_latest_unpublished", detectors))
    report: dict[str, Any] = {
        "schema": "aa925-day-oracle", "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
        "detectors_from": "archive" if a.oracle_dir else "image",
        "modes": dict(detectors[0].DAY_ORACLE_MODES), "sets": parts,
    }
    from scripts.aa729.common import save_output

    path = save_output(report, f"{S3_PREFIX}/{run_id}/day-oracle.json", local_dir="/tmp/aa925-out",
                       upload=not a.no_upload)
    print("\n".join(summary_lines(report)), flush=True)
    print(f"run_id={run_id} local={path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
