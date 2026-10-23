"""AA-839 — `data_erasure` job kind: DRY RUN (default) or approved apply of a traveller or tenant erasure.

Payload (POST /admin/job-runner/erasure/run builds it; never an e-mail address):
  {"subject": "tenant", "tenant": "<uuid>", "mode": "dry_run"}
  {"subject": "traveller", "email_sha256": "<64 hex>", "mode": "dry_run"}
  {"subject": "traveller", "email_sha256": "<64 hex>", "mode": "dry_run", "verify": true}  (after an apply: the
      job strips the hash from its own row when done, so the verification leaves no copy behind)
  apply adds: "dry_run_job_id", "approved_fingerprint", "approved_by", "snapshot_id", "actor"

DRY RUN (SELECT only, one repeatable-read read-only transaction): rows per CIS table and action, blockers, the
delete order, S3 objects and secrets of a tenant (best-effort), the per-app checklist and a `fingerprint` of the
subject and the counts (plan.fingerprint v2: the tenant id, or the address hash with a per-DRY-RUN salt kept in
`result.subject_salt` until the hash is stripped). A DRY RUN after an apply is the independent verification
(every count 0).

Apply (destructive-ops.md: DRY RUN -> approval -> snapshot -> transaction -> verify) runs only when ALL hold:
  - env ERASURE_APPLY_ENABLED is "true" (unset = off; it is off on every environment today),
  - dry_run_job_id is a succeeded data_erasure DRY RUN of the same subject, at most ERASURE_DRY_RUN_MAX_AGE_HOURS
    old, whose fingerprint (v2, bound to the subject) equals approved_fingerprint (what Nghiệp approved),
  - approved_by and snapshot_id (the manual RDS snapshot) are given,
  - tenant: offboarded (cancelled_at) at least ERASURE_TENANT_RETENTION_DAYS days ago, never the AA tenant,
  - inside the apply transaction: the recount gives the same fingerprint and no blocker; every statement touches
    exactly the approved rows; a recount finds 0. Otherwise it raises and nothing is committed.
It writes one acp_shared.audit_log row (counts only) and, for a traveller, strips the hash from every
data_erasure job row that holds it.

Single use, no retry (AA-839 review): an apply claims its DRY RUN (`result.used_by_apply`) before it starts, so
neither a second apply nor a retried one can reuse that approval; POST /jobs/{id}/retry refuses an apply
(`retry_refusal`). A traveller job that ends without success has its hash replaced by a marker (`on_terminal`),
and Sentry events of this kind carry no frame locals (shared/sentry_init.py SCRUB_LOCALS_KINDS).
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.erasure import inventory as inv
from services.erasure import plan as pl
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "data_erasure"
SUBJECTS = ("tenant", "traveller")
MODES = ("dry_run", "apply")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SALT_RE = re.compile(r"^[0-9a-f]{32}$")

APPLY_FLAG = "ERASURE_APPLY_ENABLED"
# Business values not set yet (Nghiệp sets the tenant retention window, AA-839): safe defaults, env-overridable.
DEFAULT_TENANT_RETENTION_DAYS = 30
DEFAULT_DRY_RUN_MAX_AGE_HOURS = 24
STATEMENT_TIMEOUT = "120s"
LOCK_TIMEOUT = "10s"

TENANT_ERASED = AdminTenantAuditAction.ERASED
DSAR_ERASED = "dsar.cis_erased"
STRIPPED = "stripped-after-apply"
STRIPPED_VERIFY = "stripped-after-verify"

# What a caller without tenant.read sees of a data_erasure job on the job-runner API (AA-816 M1 mechanism).
RESULT_CAPABILITY = "tenant.read"
_REDACTED_RESULT_KEYS = ("subject", "mode", "fingerprint", "totals", "blocked")


def apply_enabled(env: Optional[dict] = None) -> bool:
    return (env if env is not None else os.environ).get(APPLY_FLAG, "").strip().lower() == "true"


def _int_env(name: str, default: int, env: Optional[dict] = None) -> int:
    raw = (env if env is not None else os.environ).get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def tenant_retention_days(env: Optional[dict] = None) -> int:
    return max(0, _int_env("ERASURE_TENANT_RETENTION_DAYS", DEFAULT_TENANT_RETENTION_DAYS, env))


def dry_run_max_age(env: Optional[dict] = None) -> timedelta:
    return timedelta(hours=max(1, _int_env("ERASURE_DRY_RUN_MAX_AGE_HOURS", DEFAULT_DRY_RUN_MAX_AGE_HOURS, env)))


def subject_key(payload: dict, salt: Optional[str]) -> str:
    """What the fingerprint binds besides the counts (AA-839 review): the tenant id, or the traveller key (the
    address hash with the DRY RUN's salt, plan.traveller_key)."""
    if payload["subject"] == "tenant":
        return payload["tenant"]
    return pl.traveller_key(payload["email_sha256"], salt or "")


def validate_payload(payload: dict) -> dict:
    """The normalised payload, or NonRetryable. Pure, unit-tested; the router runs it before enqueueing."""
    p = dict(payload or {})
    subject = p.get("subject")
    if subject not in SUBJECTS:
        raise NonRetryable(f"subject must be one of {list(SUBJECTS)}")
    mode = p.get("mode", "dry_run")
    if mode not in MODES:
        raise NonRetryable(f"mode must be one of {list(MODES)}")
    p["mode"] = mode
    if "verify" in p:
        if p["verify"] is not True or subject != "traveller" or mode != "dry_run":
            raise NonRetryable("verify is only for a traveller DRY RUN after an apply (it strips its own hash)")
    if subject == "tenant":
        tid = str(p.get("tenant") or "").strip().lower()
        if not UUID_RE.match(tid):
            raise NonRetryable("tenant must be a tenant id (uuid)")
        if tid == inv.PLATFORM_TENANT_ID:
            raise NonRetryable("the AA internal tenant can never be erased")
        if "email_sha256" in p:
            raise NonRetryable("a tenant erasure takes no email_sha256")
        p["tenant"] = tid
    else:
        h = str(p.get("email_sha256") or "").strip().lower()
        if not HEX64.match(h):
            raise NonRetryable("email_sha256 must be the 64-hex sha256 of the trimmed, lowercased address "
                               "(runbook section 2 step 2); the address itself is never accepted")
        if "tenant" in p:
            raise NonRetryable("a traveller erasure takes no tenant")
        p["email_sha256"] = h
    if mode == "apply":
        for k in ("dry_run_job_id", "approved_fingerprint", "approved_by", "snapshot_id"):
            if not str(p.get(k) or "").strip():
                raise NonRetryable(f"an apply needs {k} (destructive-ops.md: DRY RUN, approval, snapshot)")
        if not HEX64.match(str(p["approved_fingerprint"]).lower()):
            raise NonRetryable("approved_fingerprint must be the DRY RUN's 64-hex fingerprint")
        if not UUID_RE.match(str(p["dry_run_job_id"]).lower()):
            raise NonRetryable("dry_run_job_id must be a job id (uuid)")
    return p


def apply_refusals(payload: dict, dry_job: Optional[dict], tenant_row: Optional[dict], now: datetime,
                   env: Optional[dict] = None) -> list[str]:
    """Why an apply may not run (empty = it may). Pure, unit-tested. The recount checks come later, inside the
    apply transaction."""
    out = []
    if not apply_enabled(env):
        out.append(f"{APPLY_FLAG} is not 'true' on this environment (apply is off; turning it on needs approval)")
    if dry_job is None:
        out.append("dry_run_job_id is not a data_erasure job")
    else:
        dp = dry_job.get("payload") or {}
        res = dry_job.get("result") or {}
        if dry_job.get("kind") != KIND or dry_job.get("status") != "succeeded" or res.get("mode") != "dry_run":
            out.append("dry_run_job_id is not a succeeded data_erasure DRY RUN")
        if dp.get("subject") != payload.get("subject") or dp.get("tenant") != payload.get("tenant") \
                or dp.get("email_sha256") != payload.get("email_sha256"):
            out.append("the DRY RUN was for another subject")
        finished = dry_job.get("finished_at")
        if finished is None or now - finished > dry_run_max_age(env):
            out.append(f"the DRY RUN is older than {dry_run_max_age(env)}; run a new one and approve it")
        if res.get("fingerprint_version") != pl.FINGERPRINT_VERSION:
            out.append("the DRY RUN predates the subject-bound fingerprint (AA-839); run a new DRY RUN and "
                       "approve it")
        elif payload.get("subject") == "traveller" and not SALT_RE.match(str(res.get("subject_salt") or "")):
            out.append("the DRY RUN holds no subject salt (stripped or not a DRY RUN); run a new DRY RUN")
        if res.get("fingerprint") != str(payload.get("approved_fingerprint", "")).lower():
            out.append("approved_fingerprint differs from the DRY RUN's fingerprint")
        if res.get("blocked"):
            out.append("the DRY RUN had blockers")
        if res.get("used_by_apply"):
            out.append(f"the DRY RUN was already used by apply job {res['used_by_apply']}; run a new DRY RUN and "
                       "approve it")
    if payload.get("subject") == "tenant":
        out.extend(tenant_refusals(tenant_row, now, env))
    return out


def tenant_refusals(tenant_row: Optional[dict], now: datetime, env: Optional[dict] = None) -> list[str]:
    """The tenant must exist, be offboarded and be past the retention window. Checked before the apply and
    again inside the apply transaction on the locked row (a restore in between must stop the apply)."""
    if tenant_row is None:
        return ["the tenant row does not exist (already erased?)"]
    cancelled = tenant_row.get("cancelled_at")
    days = tenant_retention_days(env)
    if cancelled is None:
        return ["the tenant is not offboarded (POST /admin/tenants/{id}/offboard first)"]
    if now - cancelled < timedelta(days=days):
        return [f"the tenant was offboarded less than {days} days ago (ERASURE_TENANT_RETENTION_DAYS)"]
    return []


def totals(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        key = r["action"].split(":", 1)[0]
        out[key] = out.get(key, 0) + int(r["rows"])
    return out


def checklist(subject: str, tenant: Optional[str]) -> list[dict]:
    items = inv.TENANT_CHECKLIST if subject == "tenant" else inv.TRAVELLER_CHECKLIST
    return [{"owner": i.owner, "item": i.item.replace("<tenant>", tenant or "<tenant>"), "how": i.how}
            for i in items]


def redact_job(job: dict) -> dict:
    """The job as a caller without RESULT_CAPABILITY sees it: counts only, no hash, no ids."""
    result = job.get("result")
    if isinstance(result, dict):
        job["result"] = {**{k: result[k] for k in _REDACTED_RESULT_KEYS if k in result}, "redacted": True}
    payload = job.get("payload")
    if isinstance(payload, dict):
        for k in ("email_sha256", "approved_by", "actor"):
            if k in payload:
                payload[k] = "redacted"
    return job


# ── AWS (tenant objects outside the DB; best-effort, read-only) ─────────────────────────────────

def _list_count(s3, bucket: str, prefix: str) -> int:
    n, token = 0, None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        n += int(page.get("KeyCount", len(page.get("Contents") or [])))
        if not page.get("IsTruncated"):
            return n
        token = page.get("NextContinuationToken")


def _secret_exists(sm, name: str) -> Optional[bool]:
    try:
        sm.describe_secret(SecretId=name)
        return True
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
        if code == "ResourceNotFoundException":
            return False
        raise


async def tenant_objects(resources: dict, tenant: str) -> dict:
    """S3 objects under the tenant prefixes and the tenant secrets. Errors are recorded, never fatal."""
    out: dict[str, Any] = {"s3": [], "secrets": []}
    aws = resources.get("erasure_aws")
    if aws is None and os.getenv("ENVIRONMENT") == "test":
        out["note"] = "not checked (test environment)"
        return out
    try:
        if aws is None:
            import boto3
            aws = {"s3": boto3.client("s3"), "secretsmanager": boto3.client("secretsmanager")}
        from shared.aws_client.bronze import bronze_bucket
        bucket = aws.get("bucket") or bronze_bucket()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out
    for tpl in inv.TENANT_S3_PREFIXES:
        prefix = tpl.format(tenant=tenant)
        try:
            n = await asyncio.to_thread(_list_count, aws["s3"], bucket, prefix)
            out["s3"].append({"prefix": f"bronze:{prefix}", "objects": n})
        except Exception as e:
            out["s3"].append({"prefix": f"bronze:{prefix}", "objects": None, "error": type(e).__name__})
    for tpl in inv.TENANT_SECRETS:
        name = tpl.format(tenant=tenant)
        try:
            exists = await asyncio.to_thread(_secret_exists, aws["secretsmanager"], name)
            out["secrets"].append({"name": name, "exists": exists})
        except Exception as e:
            out["secrets"].append({"name": name, "exists": None, "error": type(e).__name__})
    return out


def _s3_ref_sql(table: str, col: str) -> tuple[str, str]:
    """(count SQL, outside-keys SQL); $1 = tenant id, $2 = the tenant's S3 prefixes. A key may be s3://bucket/key."""
    c = pl.qcol(col)
    key = f"regexp_replace(t.{c}, '^s3://[^/]+/', '')"
    inside = f"EXISTS (SELECT 1 FROM unnest($2::text[]) AS px(p) WHERE starts_with({key}, px.p))"
    where = f"t.tenant_id = $1::uuid AND t.{c} IS NOT NULL"
    return (f"SELECT count(*) AS n, count(*) FILTER (WHERE NOT {inside}) AS outside "
            f"FROM {pl.qname(table)} t WHERE {where}",
            f"SELECT DISTINCT t.{c}::text AS k FROM {pl.qname(table)} t WHERE {where} AND NOT {inside} "
            f"ORDER BY 1 LIMIT {inv.MAX_S3_REF_KEYS + 1}")


async def _s3_refs(conn, tenant: str, tables: set[str]) -> tuple[list[dict], list[dict]]:
    """S3 keys held by rows the apply deletes. Keys outside TENANT_S3_PREFIXES are listed (the operator's only
    pointer to them once the rows are gone); more than MAX_S3_REF_KEYS per column, or a column that cannot be
    read, blocks the apply. Each statement runs in a savepoint (a failure must not abort the DRY RUN's
    transaction)."""
    prefixes = [tpl.format(tenant=tenant) for tpl in inv.TENANT_S3_PREFIXES]
    out, blockers = [], []
    for table, col in inv.TENANT_S3_REF_COLUMNS:
        if table not in tables:
            continue
        name = f"{table}.{col}"
        count_sql, keys_sql = _s3_ref_sql(table, col)
        try:
            async with conn.transaction():
                row = await conn.fetchrow(count_sql, tenant, prefixes)
                keys = [r["k"] for r in await conn.fetch(keys_sql, tenant, prefixes)] if row["outside"] else []
        except Exception as e:
            out.append({"column": name, "rows": None, "error": type(e).__name__})
            blockers.append({"kind": "s3_refs", "table": table, "rows": None,
                             "detail": f"{name} not readable ({type(e).__name__}): the S3 keys it holds are unknown"})
            continue
        listed = keys[:inv.MAX_S3_REF_KEYS]
        out.append({"column": name, "rows": int(row["n"]), "outside_prefixes": int(row["outside"]),
                    "outside_keys": listed,
                    "outside_dirs": sorted({k.rsplit("/", 1)[0] + "/" for k in listed if "/" in k})})
        if len(keys) > inv.MAX_S3_REF_KEYS:
            blockers.append({"kind": "s3_refs_unlisted", "table": table, "rows": int(row["outside"]),
                             "detail": f"{name}: more than {inv.MAX_S3_REF_KEYS} distinct keys outside the tenant "
                                       "prefixes; list them by hand before an apply"})
    return out, blockers


# ── the job ─────────────────────────────────────────────────────────────────────────────────────

async def _timeouts(conn) -> None:
    await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
    await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")


async def _dry_job(conn, job_id: str) -> Optional[dict]:
    import json
    row = await conn.fetchrow("SELECT id, kind, status, payload, result, finished_at FROM shared.job "
                              "WHERE id = $1::uuid", job_id)
    if row is None:
        return None
    d = dict(row)
    for k in ("payload", "result"):
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


async def run_erasure(ctx: JobContext, *, env: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    payload = validate_payload(ctx.payload)
    subject, mode = payload["subject"], payload["mode"]
    pool = ctx.pool
    async with pool.acquire() as conn:
        catalog = await pl.load_catalog(conn)
        tenant_row = None
        if subject == "tenant":
            row = await conn.fetchrow("SELECT tenant_id, is_active, cancelled_at FROM shared.tenants "
                                      "WHERE tenant_id = $1::uuid", payload["tenant"])
            tenant_row = dict(row) if row else None
        await ctx.progress(phase="counting", subject=subject, mode=mode)
        plan = pl.plan_tenant(inv.Registry(), catalog) if subject == "tenant" else None
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await _timeouts(conn)
            if subject == "tenant":
                counted = await pl.count_tenant(conn, plan, payload["tenant"])
                s3_refs, s3_blockers = await _s3_refs(conn, payload["tenant"], catalog.tables)
                counted["blockers"].extend(s3_blockers)
            else:
                counted = await pl.count_traveller(conn, payload["email_sha256"], catalog.tables)
                counted.setdefault("blockers", [])
        rows = counted["rows"]
        dry_job = await _dry_job(conn, payload["dry_run_job_id"]) if mode == "apply" else None
        # a traveller DRY RUN draws the salt; a traveller apply reuses its DRY RUN's (apply_refusals refuses a
        # missing one); a tenant fingerprint is keyed by the tenant id and takes no salt
        salt = None
        if subject == "traveller":
            salt = (secrets.token_hex(16) if mode == "dry_run" else
                    str(((dry_job or {}).get("result") or {}).get("subject_salt") or ""))
        key = subject_key(payload, salt)
        fp = pl.fingerprint(subject, key, rows)
        result: dict[str, Any] = {
            "subject": subject, "mode": "dry_run", "fingerprint": fp, "fingerprint_version": pl.FINGERPRINT_VERSION,
            "totals": totals(rows), "rows": rows,
            "blockers": counted.get("blockers", []), "blocked": bool(counted.get("blockers")),
            "checklist": checklist(subject, payload.get("tenant")),
        }
        if subject == "traveller" and mode == "dry_run" and not payload.get("verify"):
            # kept only while the hash is kept: _strip_hash removes both (a verification run is never applied)
            result["subject_salt"] = salt
        if subject == "tenant":
            result.update({
                "tenant": payload["tenant"],
                "tenant_state": (None if tenant_row is None else
                                 "offboarded" if tenant_row["cancelled_at"] else
                                 "active" if tenant_row["is_active"] else "suspended"),
                "offboarded_at": tenant_row["cancelled_at"].isoformat()
                if tenant_row and tenant_row["cancelled_at"] else None,
                "checks_passed": counted.get("checks_passed", []),
                "unregistered": plan.unregistered, "missing_tables": plan.missing,
                "delete_order": plan.order,
                "null_first": [nf.edge.conname for nf in plan.null_first],
                "s3_refs": s3_refs,
            })
            result.update(await tenant_objects(ctx.resources, payload["tenant"]))
        else:
            result["skipped_tables"] = counted.get("skipped", [])
            result["manual"] = [r for r in rows if r["action"] == "manual" and r["rows"]]
        now = now or datetime.now(timezone.utc)
        refusals = apply_refusals(payload, dry_job, tenant_row, now, env) if mode == "apply" else None
        result["apply"] = {
            "enabled": apply_enabled(env),
            "tenant_retention_days": tenant_retention_days(env) if subject == "tenant" else None,
            "needs": ["approved_fingerprint = this fingerprint", "approved_by", "snapshot_id (manual RDS snapshot)",
                      "dry_run_job_id = this job", f"{APPLY_FLAG}=true", "a verified Cognito staff session"],
        }
        if mode == "dry_run":
            if payload.get("verify"):
                # the verification after an apply must not leave the hash behind for another retention window
                await _strip_hash(conn, [ctx.job_id], None, STRIPPED_VERIFY)
                result["hash_stripped"] = True
            await ctx.progress(phase="done", mode="dry_run", **result["totals"])
            logger.info("erasure_dry_run", subject=subject, job_id=ctx.job_id, totals=result["totals"],
                        blocked=result["blocked"])
            return result
        if refusals:
            raise NonRetryable("apply refused: " + "; ".join(refusals))
        # single use: the apply claims its DRY RUN before it writes anything; a second apply or a retry of this
        # one finds the claim and is refused (AA-839 review)
        claimed = await conn.fetchval(
            "UPDATE shared.job SET result = coalesce(result, '{}'::jsonb) || jsonb_build_object('used_by_apply', "
            "$2::text) WHERE id = $1::uuid AND NOT coalesce(result ? 'used_by_apply', false) RETURNING id",
            payload["dry_run_job_id"], ctx.job_id)
        if claimed is None:
            raise NonRetryable("apply refused: the DRY RUN was already used by another apply; run a new DRY RUN")
        return await _apply(ctx, conn, payload, plan, catalog.tables, fp, key, result, env=env, now=now)


async def _strip_hash(conn, job_ids: list[str], email_sha256: Optional[str], marker: str) -> None:
    """Replaces the traveller hash in data_erasure job rows: the given ids, and every row holding that hash. The
    DRY RUN's subject salt goes with it, so the fingerprint left in the row cannot be tested against a guessed
    address (AA-839 review)."""
    await conn.execute(
        "UPDATE shared.job SET payload = jsonb_set(payload, '{email_sha256}', to_jsonb($2::text)), "
        "result = CASE WHEN jsonb_typeof(result) = 'object' THEN result - 'subject_salt' ELSE result END "
        "WHERE kind = $4 AND jsonb_typeof(payload) = 'object' AND payload ? 'email_sha256' "
        "AND (id = ANY($1::uuid[]) OR ($3::text IS NOT NULL AND payload ->> 'email_sha256' = $3::text))",
        job_ids, marker, email_sha256, KIND)


async def _apply(ctx: JobContext, conn, payload: dict, plan, tables: set[str], dry_fp: str, key: str,
                 result: dict, *, env: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    subject = payload["subject"]
    approved = str(payload["approved_fingerprint"]).lower()
    if dry_fp != approved:
        raise NonRetryable("the counts changed since the approved DRY RUN (fingerprint differs); nothing written")
    if result["blocked"]:
        raise NonRetryable("blockers present; nothing written")
    await ctx.progress(phase="applying", subject=subject)
    async with conn.transaction():
        await _timeouts(conn)
        if subject == "tenant":
            # lock the account row and re-check its state: a restore (or anything else) landing after the checks
            # above must stop the apply, whether or not it changed a counted row (AA-839 review)
            row = await conn.fetchrow("SELECT cancelled_at, is_active FROM shared.tenants WHERE tenant_id = $1::uuid "
                                      "FOR UPDATE", payload["tenant"])
            why = tenant_refusals(dict(row) if row else None, now or datetime.now(timezone.utc), env)
            if why:
                raise NonRetryable("apply refused inside the transaction: " + "; ".join(why))
        # recount inside the write transaction: the approval must still describe these exact rows
        if subject == "tenant":
            recount = await pl.count_tenant(conn, plan, payload["tenant"])
        else:
            recount = await pl.count_traveller(conn, payload["email_sha256"], tables)
        if pl.fingerprint(subject, key, recount["rows"]) != approved or recount.get("blockers"):
            raise NonRetryable("the recount inside the apply transaction differs from the approved DRY RUN")
        expected = {(r["table"], r["action"]): int(r["rows"]) for r in recount["rows"]}
        if subject == "tenant":
            done = await pl.apply_tenant(conn, plan, payload["tenant"], expected, inv.Registry().merge_sql)
        else:
            done = await pl.apply_traveller(conn, payload["email_sha256"], expected,
                                            protect_job_ids=[ctx.job_id, payload["dry_run_job_id"]])
        details = {"job_id": ctx.job_id, "dry_run_job_id": payload["dry_run_job_id"], "fingerprint": approved,
                   "approved_by": payload["approved_by"], "snapshot_id": payload["snapshot_id"],
                   "totals": totals(done)}
        if subject == "tenant":
            await write_audit_log(conn, tenant_id=payload["tenant"], actor=payload.get("actor") or "admin",
                                  action=TENANT_ERASED, resource_type="tenant", resource_id=payload["tenant"],
                                  details=details)
        else:
            await write_audit_log(conn, tenant_id=None, actor=payload.get("actor") or "admin",
                                  action=DSAR_ERASED, resource_type="dsar", resource_id=ctx.job_id, details=details)
            # the hash leaves CIS with the erasure: every data_erasure row that holds it (this apply, its DRY RUN,
            # earlier DRY RUNs of the same request) keeps everything but the hash
            await _strip_hash(conn, [ctx.job_id, payload["dry_run_job_id"]], payload["email_sha256"], STRIPPED)
    # independent verification from a new connection (destructive-ops step 5)
    async with ctx.pool.acquire() as fresh:
        if subject == "tenant":
            left = await pl.verify_tenant(fresh, plan, payload["tenant"])
        else:
            again = await pl.count_traveller(fresh, payload["email_sha256"], tables)
            left = {r["table"]: r["rows"] for r in again["rows"] if r["action"] in ("delete", "redact")}
    out = {**result, "mode": "apply", "applied": done, "totals": totals(done),
           "verified_left": {k: v for k, v in left.items() if v}, "verified": not any(left.values())}
    out.pop("apply", None)
    logger.info("erasure_applied", subject=subject, job_id=ctx.job_id, totals=out["totals"], verified=out["verified"])
    await ctx.progress(phase="done", mode="apply", **out["totals"])
    return out


async def on_terminal(pool, job_row: dict) -> None:
    """A traveller job that ended without success (failed, cancelled) keeps no hash: retention archives failed
    jobs to S3 for 90 days (AA-825), and a refused or failed run is never reused (no retry, single-use DRY RUN)."""
    import json
    payload = job_row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if payload.get("subject") != "traveller" or not HEX64.match(str(payload.get("email_sha256") or "")):
        return
    async with pool.acquire() as conn:
        await _strip_hash(conn, [str(job_row["id"])], None, f"stripped-on-{job_row.get('status') or 'end'}")


def retry_refusal(job_row: dict) -> Optional[str]:
    """POST /admin/job-runner/jobs/{id}/retry: an apply is never re-queued (its approval, the Cognito actor and
    the DRY RUN are single use); a DRY RUN may be."""
    payload = job_row.get("payload") or {}
    if isinstance(payload, dict) and payload.get("mode") == "apply":
        return "an erasure apply is never retried: run a new DRY RUN, approve it and start a new apply from " \
               "POST /admin/job-runner/erasure/run"
    return None


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=900, background=True, on_terminal=on_terminal,
          retry_refusal=retry_refusal)
async def run(ctx: JobContext) -> dict:
    result = await run_erasure(ctx)
    ctx.set_result(result)
    return result
