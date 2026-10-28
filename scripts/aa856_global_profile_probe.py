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
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa856_global_profile_probe.py s3://$BRONZE/scripts/aa856_probe.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa856_probe.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/s.py && cd /app && python3 /tmp/s.py preflight --invoke'" --profile aa365-admin --region us-west-1
  aws s3 cp s3://$BRONZE/scripts/aa856-probe-preflight.json .tmp-session/aa856-probe-preflight.json --profile aa365-admin --region us-west-1

Gate: apply 238 only when preflight --invoke reports "gate": {"apply_238": true} — the global
profile answered through the task role and shared.job has nothing queued/running. `f10_embed` is
called from many job kinds and API routes (angle gate, slate, segment matching, content writing,
export...), so the gate is "no job in flight", the same rule as a backend deploy.

AA-862 (migration 240, embed routes fall back acc2 -> acc3): `--account acc3` makes
`preflight --invoke` call that SATELLITE account instead, through the gateway's satellite client
(STS AssumeRole into the account's invoker role, ExternalId, the same path the route fallback takes),
plus one acc2 call of the same text to compare. It checks 1536 floats and cosine >= 0.999 between the
two vectors (same model, so stored vectors stay comparable). Two calls, two llm_call_log rows (stage
'adhoc_aa862'), about $0.000002. Result: s3://.../scripts/aa862-probe-preflight-<account>.json.
  ... python3 /tmp/s.py preflight --invoke --account acc3
Gate: apply 240 only when it reports "gate": {"apply_240": true} for acc3. An acc2 throttle (20 rpm)
leaves the cosine unproven (gate false): run it again a minute later. 240 only adds a fallback, so
jobs in flight are reported but do not block it. AA-998: acc3 is the only satellite account.
`post-apply --since` also reads the acc3 row and counts successes per account.
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
# AA-862 — migration 240: one catalog row per satellite account, same model and profile.
SATELLITE_KEYS = {"acc3": "cohere-embed-v4-acc3"}  # AA-998: acc3 is the only satellite account
ALL_MODEL_KEYS = [MODEL_KEY, *SATELLITE_KEYS.values()]
SATELLITE_ADHOC_STAGE = "adhoc_aa862"
SAME_MODEL_MIN_COSINE = 0.999
PROBE_TEXT = "Hanoi Old Quarter street food walk"
EMBED_STAGES = ("f10_embed", "a3_place_embed", "tp_search_embed", "tp_component_embed")
ADHOC_STAGE = "adhoc_aa856"
PROBE_DIMENSIONS = 1536
REGION = "us-west-1"

CATALOG_SQL = """
SELECT model_key, bedrock_profile_ids, enabled, blocked_reason, updated_at, updated_by
  FROM shared.llm_model_catalog
 WHERE model_key = ANY ($1::text[])
 ORDER BY model_key
"""
ROUTES_SQL = """
SELECT stage, role, model_id, fallback_model_ids, account_route, is_active
  FROM shared.llm_role_config
 WHERE model_id = ANY ($1::text[]) OR fallback_model_ids && $1::text[]
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


def _invoke_global(account: str = "acc2") -> dict[str, Any]:
    """One embed on the global profile through the gateway's own call path: the acc2 client (task
    role, no retry), or for acc3 the satellite client (AssumeRole into the invoker role)."""
    from shared.llm_client import embed as embed_mod
    from shared.llm_client.catalog import CatalogModel

    model = CatalogModel(
        model_key=SATELLITE_KEYS.get(account, MODEL_KEY),
        label=f"Cohere Embed v4 ({'AA-862' if account != 'acc2' else 'AA-856'} probe)", vendor="cohere",
        provider="bedrock", api_style="embed", bedrock_profile_ids={account: GLOBAL_PROFILE},
        callable_via=("embed",), price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True,
    )
    t0 = time.monotonic()
    try:
        resp = embed_mod._call(model, account, [PROBE_TEXT], "search_query", PROBE_DIMENSIONS)
    except Exception as e:  # noqa: BLE001 — the probe reports every refusal, it never raises
        return {"ok": False, "account": account, "model_id": GLOBAL_PROFILE,
                "error_type": type(e).__name__, "error": str(e)[:500],
                "ms": round((time.monotonic() - t0) * 1000)}
    return {"ok": True, "account": account, "model_id": GLOBAL_PROFILE,
            "dimensions": len(resp.vectors[0]),
            "input_tokens": resp.input_tokens, "tokens_estimated": resp.tokens_estimated,
            "cost_usd": resp.cost_usd, "ms": round((time.monotonic() - t0) * 1000), "_resp": resp}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def _log_probe(result: dict[str, Any], issue: str = "AA-856") -> None:
    from shared.llm_client.call_log import record_call

    resp = result.pop("_resp")
    await record_call(
        stage=ADHOC_STAGE if issue == "AA-856" else SATELLITE_ADHOC_STAGE, role="embed",
        model=resp.model_used, tokens_in=resp.input_tokens, tokens_out=0, cost_usd=resp.cost_usd,
        quality_signal={"app": "cis", "issue": issue,
                        "probe": "global_profile" if resp.account == "acc2" else "satellite_profile",
                        "model_id": GLOBAL_PROFILE, "texts": 1,
                        "dimensions": len(resp.vectors[0]),
                        "tokens_estimated": resp.tokens_estimated},
        account=resp.account, fallback_used=False,
        provider="bedrock-native" if resp.account == "acc2" else "bedrock-satellite",
    )


async def _satellite_invoke(account: str) -> dict[str, Any]:
    """AA-862 — one embed on `account` (satellite) and one on acc2 of the same text; the two vectors
    must be the same model's (cosine >= SAME_MODEL_MIN_COSINE)."""
    satellite = await asyncio.to_thread(_invoke_global, account)
    acc2 = await asyncio.to_thread(_invoke_global, "acc2")
    cosine = None
    if satellite.get("ok") and acc2.get("ok"):
        cosine = round(_cosine(satellite["_resp"].vectors[0], acc2["_resp"].vectors[0]), 6)
    for r in (satellite, acc2):
        if r.get("ok"):
            await _log_probe(r, issue="AA-862")
    return {"satellite": satellite, "acc2_reference": acc2, "cosine_vs_acc2": cosine}


def _catalog_profile(catalog: list[dict[str, Any]], key: str, account: str) -> Any:
    row = next((r for r in catalog if r["model_key"] == key), {})
    return (row.get("bedrock_profile_ids") or {}).get(account)


async def preflight(invoke: bool, account: str = "acc2") -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "preflight", "invoke_requested": invoke, "account": account}
    out["identity"] = _identity()
    conn = await _connect()
    try:
        out["catalog"] = _rows(await conn.fetch(CATALOG_SQL, ALL_MODEL_KEYS))
        out["routes"] = _rows(await conn.fetch(ROUTES_SQL, ALL_MODEL_KEYS))
        jobs = _rows(await conn.fetch(JOBS_IN_FLIGHT_SQL))
    finally:
        await conn.close()
    out["jobs_in_flight"] = jobs
    if account != "acc2":
        if invoke:
            out["invoke"] = await _satellite_invoke(account)
        sat = out.get("invoke", {}).get("satellite", {})
        cosine = out.get("invoke", {}).get("cosine_vs_acc2")
        out["gate"] = {
            "account": account,
            "satellite_invoke_ok": bool(sat.get("ok")) if invoke else None,
            "dimensions_ok": sat.get("dimensions") == PROBE_DIMENSIONS if invoke else None,
            "cosine_vs_acc2": cosine,
            "same_model": cosine is not None and cosine >= SAME_MODEL_MIN_COSINE,
            # Reported only: 240 adds a fallback, a failing satellite fails the call as it did before.
            "no_jobs_in_flight": not jobs,
        }
        g = out["gate"]
        g["apply_240"] = bool(invoke and g["satellite_invoke_ok"] and g["dimensions_ok"] and g["same_model"])
        return out
    if invoke:
        result = await asyncio.to_thread(_invoke_global)
        if result.get("ok"):
            await _log_probe(result)
        out["invoke"] = result
    profile = _catalog_profile(out["catalog"], MODEL_KEY, "acc2")
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
    stages = [*EMBED_STAGES, ADHOC_STAGE, SATELLITE_ADHOC_STAGE]
    conn = await _connect()
    try:
        out["catalog"] = _rows(await conn.fetch(CATALOG_SQL, ALL_MODEL_KEYS))
        out["routes"] = _rows(await conn.fetch(ROUTES_SQL, ALL_MODEL_KEYS))
        out["calls"] = _rows(await conn.fetch(CALLS_SINCE_SQL, since, stages))
        out["failed_call_errors"] = _rows(await conn.fetch(FAILED_ERRORS_SQL, since, stages))
        out["job_errors"] = _rows(await conn.fetch(JOB_ERRORS_SQL, since))
        out["jobs_in_flight"] = _rows(await conn.fetch(JOBS_IN_FLIGHT_SQL))
    finally:
        await conn.close()
    ok_by_stage: dict[str, int] = {}
    ok_by_stage_account: dict[str, dict[str, int]] = {}
    for r in out["calls"]:
        ok_by_stage[r["stage"]] = ok_by_stage.get(r["stage"], 0) + int(r["ok_rows"])
        per = ok_by_stage_account.setdefault(r["stage"], {})
        per[r["account"] or "?"] = per.get(r["account"] or "?", 0) + int(r["ok_rows"])
    out["ok_rows_by_stage"] = ok_by_stage
    # AA-862: after 240 an acc2 throttle shows as a failed acc2 row (TripPlanner only) followed by an
    # ok acc3 row of model cohere-embed-v4-acc3.
    out["ok_rows_by_stage_account"] = ok_by_stage_account
    out["verdict"] = {
        "catalog_on_global": _catalog_profile(out["catalog"], MODEL_KEY, "acc2") == GLOBAL_PROFILE,
        "stages_served_by_acc3": sorted(s for s, per in ok_by_stage_account.items()
                                        if s in EMBED_STAGES and per.get("acc3")),
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


def _s3_bucket() -> str:
    """The bronze bucket: BRONZE_BUCKET of the api container this runs in, else aa-cis-bronze-<caller
    account> (AA-902: no account ID in this public repo; same rule as shared/aws_client/bronze.py,
    inlined because the script is fetched to /tmp and may not import the app)."""
    import boto3

    name = os.environ.get("BRONZE_BUCKET", "").strip()
    return name or "aa-cis-bronze-" + boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def _write(result: dict[str, Any], name: str) -> None:
    body = json.dumps(result, indent=2, default=str)
    print(body, flush=True)
    path = f"/tmp/{name}"
    with open(path, "w") as f:
        f.write(body)
    try:
        import boto3

        bucket = _s3_bucket()
        boto3.client("s3", region_name=REGION).upload_file(path, bucket, f"scripts/{name}")
        print(f"uploaded s3://{bucket}/scripts/{name}", flush=True)
    except Exception as e:  # noqa: BLE001 — local run without S3 access still prints the result
        print(f"S3 upload skipped: {e}", flush=True)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("mode", nargs="?", default="preflight", choices=("preflight", "post-apply"))
    p.add_argument("--invoke", action="store_true", help="preflight: make the one global-profile embed")
    p.add_argument("--since", help="post-apply: ISO-8601 UTC time DB Migrate applied 238 / 240")
    p.add_argument("--account", default="acc2", choices=("acc2", "acc3"),
                   help="preflight: AA-862 — probe this satellite account (plus an acc2 reference call)")
    a = p.parse_args(argv)
    if a.mode == "preflight":
        name = ("aa856-probe-preflight.json" if a.account == "acc2"
                else f"aa862-probe-preflight-{a.account}.json")
        _write(asyncio.run(preflight(a.invoke, a.account)), name)
        return
    if not a.since:
        p.error("post-apply needs --since <ISO-8601 UTC>")
    since = datetime.datetime.fromisoformat(a.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=datetime.timezone.utc)
    _write(asyncio.run(post_apply(since)), "aa856-probe-post-apply.json")


if __name__ == "__main__":
    main(sys.argv[1:])
