#!/usr/bin/env python3
"""AA-1047 W1 — run the REPORT-mode final contract over every stored S1 Master and list the publish-flip set.
Read-only, $0, no LLM. Root ADR 0020 W1 proof: "the REPORT-mode final_contract over stored v2 Masters lists Japan
12->4 and Rajasthan 15->10 in its publish-flip list".

Set: the live Masters (``gold_aa_internal.published_tours``, ``deleted_at IS NULL``, ``master_status = 'active'``)
with their published version (``generated_content``, its latest ``quality_scores`` row, ``raw_tours``) — the S1
writer v2 ones (``metadata.writer_version = 'v2'``) unless ``--all-writers``. Each row runs
``services.quality.final_contract_store.s1_record_from_row`` (the exact contract every exit runs, with the tenant's
active brand forbidden words, AA-1049 S277) with the current status ``published``. A flip = the contract's verdict
is not ``publish`` (the page would move to review / policy_hold / source_hold / degraded once that code is enforced).

Output: JSON on stdout — aggregates and ids only, never page or source text: counts per verdict, per blocking code
among the flips, per country; the flip list (published tour id, tour id, version id, country, verdict, block codes,
the day counts of a day-structure finding); the day-loss cases (source days vs page days). ``--sample N`` keeps N
flips per code in the list (default: all).

Run in the api container (S3-mediated ECS exec, container ``DATABASE_URL`` = role cis_app, ssl=require):

  cd /app && python3 /tmp/final_contract_report.py --sample 5

(``cd /app`` puts the image's ``services`` package on the path; the JSON goes to the exec session's stdout.)

The image ships services/quality/final_contract*.py from the AA-1047 deploy (no scripts/ dir: copy this file in).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any

sys.path.insert(0, os.getcwd())
if os.path.isdir("/app") and "/app" not in sys.path:
    sys.path.insert(0, "/app")

WRITER_V2 = "v2"
_DAYS_RE = re.compile(r"(\d+) output day\(s\) for a (\d+)-day duration")


def masters_sql(all_writers: bool) -> str:
    from services.quality.final_contract_store import S1_ROW_COLUMNS, S1_ROW_FROM
    where = "" if all_writers else "AND gc.metadata->>'writer_version' = 'v2'"
    return (f"SELECT pt.id::text AS published_tour_id, {S1_ROW_COLUMNS} {S1_ROW_FROM}"
            " JOIN gold_aa_internal.published_tours pt ON pt.generated_content_id = gc.id"
            f" WHERE pt.deleted_at IS NULL AND pt.master_status = 'active' {where}"
            " ORDER BY rt.country NULLS LAST, gc.tour_id")


def _block_codes(record: dict) -> list[str]:
    return [f["code"] for f in record.get("findings") or [] if f.get("severity") == "block"]


def _day_counts(record: dict) -> dict:
    for f in record.get("findings") or []:
        m = _DAYS_RE.search(f.get("detail") or "")
        if f.get("code") == "DAY_COUNT_VS_DURATION" and m:
            return {"page_days": int(m.group(1)), "duration_days": int(m.group(2))}
    return {}


def summarize(rows: list[Any], *, sample: int = 0, brand_words: dict | None = None) -> dict:
    """Aggregates + the flip list of the contract over ``rows`` (``masters_sql`` rows). Pure apart from the
    contract's own imports. ``brand_words`` = ``final_contract_store.brand_words_by_key`` over the same rows (the
    tenant's active brand forbidden words per ``brand_key``); none = the AA core list alone."""
    from services.quality.final_contract_store import brand_key, s1_record_from_row

    verdicts: Counter = Counter()
    flip_codes: Counter = Counter()
    warn_codes: Counter = Counter()
    by_country: dict[str, Counter] = defaultdict(Counter)
    flips: list[dict] = []
    day_loss: list[dict] = []
    errors = 0
    per_code_kept: Counter = Counter()
    for row in rows:
        country = row["country"] or "unknown"
        try:
            rec = s1_record_from_row(row, "report", actual="published",
                                     brand_forbidden_words=(brand_words or {}).get(brand_key(row)))
        except Exception as exc:  # noqa: BLE001 — one bad row is counted, the report goes on
            errors += 1
            print(f"row {row['generated_content_id']}: {type(exc).__name__}", file=sys.stderr)
            continue
        verdicts[rec["verdict"]] += 1
        by_country[country]["masters"] += 1
        for f in rec.get("findings") or []:
            if f.get("severity") == "warn":
                warn_codes[f["code"]] += 1
        if rec.get("flip") != "unpublish":
            continue
        codes = _block_codes(rec)
        by_country[country]["flips"] += 1
        by_country[country][f"verdict:{rec['verdict']}"] += 1
        flip_codes.update(set(codes))
        days = _day_counts(rec)
        if days and days["page_days"] < days["duration_days"]:
            day_loss.append({"tour_id": row["tour_id"], "country": country, **days})
        if sample and all(per_code_kept[c] >= sample for c in codes):
            continue
        per_code_kept.update(codes)
        flips.append({"published_tour_id": row["published_tour_id"], "tour_id": row["tour_id"],
                      "version_id": row["generated_content_id"], "country": country, "verdict": rec["verdict"],
                      "block_codes": codes, **days})
    total = sum(verdicts.values())
    flip_total = sum(c["flips"] for c in by_country.values())
    return {
        "schema": "aa1047-final-contract-report/1", "contract_version": _contract_version(),
        "masters": total, "errors": errors, "verdicts": dict(verdicts),
        "publish_flip": {"count": flip_total, "share": round(flip_total / total, 4) if total else None,
                         "by_code": dict(flip_codes.most_common())},
        "warn_codes": dict(warn_codes.most_common()),
        "by_country": {k: dict(v) for k, v in sorted(by_country.items())},
        "day_loss": sorted(day_loss, key=lambda d: (d["country"], -d["duration_days"])),
        "flips": flips,
    }


def _contract_version() -> str:
    from services.quality.final_contract import CONTRACT_VERSION
    return CONTRACT_VERSION


async def _fetch(all_writers: bool) -> tuple[list, dict]:
    import asyncpg

    from services.quality.final_contract_store import brand_words_by_key
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(masters_sql(all_writers))
            return rows, await brand_words_by_key(conn, rows)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all-writers", action="store_true", help="every live Master, not only the S1 v2 ones")
    ap.add_argument("--sample", type=int, default=0, help="keep at most N flips per blocking code in the list")
    args = ap.parse_args(argv)
    rows, brand_words = asyncio.run(_fetch(args.all_writers))
    print(json.dumps(summarize(rows, sample=args.sample, brand_words=brand_words), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
