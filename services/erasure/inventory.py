"""AA-839 — what an erasure covers in the CIS schemas, and what it leaves to the other owners.

Tenant erasure: every CIS base table with a `tenant_id` column has one entry in TENANT_TABLES with its action.
The job compares this registry with the live catalog on every run: a table that has `tenant_id` and no entry is
listed under `unregistered` and blocks an apply (tests/integration/test_aa839_erasure_job.py fails CI on it), so a
new tenant table cannot be missed silently. Rows that reach the tenant only through foreign keys (no tenant_id)
are found from pg_constraint at run time by services/erasure/plan.py; they need no entry here.

Actions:
  delete  rows of the tenant are deleted (children first, see plan.py)
  detach  rows stay; tenant_id and the listed soft keys to deleted rows are set to NULL (platform cost totals)
  merge   rows are added into the NULL-tenant row of the same key, then deleted (rollups with a NULLS NOT
          DISTINCT unique key, where a plain detach could collide)
  keep    rows stay as they are (append-only audit chain); counted so the result says what remains

Traveller erasure (DSAR): CIS holds no traveller data by design (DM-6.1 tags in shared: only staff and alert
recipient e-mails). TRAVELLER_EXACT / TRAVELLER_TEXT are the places where an address could still land; the
job proves absence there. The traveller's own data is in other owners' schemas: TRAVELLER_CHECKLIST.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The 11 CIS runtime schemas (X1, tests/integration/conftest.py X1_CIS_SCHEMAS). `catalog` holds views only.
CIS_SCHEMAS: tuple[str, ...] = (
    "acp_contract", "acp_deliver", "acp_gold_output", "acp_shared", "acp_silver_s2", "acp_silver_s3",
    "acp_silver_s4", "geo", "gold_aa_internal", "shared", "silver_aa_internal",
)

# The AA internal tenant owns Master Content; it can never be erased (offboard refuses it too).
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"


@dataclass(frozen=True)
class TenantTable:
    table: str                       # schema.table
    action: str                      # delete | detach | merge | keep
    reason: str
    # Overrides the default `{t}.tenant_id = $1` predicate ({t} = the table alias, $1 = the tenant id as text).
    where: Optional[str] = None
    # detach: further soft-key columns (no FK) set to NULL with tenant_id, because they point at rows the erasure
    # deletes.
    null_also: tuple[str, ...] = ()
    # Without a tenant_id column (keyed some other way): skipped by the catalog check.
    no_tenant_column: bool = False


def _d(table: str, reason: str, **kw) -> TenantTable:
    return TenantTable(table, "delete", reason, **kw)


TENANT_TABLES: tuple[TenantTable, ...] = (
    # ── content and planning (T2 / T7 / T8 / T9, quarter plans)
    _d("acp_contract.route_pick", "tenant route picks (T7)"),
    _d("acp_deliver.tenant_tour_pages", "tenant tour page URLs"),
    _d("acp_shared.acp_output_rules", "tenant output rules (NULL tenant = global rules, untouched)"),
    _d("acp_shared.acp_quota_ledger", "tenant quota ledger"),
    _d("acp_shared.angle_gate_request", "T8 angle requests (options follow through the FK)"),
    _d("acp_shared.content_piece", "T9 content pieces (labels and publish rows follow)"),
    _d("acp_shared.debate_brand_fit_cache", "brand-fit cache of the tenant"),
    _d("acp_shared.facts", "tenant-scope facts (platform facts have no tenant)"),
    _d("acp_shared.publish_log", "tenant publish history (channels)"),
    _d("acp_shared.quarter_plan", "quarter plans (versions follow through the FK)"),
    _d("acp_shared.subject", "tenant subjects (T7 slate)"),
    _d("acp_shared.tenant_atom_state", "tenant atom state"),
    _d("acp_shared.tenant_config", "planning config: markets, channels, posts/week"),
    _d("acp_shared.year_plan", "year plans"),
    _d("gold_aa_internal.content_exports", "export rows"),
    _d("gold_aa_internal.published_tours", "tours published under the tenant"),
    _d("gold_aa_internal.tenant_tour_versions", "T2 tour versions"),
    _d("gold_aa_internal.webhook_deliveries", "webhook delivery log"),
    _d("silver_aa_internal.generated_content", "rewrites under the tenant"),
    _d("silver_aa_internal.quality_scores", "quality scores under the tenant"),
    _d("silver_aa_internal.raw_sources", "the tenant's uploaded sources"),
    _d("silver_aa_internal.raw_tours", "the tenant's own raw tours (derived atoms/routes follow through FKs)"),
    _d("silver_aa_internal.review_queue", "review queue rows"),
    _d("silver_aa_internal.seo_context", "SEO context rows"),
    _d("silver_aa_internal.upload_staging", "upload staging rows"),
    # ── account, brand, integrations, settings
    _d("shared.tenant_brand_rules", "brand identity"),
    _d("shared.tenant_brand_rule_versions", "brand identity history"),
    _d("shared.tenant_brand_rules_deleted_aa404", "AA-404 backup copy of brand rules"),
    _d("shared.tenant_export_config", "export settings"),
    _d("shared.tenant_integrations", "CMS / social connections (secret names; the secrets are a checklist step)"),
    _d("shared.tenant_rewrite_usage", "rewrite counters"),
    _d("shared.tenant_seo_config", "SEO settings"),
    _d("shared.jev_tenant_allowlist", "Jev allow-list (offboard already removed it)"),
    _d("shared.unmapped_market_requests", "market requests"),
    _d("shared.notifications", "tenant notifications (bell)"),
    _d("shared.pipeline_runs", "tenant pipeline batches (acp_runs and raw rows follow through FKs)"),
    _d("shared.acp_runs", "ACP run rows"),
    _d("shared.tenant_api_usage", "per-request API usage; a CHECK forbids a tenant-actor row without a tenant"),
    _d("shared.tenant_api_usage_monthly", "monthly API usage rollup of the tenant"),
    # Job rows carry the tenant in the payload (offboard cancels the queued ones). Queued/running rows are a
    # blocker (plan.live_job_blockers), never deleted.
    _d("shared.job", "the tenant's finished jobs (payload tenant_id)",
       where="{t}.payload ->> 'tenant_id' = $1::text AND {t}.status NOT IN ('queued', 'running') "
             "AND {t}.kind <> 'data_erasure'",
       no_tenant_column=True),
    # The account row goes last (plan.py orders deletes children-first, and every table above points at it).
    _d("shared.tenants", "the tenant account row"),
    # ── legacy tables: absent on Dev (schema reference 22/10/2026), still created by the CI migration replay
    # (conftest X1_CI_ONLY_LEGACY_RLS + two competitor tables). Registered so a database that has them erases them.
    _d("acp_deliver.packets", "legacy ACP v1 delivery packets (absent on Dev)"),
    _d("acp_deliver.pieces", "legacy ACP v1 delivery pieces (absent on Dev)"),
    _d("acp_shared.acp_v2_runs", "legacy ACP v2 runs (absent on Dev)"),
    _d("acp_shared.acp_v2_slots", "legacy ACP v2 slots (absent on Dev)"),
    _d("acp_shared.competitor_index_cache", "legacy competitor cache (absent on Dev)"),
    _d("acp_shared.content_metric_snapshot", "legacy content metrics (absent on Dev)"),
    _d("acp_shared.unknown_ledger", "legacy unknown ledger (absent on Dev)"),
    _d("acp_silver_s2.competitor_inputs", "legacy competitor inputs (absent on Dev)"),
    # ── platform cost history: keep the totals, drop the link to the tenant
    TenantTable("shared.llm_call_log", "detach", "LLM cost history; the totals stay, the tenant link goes",
                null_also=("content_piece_id", "angle_gate_request_id", "job_id")),
    TenantTable("shared.decision_log", "detach", "Jev decision history and cache", null_also=("job_id",)),
    TenantTable("shared.dfs_call_log", "detach", "DataForSEO cost history"),
    TenantTable("shared.llm_call_daily", "merge",
                "daily LLM rollup; added into the NULL-tenant row of the same key, then deleted"),
    # ── kept
    TenantTable("acp_shared.audit_log", "keep",
                "append-only hash chain (triggers refuse UPDATE/DELETE); AA-839 keeps the audit row"),
)

TENANT_TABLE_BY_NAME: dict[str, TenantTable] = {t.table: t for t in TENANT_TABLES}

# merge SQL per table ($1 = tenant id as text). Sums the tenant's rows into the NULL-tenant row of the same key.
MERGE_SQL: dict[str, str] = {
    "shared.llm_call_daily": """
        INSERT INTO shared.llm_call_daily AS d
               (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
                calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at)
        SELECT day, app, NULL, stage, role, model, provider, account, is_shadow,
               sum(calls), sum(fallback_calls), sum(tokens_in), sum(tokens_out), sum(cost_usd), now()
          FROM shared.llm_call_daily
         WHERE tenant_id = $1::uuid
         GROUP BY day, app, stage, role, model, provider, account, is_shadow
        ON CONFLICT (day, app, tenant_id, stage, role, model, provider, account, is_shadow) DO UPDATE SET
               calls = d.calls + EXCLUDED.calls,
               fallback_calls = d.fallback_calls + EXCLUDED.fallback_calls,
               tokens_in = d.tokens_in + EXCLUDED.tokens_in,
               tokens_out = d.tokens_out + EXCLUDED.tokens_out,
               cost_usd = d.cost_usd + EXCLUDED.cost_usd,
               updated_at = now()
    """,
}

# Objects outside the database. {tenant} is the tenant id. Listed by the DRY RUN (best-effort), deleted by the
# operator (runbook section 4); the worker role has no delete rights on them.
TENANT_S3_PREFIXES: tuple[str, ...] = ("raw-inbox/{tenant}/", "brand-identity/{tenant}/")
TENANT_SECRETS: tuple[str, ...] = ("acp/cms/{tenant}", "acp/social/{tenant}")
# DB columns that hold an S3 key of the tenant (counted, so the operator knows what the prefixes must cover).
TENANT_S3_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("shared.tenants", "last_brand_brief_s3_key"),
    ("gold_aa_internal.webhook_deliveries", "payload_s3_path"),
    ("gold_aa_internal.content_exports", "s3_path"),
)


@dataclass(frozen=True)
class TravellerScan:
    table: str
    id_col: str                # printed ids (row ids, never values)
    column: str                # SQL expression on alias t (text)
    action: str                # delete | redact | manual
    reason: str
    exact: bool = False        # True: the whole value is the address; False: e-mail-shaped tokens inside text
    # redact: the SET clause used by the apply (alias-free column assignments)
    redact_set: Optional[str] = None


TRAVELLER_EXACT: tuple[TravellerScan, ...] = (
    TravellerScan("shared.admin_users", "id", "t.email", "manual",
                  "a staff account: a staff request, not a traveller erasure", exact=True),
    TravellerScan("shared.alert_recipient", "id", "t.email", "manual",
                  "an ops alert recipient: remove it on the Alerts page", exact=True),
)

TRAVELLER_TEXT: tuple[TravellerScan, ...] = (
    TravellerScan("shared.notifications", "id", "t.payload::text", "delete",
                  "a bell notification that carries the address"),
    TravellerScan("shared.job", "id", "t.payload::text", "redact",
                  "a job payload must hold ids only (DM-6.3)",
                  redact_set="payload = jsonb_build_object('redacted', 'AA-839')"),
    TravellerScan("shared.job", "id", "t.result::text", "redact", "a job result with the address",
                  redact_set="result = jsonb_build_object('redacted', 'AA-839')"),
    TravellerScan("shared.job", "id", "t.error", "redact", "a job error with the address",
                  redact_set="error = 'redacted (AA-839)'"),
    TravellerScan("acp_shared.audit_log", "id", "t.details::text", "manual",
                  "append-only audit chain: needs a reviewed migration to redact, never an UPDATE"),
    TravellerScan("acp_shared.publish_log", "publish_id", "t.last_error", "manual",
                  "a channel error message: review by hand"),
    TravellerScan("shared.tenants", "tenant_id", "t.cancellation_reason", "manual",
                  "an offboarding reason that names a person: review by hand"),
    TravellerScan("shared.tenant_integrations", "id", "t.config::text", "manual",
                  "a tenant's own channel config (the tenant's contact, not a traveller)"),
)

# E-mail-shaped token in text. Hashed after lower() and compared with the traveller hash (runbook section 2).
EMAIL_TOKEN_RE = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"


@dataclass(frozen=True)
class ChecklistItem:
    owner: str
    item: str
    how: str


TRAVELLER_CHECKLIST: tuple[ChecklistItem, ...] = (
    ChecklistItem("AA-Booking", "booking.customers, customer_otps, customer_sessions, inbox_events.payload, "
                  "trip_cases (+ children), notifications, media_files + S3 object; audit_logs before/after "
                  "redacted, never deleted; one Booking audit row for the erasure",
                  "runbook sections 2 (lookup) and 3 (erase): DB Migrate run-script, ids only, AA-Booking lane"),
    ChecklistItem("AA-TripPlanner-Web", "tripplanner.customers, sessions, trip_drafts, trip_events, outbox.payload",
                  "runbook sections 2 and 3: DB Migrate run-script, ids only, TripPlanner lane"),
    ChecklistItem("AA-Partner (crm)", "a tenant's own CRM contacts (DM-6.8: the tenant is the controller)",
                  "forward the request to the tenant; AA erases on the tenant's instruction (AA-Partner runbook)"),
    ChecklistItem("Cognito", "the EXTERNAL pool user, if any", "runbook section 3 step 5 (address at a prompt only)"),
    ChecklistItem("S3", "Booking inbox archives (archive/booking/), AA-825", "runbook section 3 step 6"),
    ChecklistItem("Staff mailbox", "bk_notify_email copies and the request thread", "runbook section 3 step 9"),
    ChecklistItem("Sentry", "events holding the address (CIS project)", "runbook section 3 step 9"),
    ChecklistItem("RDS backups", "automated backups age out in 7 days; delete the manual pre-erasure snapshot",
                  "runbook section 3 step 8"),
)

TENANT_CHECKLIST: tuple[ChecklistItem, ...] = (
    ChecklistItem("AA-CIS (operator)", "bronze S3 prefixes raw-inbox/<tenant>/ and brand-identity/<tenant>/",
                  "runbook section 4: aws s3 rm --recursive after the DB apply (WRITE)"),
    ChecklistItem("AA-CIS (operator)", "Secrets Manager acp/cms/<tenant> and acp/social/<tenant>",
                  "runbook section 4: aws secretsmanager delete-secret (WRITE)"),
    ChecklistItem("AA-Partner (crm)", "the tenant's CRM rows (DM-6.8 hard delete on offboarding)",
                  "AA-Partner tenant erasure, owner crm; cis_app has no grant on crm.*"),
    ChecklistItem("Cognito", "tenant portal users (AA-813, once built)", "runbook section 4"),
    ChecklistItem("Sentry", "events tagged with the tenant id", "runbook section 4"),
    ChecklistItem("RDS backups", "automated backups age out in 7 days; delete the manual pre-erasure snapshot",
                  "runbook section 4"),
)


@dataclass
class Registry:
    """The inventory as one object, so tests can hand the planner a smaller one."""
    tenant_tables: tuple[TenantTable, ...] = TENANT_TABLES
    merge_sql: dict[str, str] = field(default_factory=lambda: dict(MERGE_SQL))

    def by_name(self) -> dict[str, TenantTable]:
        return {t.table: t for t in self.tenant_tables}
