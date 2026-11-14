# api/routers/admin_pipeline/metrics.py — pipeline metrics routes: metrics, SEO, library, spot workers, infra
# status.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import datetime
import json
import os
import boto3 as _boto3
from fastapi import Header, Request
from api.core.error_ref import log_exception_ref, public_error
from api.routers.admin import verify_admin_secret

from api.routers.admin_pipeline._shared import _SCORE_HISTOGRAM_CASE, logger, router


# ── GET /admin/metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_pipeline_metrics(
    request: Request,
    days: int = 7,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    tenant_slug = "aa_internal"

    async with pool.acquire() as conn:
        daily = await conn.fetch("""
            SELECT
                DATE(started_at)              AS day,
                COUNT(*)                      AS runs,
                COALESCE(SUM(tours_total),0)  AS tours,
                COALESCE(SUM(tours_passed),0) AS passed,
                COALESCE(SUM(tours_hitl),0)   AS hitl,
                COALESCE(SUM(tours_failed),0) AS failed,
                COALESCE(ROUND(SUM(cost_usd)::numeric,4), 0) AS cost
            FROM shared.pipeline_runs
            WHERE started_at >= NOW() - ($1 || ' days')::interval
              AND status != 'ingesting'
            GROUP BY DATE(started_at)
            ORDER BY day ASC
        """, str(days))

        # AA-702: model usage now comes from shared.llm_call_log (real calls + real cost, every
        # tenant and stage, same source as External Spend). The old version counted
        # generated_content rows and priced them from pipeline_runs.cost_usd, which only the
        # legacy S1 batch path writes — it showed one model at $0.0000 while the log had $40+.
        model_usage_rows = await conn.fetch("""
            SELECT model,
                   COUNT(*)                          AS calls,
                   COALESCE(SUM(cost_usd), 0)::float AS total_cost
            FROM shared.llm_call_log
            WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY model
            ORDER BY total_cost DESC, calls DESC
        """)

        avg_cost_per_run = await conn.fetchval("""
            SELECT ROUND(SUM(cost_usd) / NULLIF(COUNT(*), 0)::numeric, 6)
            FROM shared.pipeline_runs
            WHERE cost_usd > 0 AND status != 'ingesting'
        """)

        published_count = await conn.fetchval("""
            SELECT COUNT(*) FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
        """)

        tenant_rewrite_count = await conn.fetchval(
            "SELECT COUNT(*) FROM gold_aa_internal.tenant_tour_versions"
        )

        tenant_breakdown = await conn.fetch("""
            SELECT t.slug, t.plan_tier, COUNT(ttv.id) AS rewrite_count
            FROM gold_aa_internal.tenant_tour_versions ttv
            JOIN shared.tenants t ON t.tenant_id = ttv.tenant_id
            GROUP BY t.slug, t.plan_tier ORDER BY rewrite_count DESC
        """)

        daily_rewrites = await conn.fetch("""
            SELECT DATE(created_at) AS day, COUNT(*) AS rewrites
            FROM gold_aa_internal.tenant_tour_versions
            WHERE created_at >= NOW() - ($1 || ' days')::interval
            GROUP BY DATE(created_at)
        """, str(days))

        # AA-476: same "rows, not real calls" caveat as the `models` query above — kept the
        # response key name (llm_calls) to avoid a wider API contract change; the frontend
        # displays it as "Content Versions", not "LLM Calls".
        llm_calls = await conn.fetchval(
            f"SELECT COUNT(*) FROM silver_{tenant_slug}.generated_content"
        )

        last_run = await conn.fetchrow("""
            SELECT tours_total, tours_passed, tours_failed, started_at, completed_at,
                   EXTRACT(EPOCH FROM (completed_at - started_at)) AS duration_sec
            FROM shared.pipeline_runs
            WHERE completed_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
        """)

        health_rows = await conn.fetch("""
            SELECT endpoint,
                   COUNT(*)                                             AS calls,
                   ROUND(AVG(response_ms)::numeric, 0)                 AS avg_ms,
                   SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS errors,
                   SUM(CASE WHEN status_code = 429  THEN 1 ELSE 0 END) AS rate_limited,
                   MAX(called_at)                                       AS last_call
            FROM shared.tenant_api_usage
            WHERE called_at >= NOW() - INTERVAL '1 hour'
            GROUP BY endpoint ORDER BY calls DESC
        """)

    ENDPOINT_SERVICE_MAP = {
        "/v1/pipeline/run":         "Step Functions Pipeline",
        "/v1/pipeline/run-tour":    "Content Generation",
        "/admin/run-tour":          "Content Generation",
        "/v1/pipeline/review-queue": "Validation Lambda",
        "/v1/tours":                "Export / Catalog API",
        "/v1/pipeline/upload-url":  "Ingestion Lambda",
        "/admin/upload-url":        "Ingestion Lambda",
        "/admin/metrics":           "Admin Metrics API",
        "/v1/pipeline/sources":     "Source Tracker",
        "/health":                  "API Health Check",
    }

    import re as _re
    pipeline_health = []
    seen_services: set = set()
    for r in health_rows:
        ep = r["endpoint"]
        ep_norm = _re.sub(r'/[0-9a-f-]{8,}', '/{id}', ep)
        service = ENDPOINT_SERVICE_MAP.get(ep, ENDPOINT_SERVICE_MAP.get(ep_norm, ep))
        if service in seen_services:
            continue
        seen_services.add(service)
        errors = int(r["errors"] or 0)
        calls  = int(r["calls"] or 0)
        avg_ms = float(r["avg_ms"] or 0)
        status = "healthy" if errors == 0 else ("degraded" if errors / max(calls, 1) < 0.1 else "down")
        pipeline_health.append({"name": service, "status": status,
                                 "latency": f"{avg_ms:.0f}ms", "errors": errors, "calls": calls})

    CORE_SERVICES = ["Ingestion Lambda", "Step Functions Pipeline",
                     "Content Generation", "Validation Lambda", "Export / Catalog API"]
    for svc in CORE_SERVICES:
        if svc not in seen_services:
            pipeline_health.append({"name": svc, "status": "idle",
                                    "latency": "—", "errors": 0, "calls": 0})

    model_usage = [
        {
            "model":         r["model"],
            "calls":         int(r["calls"]),
            "total_cost":    round(float(r["total_cost"]), 4),
            "cost_per_call": round(float(r["total_cost"]) / int(r["calls"]), 6) if r["calls"] else 0.0,
        }
        for r in model_usage_rows
    ]

    rewrite_by_day = {str(r["day"]): int(r["rewrites"]) for r in daily_rewrites}
    return {
        "daily_runs": [
            {
                "date":     str(r["day"]),
                "runs":     r["runs"],
                "tours":    r["tours"],
                "passed":   r["passed"],
                "hitl":     r["hitl"],
                "failed":   r["failed"],
                "cost":     float(r["cost"]),
                "rewrites": rewrite_by_day.get(str(r["day"]), 0),
            }
            for r in daily
        ],
        "model_usage": model_usage,
        "avg_cost_per_run": float(avg_cost_per_run) if avg_cost_per_run else 0.0,
        "last_run": {
            "tours_total":  last_run["tours_total"]  if last_run else 0,
            "tours_passed": last_run["tours_passed"] if last_run else 0,
            "tours_failed": last_run["tours_failed"] if last_run else 0,
            "duration_sec": float(last_run["duration_sec"]) if last_run and last_run["duration_sec"] else 0,
        } if last_run else None,
        "pipeline_health": pipeline_health,
        "published_count":       int(published_count or 0),
        "tenant_rewrite_count":  int(tenant_rewrite_count or 0),
        "llm_calls":             int(llm_calls or 0),
        "content_summary": {
            "total_published_master":    int(published_count or 0),
            "total_tenant_rewrites":     int(tenant_rewrite_count or 0),
            "total_content_all_tenants": int(published_count or 0) + int(tenant_rewrite_count or 0),
            "tenant_breakdown": [
                {"slug": r["slug"], "plan_tier": r["plan_tier"], "rewrite_count": int(r["rewrite_count"])}
                for r in tenant_breakdown
            ],
        },
    }


# ── GET /admin/metrics/seo ────────────────────────────────────────────────────

@router.get("/metrics/seo")
async def get_seo_metrics(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # AA-702: keywords with a real DataForSEO search volume, across published masters. The old
        # list took the first items of top_keywords, which for S1 is the tour-name seed itself
        # (AA-251) — so it listed tour names with count 1.
        top_keywords = await conn.fetch("""
            SELECT lower(ki->>'keyword')                   AS keyword,
                   MAX((ki->>'search_volume')::bigint)    AS search_volume,
                   COUNT(DISTINCT sc.tour_id)              AS tours
            FROM silver_aa_internal.seo_context sc
            JOIN gold_aa_internal.published_tours pt
              ON pt.tour_id = sc.tour_id AND pt.master_status <> 'trashed'
             AND pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(sc.keyword_ideas) = 'array' THEN sc.keyword_ideas ELSE '[]'::jsonb END
            ) ki
            WHERE (ki->>'search_volume') ~ '^[0-9]+$' AND (ki->>'search_volume')::bigint > 0
            GROUP BY 1
            ORDER BY search_volume DESC, tours DESC
            LIMIT 15
        """)
        total_tours = await conn.fetchval(
            "SELECT COUNT(*) FROM gold_aa_internal.published_tours "
            "WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid "
            "AND master_status <> 'trashed'"
        )
        seo_covered = await conn.fetchval("""
            SELECT COUNT(DISTINCT pt.tour_id)
            FROM gold_aa_internal.published_tours pt
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status <> 'trashed'
              AND EXISTS (
                  SELECT 1 FROM silver_aa_internal.seo_context sc WHERE sc.tour_id = rt.tour_id
              )
        """)
        # AA-702: published masters with SEO data per country (was: every seo_context row for any
        # raw tour, which summed to 256 against 119 published).
        countries = await conn.fetch("""
            SELECT rt.country, COUNT(DISTINCT pt.tour_id) AS count
            FROM gold_aa_internal.published_tours pt
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status <> 'trashed'
              AND rt.country IS NOT NULL
              AND EXISTS (SELECT 1 FROM silver_aa_internal.seo_context sc WHERE sc.tour_id = pt.tour_id)
            GROUP BY rt.country ORDER BY count DESC
        """)

    redis = request.app.state.redis
    cache_stats: dict = {"hit_rate": "N/A", "keys": 0}
    try:
        info = await redis.info("stats")
        hits   = int(info.get("keyspace_hits", 0))
        misses = int(info.get("keyspace_misses", 0))
        total  = hits + misses
        cache_stats["hit_rate"] = f"{round(hits/total*100, 1)}%" if total > 0 else "0%"
        cache_stats["hits"]     = hits
        cache_stats["misses"]   = misses
        db_info = await redis.info("keyspace")
        cache_stats["keys"] = sum(
            int(v.split(",")[0].split("=")[1])
            for v in db_info.values() if isinstance(v, str) and "keys=" in v
        )
    except Exception:
        pass

    return {
        "total_tours":  total_tours,
        "seo_covered":  seo_covered,
        "coverage_pct": round(seo_covered / total_tours * 100, 1) if total_tours else 0,
        "countries":    [{"country": dict(r)["country"], "count": dict(r)["count"]} for r in countries],
        "top_keywords": [
            {"keyword": r["keyword"], "search_volume": int(r["search_volume"]), "tours": int(r["tours"])}
            for r in top_keywords
        ],
        "cache":        cache_stats,
    }


# ── GET /admin/metrics/library ────────────────────────────────────────────────

@router.get("/metrics/library")
async def get_library_metrics(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        by_country = await conn.fetch("""
            SELECT rt.country,
                   COUNT(pt.id)                            AS total,
                   ROUND(AVG(pt.quality_score)::numeric,2) AS avg_score,
                   MAX(pt.updated_at)                      AS last_published
            FROM gold_aa_internal.published_tours pt
            LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
            WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND pt.master_status <> 'trashed'
            GROUP BY rt.country ORDER BY total DESC
        """)
        # AA-645 d06 (migration 273): published_at is a tour's FIRST publish and stays put on a rewrite; updated_at
        # moves on every publish / promote. "Published in 30 days", "stale" and "last published" are about the last
        # change (updated_at); first_published_last_30d counts tours new to Master.
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*)                                  AS total,
                ROUND(AVG(quality_score)::numeric, 2)     AS avg_score,
                COUNT(CASE WHEN updated_at >= NOW() - INTERVAL '30 days' THEN 1 END) AS published_last_30d,
                COUNT(CASE WHEN published_at >= NOW() - INTERVAL '30 days' THEN 1 END) AS first_published_last_30d,
                COUNT(CASE WHEN updated_at < NOW() - INTERVAL '180 days' THEN 1 END) AS stale_count
            FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
        """)
        score_dist = await conn.fetch(f"""
            SELECT
                {_SCORE_HISTOGRAM_CASE} AS range,
                COUNT(*) AS count
            FROM gold_aa_internal.published_tours
            WHERE tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND master_status <> 'trashed'
            GROUP BY range ORDER BY range DESC
        """)

    return {
        "total":              stats["total"],
        "avg_score":          float(stats["avg_score"] or 0),
        "published_last_30d": stats["published_last_30d"],              # published or re-published (updated_at)
        "first_published_last_30d": stats["first_published_last_30d"],  # new to Master (published_at)
        "stale_count":        stats["stale_count"],                     # not re-published in 180 days
        "by_country":         [dict(r) for r in by_country],
        "score_distribution": [dict(r) for r in score_dist],
    }


# ── GET /admin/metrics/spot-workers ──────────────────────────────────────────

@router.get("/metrics/spot-workers")
async def get_spot_workers(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    ecs = _boto3.client("ecs", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    cluster = os.environ.get("ECS_CLUSTER", "aa-cis-dev-cluster")
    try:
        task_arns = ecs.list_tasks(cluster=cluster, desiredStatus="RUNNING").get("taskArns", [])
        spot_tasks, on_demand_tasks = [], []
        if task_arns:
            for t in ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", []):
                cap  = t.get("capacityProviderName", "FARGATE")
                info = {
                    "task_id":  t["taskArn"].split("/")[-1][:12],
                    "status":   t.get("lastStatus", "UNKNOWN"),
                    "cpu":      t.get("cpu", "256"),
                    "memory":   t.get("memory", "512"),
                    "started":  str(t.get("startedAt", "")),
                    "capacity": cap,
                }
                (spot_tasks if cap == "FARGATE_SPOT" else on_demand_tasks).append(info)
        spot_count  = len(spot_tasks)
        total_count = len(task_arns)
        return {
            "total_tasks":     total_count,
            "spot_tasks":      spot_count,
            "on_demand_tasks": len(on_demand_tasks),
            "spot_pct":        round(spot_count / total_count * 100) if total_count else 0,
            "saving_per_hr":   round(spot_count * 0.04048 * 0.7, 4),
            "tasks":           spot_tasks + on_demand_tasks,
        }
    except Exception:
        # AA-912: boto's AccessDenied text names the role ARN / account id — server log only
        ref = log_exception_ref("admin_spot_workers_failed", cluster=cluster)
        return {"total_tasks": 0, "spot_tasks": 0, "on_demand_tasks": 0,
                "spot_pct": 0, "saving_per_hr": 0, "tasks": [], "error": public_error("ECS task list", ref)}


# ── GET /admin/infra/status ───────────────────────────────────────────────────
# AA-257 — live infra state (ECS/RDS/NAT/Redis), cached 60s in Redis to avoid
# re-hitting 5 describe-type AWS APIs on every page load/tab.
#
# STEP0 finding: aa-cis-dev-ecs-task-role (the role this endpoint runs under)
# does NOT currently have ecs:Describe*/ecs:ListTasks, rds:DescribeDBInstances,
# ec2:DescribeInstances, elasticache:DescribeCacheClusters, or ecr:DescribeImages
# — confirmed via a real call to the pre-existing GET /admin/metrics/spot-workers
# above, which has been silently returning AccessDeniedException in prod this
# whole time (degrades to zeros, per its own try/except). Same gap here.
# Per-check try/except below mirrors that same graceful-degradation shape —
# each section reports {"error": "..."} on AccessDenied rather than 500ing the
# whole endpoint, so this ships real and useful the moment the IAM grant lands
# (a Terraform PR, opened not auto-merged — see AA-257 Linear comment) instead
# of waiting on it. Also fixed a stale hardcoded value from the issue text: the
# NAT instance ID it names (i-04ebd090e97184f45) no longer exists — the real
# current one (i-05363515303ef7c21, tag Name=aa-cis-dev-nat-instance) is the
# default here, override-able via NAT_INSTANCE_ID like the other resource IDs.
_INFRA_STATUS_CACHE_KEY = "admin:infra_status:v1"
_INFRA_STATUS_CACHE_TTL_S = 60  # technical-constant: in-process cache refresh of a read


@router.get("/infra/status")
async def get_infra_status(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)

    redis = request.app.state.redis
    try:
        cached = await redis.get(_INFRA_STATUS_CACHE_KEY)
        if cached:
            data = json.loads(cached)
            data["cache_hit"] = True
            return data
    except Exception as e:
        logger.warning("infra_status_redis_read_error", error=str(e))

    region        = os.environ.get("AWS_REGION", "us-west-1")
    cluster       = os.environ.get("ECS_CLUSTER", "aa-cis-dev-cluster")
    service       = os.environ.get("ECS_SERVICE", "aa-cis-dev-api")
    ecr_repo      = os.environ.get("ECR_REPO", "aa-cis-dev-api")
    db_id         = os.environ.get("RDS_INSTANCE_ID", "aa-cis-dev-db")
    nat_id        = os.environ.get("NAT_INSTANCE_ID", "i-05363515303ef7c21")
    redis_id      = os.environ.get("REDIS_CLUSTER_ID", "aa-cis-dev-redis")

    result = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cache_hit":  False,
    }

    # ── ECS (service state + running-task digest vs ECR :latest) ──
    try:
        ecs = _boto3.client("ecs", region_name=region)
        svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
        ecs_info = {
            "status":        svc["status"],
            "desired_count": svc["desiredCount"],
            "running_count": svc["runningCount"],
            "pending_count": svc["pendingCount"],
        }
        task_arns = ecs.list_tasks(cluster=cluster, serviceName=service,
                                    desiredStatus="RUNNING").get("taskArns", [])
        running_digest = None
        if task_arns:
            tasks = ecs.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", [])
            if tasks and tasks[0].get("containers"):
                running_digest = tasks[0]["containers"][0].get("imageDigest")
        ecs_info["running_digest"] = running_digest

        try:
            ecr = _boto3.client("ecr", region_name=region)
            latest = ecr.describe_images(
                repositoryName=ecr_repo, imageIds=[{"imageTag": "latest"}]
            )["imageDetails"][0]
            ecs_info["latest_digest"] = latest["imageDigest"]
            ecs_info["digest_matches_latest"] = (
                running_digest == latest["imageDigest"] if running_digest else None
            )
        except Exception:
            # AA-912: every error below goes to the server log under a ref; the response (also cached
            # in Redis for 60 s) carries only generic text — boto errors name the role ARN / account id
            ecs_info["ecr_error"] = public_error("ECR describe", log_exception_ref("infra_status_ecr_failed"))

        result["ecs"] = ecs_info
    except Exception:
        result["ecs"] = {"error": public_error("ECS describe", log_exception_ref("infra_status_ecs_failed"))}

    # ── RDS ──
    try:
        rds = _boto3.client("rds", region_name=region)
        db = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        result["rds"] = {
            "status":         db["DBInstanceStatus"],
            "engine":         db["Engine"],
            "instance_class": db["DBInstanceClass"],
        }
    except Exception:
        result["rds"] = {"error": public_error("RDS describe", log_exception_ref("infra_status_rds_failed"))}

    # ── NAT instance ──
    try:
        ec2 = _boto3.client("ec2", region_name=region)
        inst = ec2.describe_instances(InstanceIds=[nat_id])["Reservations"][0]["Instances"][0]
        result["nat"] = {"instance_id": nat_id, "state": inst["State"]["Name"]}
    except Exception:
        result["nat"] = {"instance_id": nat_id,
                         "error": public_error("EC2 describe", log_exception_ref("infra_status_nat_failed"))}

    # ── ElastiCache Redis ──
    try:
        ec = _boto3.client("elasticache", region_name=region)
        cache = ec.describe_cache_clusters(CacheClusterId=redis_id)["CacheClusters"][0]
        result["redis"] = {"status": cache["CacheClusterStatus"], "node_type": cache["CacheNodeType"]}
    except Exception:
        result["redis"] = {"error": public_error("ElastiCache describe",
                                                 log_exception_ref("infra_status_redis_failed"))}

    # AA-257: "AWS đang chạy — nhớ tắt cuối session" badge — true whenever ECS
    # has a nonzero desired count or RDS is available, regardless of which
    # describe calls succeeded (defaults to True/unknown-but-assume-running on
    # error, since a false "all stopped" reading is the worse mistake here).
    ecs_desired = result.get("ecs", {}).get("desired_count")
    rds_status  = result.get("rds", {}).get("status")
    result["running_reminder"] = (
        ecs_desired is None or ecs_desired > 0 or rds_status is None or rds_status == "available"
    )

    try:
        await redis.set(_INFRA_STATUS_CACHE_KEY, json.dumps(result), ex=_INFRA_STATUS_CACHE_TTL_S)
    except Exception as e:
        logger.warning("infra_status_redis_write_error", error=str(e))

    return result
