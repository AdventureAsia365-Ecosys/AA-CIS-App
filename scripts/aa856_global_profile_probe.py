#!/usr/bin/env python3
"""AA-856 — pre-apply probe and post-apply check for migration 238 (Cohere Embed v4 on acc2 moves to
`global.cohere.embed-v4:0`). Runs INSIDE the CIS `api` container (ECS exec + S3 pattern), so the
Bedrock call uses the ECS task role and the gateway's own acc2 client — the same path `f10_embed`
and `a3_place_embed` take after 238. A CLI call with the admin profile does not prove that.

Why it exists: CIS embed failures are silent. `f10_embed` (content_embedding.compute_embeddings)
logs a warning and leaves the vector NULL; `a3_place_embed` raises and the job retries. Neither
writes a failed `shared.llm_call_log` row (only TripPlanner `tp_search_embed` does), so a refused
global profile would show up only as MISSING success rows and failed jobs — this script looks for
exactly that.

Modes (one per run; the result JSON is also written to S3, because long ECS exec stdout is cut):

  preflight            (default, DRY-RUN) read only: caller identity, the catalog row and the
                       routes on cohere-embed-v4, shared.job queued/running by kind. No Bedrock call,
                       no write.
  preflight --invoke   the same, plus ONE Bedrock embed of one short text with
                       modelId=global.cohere.embed-v4:0 through shared.llm_client.embed._call
                       (acc2 client, task role, no retry) and ONE shared.llm_call_log row
                       (stage 'adhoc_aa856', role 'embed'). Cost about $0.000001.
  post-apply --since <ISO-8601 UTC>
                       read only: the catalog row, llm_call_log successes / failures per embed stage
                       since the timestamp, and shared.job rows of embedding kinds that failed or
                       retried since then with an error that names Bedrock/Cohere/AccessDenied.

Run (each line one command; see skill/aa-cis-schema/references/ecs-exec.md):
  aws s3 cp scripts/aa856_global_profile_probe.py s3://aa-cis-bronze-005097885195/scripts/aa856_probe.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://aa-cis-bronze-005097885195/scripts/aa856_probe.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && cd /app && python3 /tmp/s.py preflight --invoke'" --profile aa365-admin --region us-west-1
  aws s3 cp s3://aa-cis-bronze-005097885195/scripts/aa856-probe-preflight.json .tmp-session/aa856-probe-preflight.json --profile aa365-admin --region us-west-1

Gate: apply 238 only when preflight --invoke reports "gate": {"apply_238": true} — the global
profile answered through the task role and shared.job has nothing queued/running. `f10_embed` is
called from many job kinds and API routes (angle gate, slate, segment matching, content writing,
export...), so the gate is "no job in flight", the same rule as a backend deploy.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

GLOBAL_PROFILE = "global.cohere.embed-v4:0"
US_PROFILE = "us.cohere.embed-v4:0"
MODEL_KEY = "cohere-embed-v4"
EMBED_STAGES = ("f10_embed", "a3_place_embed", "tp_search_embed", "tp_component_embed")
ADHOC_STAGE = "adhoc_aa856"
PROBE_DIMENSIONS = 1536
S3_BUCKET = "aa-cis-bronze-005097885195"
REGION = "us-west-1"

CATALOG_SQL = """
SELECT model_key, bedrock_profile_ids, enabled, blocked_reason, updated_at, updated_by
  FROM shared.llm_model_catalog
 WHERE model_key = $1
"""
ROUTES_SQL = """
SELECT stage, role, model_id, fallback_model_ids, account_route, is_active
  FROM shared.llm_role_config
 WHERE model_id = $1 OR $1 = ANY (fallback_model_ids)
 ORDER BY stage
"""
JOBS_IN_FLIGHT_SQL = """
SELECT kind, status, count(*) AS jobs
  FROM shared.job
 WHERE status IN ('queued', 'running')
 GROUP BY kind, status
 ORDER BY kind, status
"""
CALLS_SINCE_SQL = """
SELECT stage, model, account,
       count(*) FILTER (WHERE coalesce(quality_signal ->> 'failed', 'false') <> 'true') AS ok_rows,
       count(*) FILTER (WHERE quality_signal ->> 'failed' = 'true') AS failed_rows,
       min(created_at) AS first_at, max(created_at) AS last_at
  FROM shared.llm_call_log
 WHERE created_at >= $1 AND stage = ANY ($2::text[])
 GROUP BY stage, model, account
 ORDER BY stage, model, account
"""
FAILED_ERRORS_SQL = """
SELECT stage, left(quality_signal ->> 'error', 300) AS error, count(*) AS rows
  FROM shared.llm_call_log
 WHERE created_at >= $1 AND stage = ANY ($2::text[]) AND quality_signal ->> 'failed' = 'true'
 GROUP BY 1, 2
 ORDER BY rows DESC
 LIMIT 20
"""
JOB_ERRORS_SQL = """
SELECT id, kind, status, attempt, updated_at, left(error, 300) AS error
  FROM shared.job
 WHERE updated_at >= $1 AND error IS NOT NULL
   AND (error ILIKE '%cohere%' OR error ILIKE '%embed%' OR error ILIKE '%AccessDenied%'
        OR error ILIKE '%bedrock%' OR error ILIKE '%All models in the route failed%')
 ORDER BY updated_at DESC
 LIMIT 50
"""


async def _connect():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.connect(host=u.hostname, port=u.port or 5432, user=u.username,
                                 password=u.password, database=u.path.lstrip("/"), ssl="require")


def _rows(rows) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, str) and k == "bedrock_profile_ids":
                d[k] = json.loads(v)
        out.append(d)
    return out


def _identity() -> dict[str, Any]:
    import boto3

    ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    return {"account": ident["Account"], "arn": ident["Arn"]}


def _invoke_global() -> dict[str, Any]:
    """One embed on the global profile through the gateway's own call path (acc2, no retry)."""
    from shared.llm_client import embed as embed_mod
    from shared.llm_client.catalog import CatalogModel

    model = CatalogModel(
        model_key=MODEL_KEY, label="Cohere Embed v4 (AA-856 probe)", vendor="cohere",
        provider="bedrock", api_style="embed", bedrock_profile_ids={"acc2": GLOBAL_PROFILE},
        callable_via=("embed",), price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True,
    )
    t0 = time.monotonic()
    try:
        resp = embed_mod._call(model, "acc2", ["Hanoi Old Quarter street food walk"],
                               "search_query", PROBE_DIMENSIONS)
    except Exception as e:  # noqa: BLE001 — the probe reports every refusal, it never raises
        return {"ok": False, "model_id": GLOBAL_PROFILE, "error_type": type(e).__name__,
                "error": str(e)[:500], "ms": round((time.monotonic() - t0) * 1000)}
    return {"ok": True, "model_id": GLOBAL_PROFILE, "dimensions": len(resp.vectors[0]),
            "input_tokens": resp.input_tokens, "tokens_estimated": resp.tokens_estimated,
            "cost_usd": resp.cost_usd, "ms": round((time.monotonic() - t0) * 1000), "_resp": resp}


async def _log_probe(result: dict[str, Any]) -> None:
    from shared.llm_client.call_log import record_call

    resp = result.pop("_resp")
    await record_call(
        stage=ADHOC_STAGE, role="embed", model=resp.model_used, tokens_in=resp.input_tokens,
        tokens_out=0, cost_usd=resp.cost_usd,
        quality_signal={"app": "cis", "issue": "AA-856", "probe": "global_profile",
                        "model_id": GLOBAL_PROFILE, "texts": 1,
                        "dimensions": len(resp.vectors[0]),
                        "tokens_estimated": resp.tokens_estimated},
        account=resp.account, fallback_used=False, provider="bedrock-native",
    )


async def preflight(invoke: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "preflight", "invoke_requested": invoke}
    out["identity"] = _identity()
    conn = await _connect()
    try:
        out["catalog"] = _rows(await conn.fetch(CATALOG_SQL, MODEL_KEY))
        out["routes"] = _rows(await conn.fetch(ROUTES_SQL, MODEL_KEY))
        jobs = _rows(await conn.fetch(JOBS_IN_FLIGHT_SQL))
    finally:
        await conn.close()
    out["jobs_in_flight"] = jobs
    if invoke:
        result = await asyncio.to_thread(_invoke_global)
        if result.get("ok"):
            await _log_probe(result)
        out["invoke"] = result
    profile = ((out["catalog"] or [{}])[0].get("bedrock_profile_ids") or {}).get("acc2")
    out["gate"] = {
        "catalog_acc2_profile": profile,
        "catalog_profile_expected": profile in (US_PROFILE, GLOBAL_PROFILE),
        "global_invoke_ok": bool(out.get("invoke", {}).get("ok")) if invoke else None,
        "no_jobs_in_flight": not jobs,
    }
    gate = out["gate"]
    gate["apply_238"] = bool(invoke and gate["catalog_profile_expected"] and gate["global_invoke_ok"]
                             and gate["no_jobs_in_flight"])
    return out


async def post_apply(since: datetime.datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "post-apply", "since": since.isoformat()}
    stages = [*EMBED_STAGES, ADHOC_STAGE]
    conn = await _connect()
    try:
        out["catalog"] = _rows(await conn.fetch(CATALOG_SQL, MODEL_KEY))
        out["calls"] = _rows(await conn.fetch(CALLS_SINCE_SQL, since, stages))
        out["failed_call_errors"] = _rows(await conn.fetch(FAILED_ERRORS_SQL, since, stages))
        out["job_errors"] = _rows(await conn.fetch(JOB_ERRORS_SQL, since))
        out["jobs_in_flight"] = _rows(await conn.fetch(JOBS_IN_FLIGHT_SQL))
    finally:
        await conn.close()
    ok_by_stage: dict[str, int] = {}
    for r in out["calls"]:
        ok_by_stage[r["stage"]] = ok_by_stage.get(r["stage"], 0) + int(r["ok_rows"])
    out["ok_rows_by_stage"] = ok_by_stage
    out["verdict"] = {
        "catalog_on_global": ((out["catalog"] or [{}])[0].get("bedrock_profile_ids") or {}).get("acc2")
        == GLOBAL_PROFILE,
        # A throttle is the 20 rpm quota (expected until L-7089DC7D rises), not a refusal of the
        # profile; AccessDenied / ValidationException / model-access errors are.
        "no_refused_embed_rows": not [r for r in out["failed_call_errors"]
                                      if "throttl" not in (r["error"] or "").lower()
                                      and "too many" not in (r["error"] or "").lower()],
        "no_embed_job_errors": not out["job_errors"],
        # CIS failures write no row: a CIS stage with 0 ok rows since the apply proves nothing
        # either way — run one small place_extract / content job and check again.
        "cis_stages_with_ok_rows": sorted(s for s in ("f10_embed", "a3_place_embed")
                                          if ok_by_stage.get(s)),
    }
    return out


def _write(result: dict[str, Any], name: str) -> None:
    body = json.dumps(result, indent=2, default=str)
    print(body, flush=True)
    path = f"/tmp/{name}"
    with open(path, "w") as f:
        f.write(body)
    try:
        import boto3

        boto3.client("s3", region_name=REGION).upload_file(path, S3_BUCKET, f"scripts/{name}")
        print(f"uploaded s3://{S3_BUCKET}/scripts/{name}", flush=True)
    except Exception as e:  # noqa: BLE001 — local run without S3 access still prints the result
        print(f"S3 upload skipped: {e}", flush=True)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("mode", nargs="?", default="preflight", choices=("preflight", "post-apply"))
    p.add_argument("--invoke", action="store_true", help="preflight: make the one global-profile embed")
    p.add_argument("--since", help="post-apply: ISO-8601 UTC time DB Migrate applied 238")
    a = p.parse_args(argv)
    if a.mode == "preflight":
        _write(asyncio.run(preflight(a.invoke)), "aa856-probe-preflight.json")
        return
    if not a.since:
        p.error("post-apply needs --since <ISO-8601 UTC>")
    since = datetime.datetime.fromisoformat(a.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.timezone.utc)
    _write(asyncio.run(post_apply(since)), "aa856-probe-post-apply.json")


if __name__ == "__main__":
    main(sys.argv[1:])
