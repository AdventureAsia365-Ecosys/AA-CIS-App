#!/usr/bin/env python3
"""AA-779 — read-only, $0 coverage check for the real-question chain (Done-when line 1).

For every blog Subject that a tenant can still write (`acp_shared.subject`, channel 'blog', state
'proposed' or 'picked') it builds the piece scope the way T9 `start_write` does (the Route's kept
moments, the SCOPE line, the trip's other places), reads the real-question chain
(services/acp_shared/real_questions.py: Segment matches, place PAA, then the tour's PAA), keeps the
in-scope ones with the same filter the t9_write job uses (`quality_gates.paa_for_scope`, without the
Facts block — so this is a lower bound: Facts only make more questions answerable) and reports:

  - share of blog Subjects with >= 3 in-scope real questions (all sources, and Segment/place only),
  - questions per source (candidates / kept),
  - the Subjects still below 3, with what each source offered (the list B/C top-ups would target).

No LLM, no DataForSEO, no write: SELECTs only, through the container's DATABASE_URL (`cis_app`).
Needs the deployed image that carries AA-779 2A (it imports the app's own chain and filter).

Run inside the CIS api container (ECS exec + S3 pattern, one line per command; see
skill/aa-cis-schema/references/ecs-exec.md):
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa779_question_coverage.py s3://$BRONZE/scripts/aa779_cov.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa779_cov.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && cd /app && python3 /tmp/s.py'" --profile aa365-admin --region us-west-1
  aws s3 cp s3://$BRONZE/scripts/aa779-question-coverage.json .tmp-session/aa779-question-coverage.json --profile aa365-admin --region us-west-1
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.getcwd())

REGION = os.environ.get("AWS_REGION", "us-west-1")
MIN_QUESTIONS = 3
TARGET_SHARE = 0.80
REAL_DB = ("segment_match", "place_paa")

_SUBJECTS_SQL = """
    SELECT s.subject_id, s.tenant_id, s.state, r.route_id, r.tour_id, r.hub_name, r.ordered_segment_ids
    FROM acp_shared.subject s
    JOIN acp_contract.route r ON r.route_id = s.route_id
    WHERE s.channel = 'blog' AND s.state IN ('proposed', 'picked')
    ORDER BY s.created_at
"""


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=4)


async def _one(pool, row) -> dict[str, Any]:
    from services.acp_content_writing import hygiene
    from services.acp_content_writing.quality_gates import paa_for_scope
    from services.acp_content_writing.service import _fetch_route_segments, _scope_line, _trip_places_outside
    from services.acp_shared.dfs_relevance import fetch_search_demand_signal
    from services.acp_shared.real_questions import collect_real_questions, label_questions

    segment_ids = row["ordered_segment_ids"]
    if isinstance(segment_ids, str):
        segment_ids = json.loads(segment_ids)
    out: dict[str, Any] = {"subject_id": str(row["subject_id"]), "route": row["hub_name"], "state": row["state"]}
    atoms: list = []
    try:
        segments = await _fetch_route_segments(row["tenant_id"], str(row["tour_id"]), segment_ids, pool,
                                               atoms_out=atoms)
    except Exception as e:  # noqa: BLE001 — a Route rebuilt away: report, keep going
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return out
    atom_text = "\n\n".join(t for _, t in segments)
    scope_atoms = [a for a, _ in atoms]
    scope = await _scope_line(scope_atoms, pool)
    trip_places = await _trip_places_outside(row["tenant_id"], str(row["tour_id"]), scope_atoms, pool)
    lexicon = hygiene.build_scope_lexicon(atom_text, scope, None, trip_places=tuple(trip_places))
    signal = await fetch_search_demand_signal(row["tour_id"], pool)
    chain = await collect_real_questions(pool, atom_ids=scope_atoms,
                                         tour_paa=signal.people_also_ask[:12] if signal else [])
    candidates = [c.as_dict() for c in chain]
    kept = label_questions(paa_for_scope([c["q"] for c in candidates], lexicon.own_text, None, lexicon.gazetteer),
                           candidates)
    out.update({
        "candidates": {s: sum(1 for c in candidates if c["source"] == s) for s in {c["source"] for c in candidates}},
        "kept": [k["question"] + f"  [{k['source']}]" for k in kept],
        "kept_n": len(kept),
        "kept_db_n": sum(1 for k in kept if k["source"] in REAL_DB),
    })
    return out


async def main() -> dict[str, Any]:
    pool = await _pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SUBJECTS_SQL)
        results = [await _one(pool, r) for r in rows]
    finally:
        await pool.close()
    measured = [r for r in results if "error" not in r]
    covered = [r for r in measured if r["kept_n"] >= MIN_QUESTIONS]
    covered_db = [r for r in measured if r["kept_db_n"] >= MIN_QUESTIONS]
    per_source: dict[str, dict[str, int]] = {}
    for r in measured:
        for s, n in r["candidates"].items():
            per_source.setdefault(s, {"candidates": 0, "kept": 0})["candidates"] += n
        for k in r["kept"]:
            s = k.rsplit("[", 1)[-1].rstrip("]")
            per_source.setdefault(s, {"candidates": 0, "kept": 0})["kept"] += 1
    share = len(covered) / len(measured) if measured else 0.0
    return {
        "blog_subjects": len(results), "measured": len(measured), "errors": len(results) - len(measured),
        "share_with_3_in_scope": round(share, 3),
        "share_with_3_in_scope_segment_or_place_only": round(len(covered_db) / len(measured), 3) if measured else 0.0,
        "done_when_line_1_met_by_A": share >= TARGET_SHARE,
        "per_source": per_source,
        "below_3": [r for r in measured if r["kept_n"] < MIN_QUESTIONS],
        "errored": [r for r in results if "error" in r],
        "note": "lower bound: no Facts block (Facts only make more questions answerable)",
    }


def _write(result: dict[str, Any], name: str = "aa779-question-coverage.json") -> None:
    body = json.dumps(result, indent=2, default=str, ensure_ascii=False)
    summary = {k: v for k, v in result.items() if k not in ("below_3", "errored")}
    print(json.dumps(summary, indent=2, default=str), flush=True)
    path = f"/tmp/{name}"
    with open(path, "w") as f:
        f.write(body)
    try:
        import boto3

        bucket = os.environ.get("BRONZE_BUCKET", "").strip() or (
            "aa-cis-bronze-" + boto3.client("sts", region_name=REGION).get_caller_identity()["Account"])
        boto3.client("s3", region_name=REGION).upload_file(path, bucket, f"scripts/{name}")
        print(f"uploaded s3://{bucket}/scripts/{name}", flush=True)
    except Exception as e:  # noqa: BLE001 — a local run without S3 access still prints the summary
        print(f"S3 upload skipped: {e}", flush=True)


if __name__ == "__main__":
    _write(asyncio.run(main()))
