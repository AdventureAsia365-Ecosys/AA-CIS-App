#!/usr/bin/env python3
"""AA-982 — read-only, $0 check that T9 blog writers no longer get place_paa questions A3 Jev rejected.

For each blog piece written after the deploy (`--pieces id,id,…`, or the latest `--latest N` blog pieces
that carry `writer_input.a3_question_filter`) it reads the admin record and re-checks it against the
stored A3 verdicts with the deployed code (services/acp_shared/real_questions.py):

  - `a3_question_filter` is present (the filter ran) and its `place_paa_kept` equals the place_paa
    questions in `writer_input.real_questions` (the chain the writer's list was cut from);
  - no place_paa question in `real_questions` or in `search_questions` (what the writer prompt got)
    is rejected under the AA-982 rule, recomputed from the scope the write used
    (`a3_countries`, `a3_segment_ids`);
  - the tours covered (Done-when: >= 3 tours).

No LLM, no Jev call, no write: SELECTs only (`default_transaction_read_only`), through the container's
DATABASE_URL (`cis_app`). Needs the deployed image that carries AA-982.

Run inside the CIS api container (ECS exec + S3 pattern, one line per command):
  aws s3 cp scripts/aa982_place_paa_check.py s3://aa-cis-bronze-<acc2>/scripts/aa982_check.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://aa-cis-bronze-<acc2>/scripts/aa982_check.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && cd /app && python3 /tmp/s.py --latest 5'" --profile aa365-admin --region us-west-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.getcwd())

MIN_TOURS = 3

_PIECES_SQL = """
    SELECT p.piece_id, p.status, p.created_at, p.writer_input, r.atom_id,
           (SELECT ta.tour_id FROM acp_contract.tour_atoms ta WHERE ta.atom_id = r.atom_id LIMIT 1) AS tour_id
    FROM acp_shared.content_piece p
    JOIN acp_shared.angle_gate_request r ON r.request_id = p.angle_gate_request_id
    WHERE p.channel = 'blog' AND p.status IN ('approved', 'held')
      AND (cardinality($1::uuid[]) > 0 AND p.piece_id = ANY($1::uuid[])
           OR cardinality($1::uuid[]) = 0 AND p.writer_input ? 'a3_question_filter')
    ORDER BY p.created_at DESC
    LIMIT $2
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def check_piece(conn, row, rq) -> dict:
    wi = _json(row["writer_input"]) or {}
    record = wi.get("a3_question_filter")
    chain_place = [q.get("q") for q in wi.get("real_questions") or []
                   if isinstance(q, dict) and q.get("source") == rq.SOURCE_PLACE and q.get("q")]
    writer_place = [q.get("question") for q in wi.get("search_questions") or []
                    if isinstance(q, dict) and q.get("source") == rq.SOURCE_PLACE and q.get("question")]
    out: dict[str, Any] = {"piece_id": str(row["piece_id"]), "status": row["status"],
                           "tour_id": str(row["tour_id"]) if row["tour_id"] else None,
                           "created_at": row["created_at"].isoformat(), "filter_record": record,
                           "place_paa_in_chain": len(chain_place), "place_paa_to_writer": len(writer_place)}
    if not isinstance(record, dict):
        out["ok"] = False
        out["why"] = "no a3_question_filter record (written before the deploy, or switch off)"
        return out
    segments = await rq.load_a3_segments(conn, record.get("a3_segment_ids") or [])
    countries = record.get("a3_countries") or []
    questions = list(dict.fromkeys(chain_place + writer_place))
    keys = rq.a3_subject_keys(questions, rq.a3_scopes(countries, segments), rq.a3_moments(segments))
    verdicts = await rq.platform_verdicts(conn, rq.A3_QUESTION_KEYS, keys) if keys else {}
    rejected = rq.a3_rejections(questions, countries, segments, verdicts)
    out.update({
        "rejected_in_chain": [q for q in chain_place if q in rejected],
        "rejected_to_writer": [q for q in writer_place if q in rejected],
        "kept_count_matches": record.get("place_paa_kept") == len(chain_place),
        "dropped": sum((record.get("a3_dropped") or {}).values()),
    })
    out["ok"] = (not out["rejected_in_chain"] and not out["rejected_to_writer"] and out["kept_count_matches"]
                 and "a3_error" not in record)
    return out


async def main(pieces: list[str], latest: int) -> dict:
    import asyncpg

    from services.acp_shared import real_questions as rq

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        rows = await conn.fetch(_PIECES_SQL, pieces, max(latest, len(pieces)) or 5)
        results = [await check_piece(conn, r, rq) for r in rows]
    finally:
        await conn.close()
    tours = {r["tour_id"] for r in results if r.get("ok") and r.get("tour_id")}
    return {"issue": "AA-982", "pieces": results, "pieces_ok": sum(1 for r in results if r.get("ok")),
            "pieces_checked": len(results), "tours_ok": len(tours),
            "pass": bool(results) and all(r.get("ok") for r in results) and len(tours) >= MIN_TOURS}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AA-982 place_paa A3 filter check (read-only, $0)")
    parser.add_argument("--pieces", default="", help="comma-separated content_piece ids")
    parser.add_argument("--latest", type=int, default=5, help="latest N blog pieces with the filter record")
    args = parser.parse_args()
    ids = [p.strip() for p in args.pieces.split(",") if p.strip()]
    print(json.dumps(asyncio.run(main(ids, args.latest)), ensure_ascii=False, indent=1, default=str))
