#!/usr/bin/env python3
"""AA-1080 — re-import the stored T0 brief of given tenants: fill Brand context (system_prompt), default CTA and the
signature words where the active brand version left them empty, and record one unmapped-market request per declared
market without search data. Nothing else of the brand version changes (services/brand_intake/reimport.py).

DRY RUN by default: prints, per tenant, the brief used, what would be filled and what is already set. APPLY=1 writes
one new brand version per tenant that has something to fill (the history keeps the previous one) and the
unmapped-market rows. No LLM, no DataForSEO: $0.

The brief is the newest .docx under brand-identity/<tenant_id>/ in the bronze bucket (the portal upload). When the
task role cannot list it, re-upload the brief in the portal's Brand Identity page instead (the same parser runs).

Run in the CIS api container through the orchestrator's S3-mediated ECS exec helper, from the workspace root:
  apps/AA-CIS-App/scripts/aa1080/brief_reimport.py TENANTS=<slug|uuid>,... [APPLY=1]
Output: one JSON object per tenant, then a SUMMARY line.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

TENANTS_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.slug FROM shared.tenants t
    WHERE t.slug = ANY($1::text[]) OR t.tenant_id::text = ANY($1::text[]) ORDER BY t.slug
"""


def parse_args(argv: list[str]) -> dict:
    out = {"TENANTS": os.environ.get("TENANTS", ""), "APPLY": os.environ.get("APPLY", "0")}
    for a in argv:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k.strip().upper()] = v.strip()
    tenants = [t.strip() for t in out["TENANTS"].split(",") if t.strip()]
    if not tenants:
        raise SystemExit("TENANTS=<slug|uuid>,... is required")
    return {"tenants": tenants, "apply": out["APPLY"] == "1"}


def newest_brief_key(s3, bucket: str, tenant_id: str) -> str | None:
    newest = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"brand-identity/{tenant_id}/"):
        for obj in page.get("Contents", []):
            later = newest is None or obj["LastModified"] > newest["LastModified"]
            if obj["Key"].lower().endswith(".docx") and later:
                newest = obj
    return newest["Key"] if newest else None


async def reimport_tenant(conn, pool, s3, bucket: str, tenant_id: str, slug: str, apply: bool) -> dict:
    from services.brand_intake.brief import parse_brief_docx
    from services.brand_intake.reimport import ACTIVE_SQL, apply_fill, plan_fill
    from services.brand_intake.save import record_unsupported_markets
    from shared.services.tenant_config_service import TenantConfigService

    out: dict = {"tenant": slug}
    active = await conn.fetchrow(ACTIVE_SQL, tenant_id)
    if not active:
        return {**out, "error": "no active brand version"}
    try:
        key = newest_brief_key(s3, bucket, tenant_id)
    except Exception as exc:  # noqa: BLE001
        return {**out, "error": f"cannot list the stored briefs ({type(exc).__name__}); re-upload in the portal"}
    if not key:
        return {**out, "error": "no stored .docx brief"}
    parsed = parse_brief_docx(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    plan = plan_fill(dict(active), parsed["fields"])
    out.update({"brief": key.rsplit("/", 1)[-1], "active_version": active["version"], "fill": plan["fill"],
                "already_set": plan["kept"], "drafted": parsed.get("drafted", []),
                "unmapped_fields": parsed.get("unmapped_fields", [])})
    cfg = await TenantConfigService(conn).get_seo_config(tenant_id)
    from services.seo_intelligence.seed_builder import unsupported_countries
    out["unsupported_markets"] = unsupported_countries(cfg.target_market)
    if apply:
        out["new_version"] = await apply_fill(conn, tenant_id, dict(active), plan, key)
        out["unmapped_recorded"] = await record_unsupported_markets(pool, tenant_id, cfg.target_market or {})
    return out


async def main(argv: list[str]) -> int:
    import asyncpg
    import boto3

    from shared.aws_client.bronze import bronze_bucket
    from shared.secrets import get_database_url

    args = parse_args(argv)
    dsn = get_database_url().strip()
    pool = await asyncpg.create_pool(dsn, ssl="require", min_size=1, max_size=2)
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    filled = 0
    try:
        async with pool.acquire() as conn:
            if not args["apply"]:
                await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            rows = [dict(r) for r in await conn.fetch(TENANTS_SQL, args["tenants"])]
            found = {r["slug"] for r in rows} | {r["tenant_id"] for r in rows}
            missing = [t for t in args["tenants"] if t not in found]
            if missing:
                print(json.dumps({"not_found": missing}))
            for r in rows:
                res = await reimport_tenant(conn, pool, s3, bronze_bucket(), r["tenant_id"], r["slug"], args["apply"])
                filled += bool(res.get("fill"))
                print(json.dumps(res, default=str))
        print("SUMMARY " + json.dumps({"tenants": len(rows), "with_fill": filled, "applied": args["apply"],
                                       "cost_usd": 0}))
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
