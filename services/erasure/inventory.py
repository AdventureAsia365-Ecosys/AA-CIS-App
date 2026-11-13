"""AA-839 — what an erasure covers in the CIS schemas, and what it leaves to the other owners.

Tenant erasure: every CIS base table with a tenant key has one entry in TENANT_TABLES with its action. A tenant
key (plan._TENANT_KEYS_SQL) is a column named `tenant_id`, `tenant` or `owner_scope`, or any other column whose
name contains "tenant" and that no foreign key covers (a soft key such as
`atomize_day_fingerprint.tenant_tour_version_id`). The job compares this registry with the live catalog on every
run: a table with a tenant key and no entry is listed under `unregistered` and blocks an apply
(tests/integration/test_aa839_erasure_job.py fails CI on it), so a new tenant table cannot be missed silently.
Rows that reach the tenant only through foreign keys (no tenant key) are found from pg_constraint at run time by
services/erasure/plan.py; they need no entry here.

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

# The 7 CIS runtime schemas: the 11 of X1 (CIS 244) less the four empty legacy ones CIS 299 dropped (AA-850,
# tests/integration/conftest.py X1_LEGACY_EMPTY_SCHEMAS). `catalog` holds views only.
CIS_SCHEMAS: tuple[str, ...] = (
    "acp_contract", "acp_deliver", "acp_shared", "geo", "gold_aa_internal", "shared", "silver_aa_internal",
)

# AA-850 (CIS 294, data standards DM-3.9): dead CIS tables wait here for their drop, with no grant to any role. Not a
# runtime schema: the erasure neither counts nor deletes in it. A quarantined table that still has an FK to an erased
# table (a tenant-keyed one -> shared.tenants) is reported by plan.py as `quarantined_child` (information): the
# quarantine is refused while a non-platform tenant has rows there (DRY RUN verdict) and no role can write there
# since, and the FKs are NO ACTION, so a row that broke this would fail the apply loudly and roll the erasure back.
# Wave 1 (route_pick, tenant_atom_state, tenant_brand_rule_versions, tenant_brand_rules_deleted_aa404, acp_runs and
# five more) left TENANT_TABLES with 294 and is dropped by CIS 296; the schema stays, empty, for the next waves.
QUARANTINE_SCHEMAS: tuple[str, ...] = ("retired",)

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
    # Without a tenant_id column (keyed some other way): the predicate comes from `where` / `where_any`.
    no_tenant_column: bool = False
    # Predicate branches OR-ed together, each with the tables it reads: (template, needs). A branch whose tables
    # are absent from the database is left out (CI lacks some live tables). The tables a used branch reads are
    # "soft parents": plan.py deletes this table before them, since the predicate needs their rows to exist.
    where_any: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _d(table: str, reason: str, **kw) -> TenantTable:
    return TenantTable(table, "delete", reason, **kw)


TENANT_TABLES: tuple[TenantTable, ...] = (
    # ── content and planning (T2 / T7 / T8 / T9, quarter plans)
    _d("acp_deliver.tenant_tour_pages", "tenant tour page URLs"),
    _d("acp_shared.acp_output_rules", "tenant output rules (NULL tenant = global rules, untouched)"),
    _d("acp_shared.acp_quota_ledger", "tenant quota ledger"),
    _d("acp_shared.angle_gate_request", "T8 angle requests (options follow through the FK)"),
    _d("acp_shared.content_piece", "T9 content pieces (labels and publish rows follow)"),
    # AA-923 WM-32 (315): human labels on the tenant's T2 versions (keyed on tenant_id: the subject has no FK here).
    _d("acp_shared.content_label", "admin labels on the tenant's T2 versions and A1 rewrites"),
    # AA-1039 R6 (360): compliance flags on the tenant's pieces / posts, its tour e-mail notices, its notice period.
    _d("acp_shared.content_compliance_flag", "admin compliance flags on the tenant's pieces and live posts"),
    _d("acp_shared.tenant_tour_notice", "e-mail notices of retired / restored tours (ids and counts)"),
    _d("acp_shared.tour_retirement_policy", "the tenant's notice period (the platform row has no tenant)"),
    _d("acp_shared.debate_brand_fit_cache", "brand-fit cache of the tenant"),
    _d("acp_shared.facts", "tenant-scope facts (platform facts have no tenant)"),
    # AA-1047 (386): the FactSheets of the tenant's T2 / T9 writes (tour text, agency facts and brand lines,
    # pii:free_text); generated_content / tenant_tour_versions name them by fs_id with no FK
    _d("acp_shared.fact_sheet", "FactSheets of the tenant's T2 / T9 writes"),
    _d("acp_shared.publish_log", "tenant publish history (channels)"),
    _d("acp_shared.quarter_plan", "quarter plans (versions follow through the FK)"),
    _d("acp_shared.subject", "tenant subjects (T7 slate)"),
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
    _d("shared.tenant_export_config", "export settings"),
    _d("shared.tenant_integrations", "CMS / social connections (secret names; the secrets are a checklist step)"),
    _d("shared.tenant_rewrite_usage", "rewrite counters"),
    _d("shared.tenant_seo_config", "SEO settings"),
    _d("shared.jev_tenant_allowlist", "Jev allow-list (offboard already removed it)"),
    _d("shared.tenant_ai_budget", "the tenant's AI spend budget"),
    _d("shared.unmapped_market_requests", "market requests"),
    _d("shared.notifications", "tenant notifications (bell)"),
    # AA-978 (276): the guards allow these deletes only for an offboarded tenant (or after the 90-day retention).
    _d("shared.tenant_members", "the agency's memberships (identities, Content studio roles)"),
    _d("shared.tenant_membership_event", "the agency's tenant.membership.v1 feed events"),
    # AA-1028 R1 (353): the control-plane rows of the tenant. The guards allow the entitlement and event deletes only
    # for an offboarded tenant; the subscription / override deletes of an offboarded tenant emit nothing.
    _d("shared.tenant_subscription", "the tenant's product subscriptions (plan, status, billing reference)"),
    _d("shared.tenant_entitlement_override", "per-tenant entitlement exceptions"),
    _d("shared.tenant_entitlement", "the tenant's materialised entitlements"),
    _d("shared.tenant_account_event", "the tenant's tenant.entitlements.v1 account events"),
    # AA-1035 R2 (364): which staff member looks after the tenant, and the read-only view-as log of the tenant. Deleted
    # explicitly here as cis_app (364 grants DELETE on both for this; the view-as log's guard trigger allows a DELETE
    # only for an offboarded or deleted tenant, so the log stays append-only for everything else). The FK cascade
    # from shared.tenants is the backstop.
    _d("shared.staff_tenant_assignment", "the staff members assigned to the tenant (owner / backup / support)"),
    _d("shared.staff_view_as_session", "the read-only view-as sessions staff opened on the tenant"),
    # AA-1036 R3 (374): the tenant's add-on requests (its note, the staff decision); FK cascade as the backstop.
    _d("shared.tenant_addon_request", "the tenant's add-on requests and the staff decisions"),
    # AA-1038 R5 slice 2 (379): the TripPlanner site settings (theme tokens, locale); FK cascade as the backstop.
    _d("shared.tenant_tripplanner_site", "the tenant's TripPlanner site settings (theme, locale, lead route)"),
    # AA-916 (277, ADR 0010 decision 11): tenant text lives only in deletable tables. Platform rows have no tenant.
    _d("shared.writing_signal", "writing-memory signals (before/after text) of the tenant"),
    # AA-917 (280): tenant-scoped learned writing rules (derived from the tenant's text). Platform rules have no tenant.
    _d("shared.writing_rule", "writing-memory rules learned from the tenant's own edits"),
    # AA-919 (284): golden-set items made from the tenant's text (input, brand pack, outputs, labels) and their scored
    # results. Platform (A1) items have no tenant; the set row and the run rows hold no tenant text and stay.
    _d("shared.eval_golden_item", "golden-set items built from the tenant's content (eval inputs and outputs)"),
    _d("shared.eval_item_result", "eval results of the tenant's golden items (regenerated outputs)"),
    # AA-919 (287): shadow outputs written from the tenant's inputs (a cross-tenant probe row belongs to the probed
    # tenant) and the tenant's own releases (ids only; another tenant release naming one as previous is set NULL).
    # Platform releases have no tenant and stay.
    _d("shared.writing_memory_shadow", "writing-memory shadow outputs written from the tenant's inputs"),
    _d("shared.writing_memory_release", "the tenant's writing-memory releases (ids only, no text)"),
    # AA-921 (313): the tenant's masked voice examples (tenant text) and the links of its releases to them (ids only;
    # they also cascade from the release and the example). Platform examples have no tenant and stay.
    _d("shared.writing_exemplar", "writing-memory voice examples masked from the tenant's own content"),
    _d("shared.writing_memory_release_exemplar", "the examples the tenant's releases pin (ids only)"),
    # AA-922 (314): keep decisions on the tenant's releases (also cascade from the release) and the disagreement-queue
    # items of the tenant's pieces / golden items. The weekly KPI report is a platform table (ids and numbers only).
    _d("shared.writing_memory_keep", "keep decisions on the tenant's writing-memory releases"),
    _d("shared.writing_memory_disagreement", "judge / human disagreement items of the tenant's content"),
    # AA-1048 (388): claim labels of the tenant's pieces and versions (claim spans and reasons quote the tenant text).
    # Platform (A1) claims have no tenant and stay.
    _d("shared.eval_claim_label", "claim-level labels of the tenant's content (claim spans and reasons)"),
    # AA-1050 (391): the claim instrument's shadow verdicts of the tenant's pieces and versions (no text, spans only).
    _d("shared.claim_shadow_verdict", "claim-instrument shadow verdicts of the tenant's content (ids and spans)"),
    _d("shared.pipeline_runs", "tenant pipeline batches (raw rows follow through FKs)"),
    _d("shared.tenant_api_usage", "per-request API usage; a CHECK forbids a tenant-actor row without a tenant"),
    _d("shared.tenant_api_usage_monthly", "monthly API usage rollup of the tenant"),
    # Job rows carry the tenant in the payload (offboard cancels the queued ones). Queued/running rows are a
    # blocker (plan.live_job_blockers), never deleted.
    _d("shared.job", "the tenant's finished jobs (payload tenant_id)",
       where="{t}.payload ->> 'tenant_id' = $1::text AND {t}.status NOT IN ('queued', 'running') "
             "AND {t}.kind <> 'data_erasure'",
       no_tenant_column=True),
    # AA-839 review: tenant data keyed without a tenant_id column.
    # Tenant-owned atoms (AA-461 T5) carry owner_scope = the tenant id while tour_id may point at an AA master raw
    # tour, so the FK walk from the tenant's raw tours misses them. One node for both ways in: owner_scope, or a
    # tour of the tenant (else the platform atoms of a tenant raw tour would show up as a cross-tenant blocker).
    # Children (atom_embedding, atom_matches, atom_segment_member, atom_place_card) follow through the FKs.
    _d("acp_contract.tour_atoms", "atoms owned by the tenant (owner_scope) or on the tenant's own raw tours",
       no_tenant_column=True, where_any=(
           ("{t}.owner_scope = $1::text", ()),
           ("{t}.tour_id IN (SELECT {t}_rt.tour_id FROM silver_aa_internal.raw_tours {t}_rt "
            "WHERE {t}_rt.tenant_id = $1::uuid)", ("silver_aa_internal.raw_tours",)),
       )),
    # Retired AA-223 run-tour jobs (no new writes since AA-735): `tenant` held the request's tenant_id as text;
    # the slug is matched too in case an old writer stored it.
    _d("shared.pipeline_jobs", "historic run-tour jobs of the tenant (column tenant)",
       no_tenant_column=True, where_any=(
           ("{t}.tenant = $1::text", ()),
           ("{t}.tenant = (SELECT {t}_tn.slug::text FROM shared.tenants {t}_tn WHERE {t}_tn.tenant_id = $1::uuid)",
            ("shared.tenants",)),
       )),
    # A3 per-day fingerprints keyed by the version id (soft key, no FK): a tenant T2 version or a generated
    # content row of the tenant. Deleted before those tables (soft parents), so the predicate still finds them.
    _d("acp_contract.atomize_day_fingerprint", "A3 per-day fingerprints of the tenant's versions (soft key)",
       no_tenant_column=True, where_any=(
           ("{t}.tenant_tour_version_id IN (SELECT {t}_v.id FROM gold_aa_internal.tenant_tour_versions {t}_v "
            "WHERE {t}_v.tenant_id = $1::uuid)", ("gold_aa_internal.tenant_tour_versions",)),
           ("{t}.tenant_tour_version_id IN (SELECT {t}_g.id FROM silver_aa_internal.generated_content {t}_g "
            "WHERE {t}_g.tenant_id = $1::uuid)", ("silver_aa_internal.generated_content",)),
       )),
    # The account row goes last (plan.py orders deletes children-first, and every table above points at it).
    _d("shared.tenants", "the tenant account row"),
    # ── legacy tables: absent on Dev (schema reference 22/10/2026), still created by the CI migration replay
    # (conftest X1_CI_ONLY_LEGACY_RLS + the competitor cache). Registered so a database that has them erases them.
    # (acp_silver_s2.competitor_inputs left with its schema, CIS 299, AA-850; 208 had dropped the table.)
    _d("acp_deliver.packets", "legacy ACP v1 delivery packets (absent on Dev)"),
    _d("acp_deliver.pieces", "legacy ACP v1 delivery pieces (absent on Dev)"),
    _d("acp_shared.acp_v2_runs", "legacy ACP v2 runs (absent on Dev)"),
    _d("acp_shared.acp_v2_slots", "legacy ACP v2 slots (absent on Dev)"),
    _d("acp_shared.competitor_index_cache", "legacy competitor cache (absent on Dev)"),
    _d("acp_shared.content_metric_snapshot", "legacy content metrics (absent on Dev)"),
    _d("acp_shared.unknown_ledger", "legacy unknown ledger (absent on Dev)"),
    # ── platform cost history: keep the totals, drop the link to the tenant
    TenantTable("shared.llm_call_log", "detach", "LLM cost history; the totals stay, the tenant link goes",
                null_also=("content_piece_id", "angle_gate_request_id", "job_id")),
    TenantTable("shared.decision_log", "detach", "Jev decision history and cache", null_also=("job_id",)),
    TenantTable("shared.dfs_call_log", "detach", "DataForSEO cost history"),
    # tenant_id on geo.geocode_call comes from CIS migration 319 (AA-804)
    TenantTable("geo.geocode_call", "detach", "Geoapify cost history"),
    TenantTable("shared.llm_call_daily", "merge",
                "daily LLM rollup; added into the NULL-tenant row of the same key, then deleted"),
    # AA-1045 (372): the hourly usage rollup of the four ledgers above. A re-roll after the erasure recomputes the
    # hours still in the raw window from the detached ledger rows (NULL tenant), which is what this merge leaves.
    TenantTable("shared.ai_usage_hourly", "merge",
                "hourly usage rollup; summed into the NULL-tenant row of the same key, then deleted"),
    # AA-900 (385): the gateway's raw model replies (tenant content, pii:free_text): deleted, nothing to keep
    _d("shared.llm_cassette", "raw model replies of the tenant's calls (AI gateway cassette)"),
    # ── kept
    TenantTable("acp_shared.audit_log", "keep",
                "append-only hash chain (triggers refuse UPDATE/DELETE); the erasure keeps the audit row"),
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
    # One tenant row per key at most (the unique key includes tenant_id), so no GROUP BY: each row lands on the
    # NULL-tenant row of its key, the latency histograms added bucket by bucket.
    "shared.ai_usage_hourly": """
        INSERT INTO shared.ai_usage_hourly AS h
               (hour, source, app, provider, account, model, stage, role, tenant_id, status, error_class, traffic,
                requests, ledger_rows, fallback_requests, tokens_in, tokens_out, cache_read_tokens,
                cache_write_tokens, reasoning_tokens, cost_usd, latency_ms_sum, latency_ms_count, latency_hist,
                refreshed_at)
        SELECT hour, source, app, provider, account, model, stage, role, NULL, status, error_class, traffic,
               requests, ledger_rows, fallback_requests, tokens_in, tokens_out, cache_read_tokens,
               cache_write_tokens, reasoning_tokens, cost_usd, latency_ms_sum, latency_ms_count, latency_hist, now()
          FROM shared.ai_usage_hourly
         WHERE tenant_id = $1::uuid
        ON CONFLICT (hour, source, app, provider, account, model, stage, role, tenant_id, status, error_class,
                     traffic) DO UPDATE SET
               requests = h.requests + EXCLUDED.requests,
               ledger_rows = h.ledger_rows + EXCLUDED.ledger_rows,
               fallback_requests = h.fallback_requests + EXCLUDED.fallback_requests,
               tokens_in = h.tokens_in + EXCLUDED.tokens_in,
               tokens_out = h.tokens_out + EXCLUDED.tokens_out,
               cache_read_tokens = h.cache_read_tokens + EXCLUDED.cache_read_tokens,
               cache_write_tokens = h.cache_write_tokens + EXCLUDED.cache_write_tokens,
               reasoning_tokens = h.reasoning_tokens + EXCLUDED.reasoning_tokens,
               cost_usd = h.cost_usd + EXCLUDED.cost_usd,
               latency_ms_sum = h.latency_ms_sum + EXCLUDED.latency_ms_sum,
               latency_ms_count = h.latency_ms_count + EXCLUDED.latency_ms_count,
               latency_hist = (SELECT array_agg(a + b ORDER BY k)
                                 FROM unnest(h.latency_hist, EXCLUDED.latency_hist) WITH ORDINALITY AS u(a, b, k)),
               refreshed_at = now()
    """,
}

# Objects outside the database. {tenant} is the tenant id. Listed by the DRY RUN (best-effort), deleted by the
# operator (runbook section 4); the worker role has no delete rights on them.
TENANT_S3_PREFIXES: tuple[str, ...] = ("raw-inbox/{tenant}/", "brand-identity/{tenant}/")
TENANT_SECRETS: tuple[str, ...] = ("acp/cms/{tenant}", "acp/social/{tenant}")
# DB columns that hold an S3 key of the tenant. The apply deletes these rows, so the DRY RUN lists every key that
# is not under TENANT_S3_PREFIXES (up to MAX_S3_REF_KEYS per column; more blocks the apply): the operator keeps
# that list and deletes those objects after the DB apply (runbook section 4). Keys may be `s3://bucket/key`.
TENANT_S3_REF_COLUMNS: tuple[tuple[str, str], ...] = (
    ("shared.tenants", "last_brand_brief_s3_key"),
    ("gold_aa_internal.webhook_deliveries", "payload_s3_path"),
    ("gold_aa_internal.content_exports", "s3_path"),
    ("silver_aa_internal.raw_sources", "s3_path"),
)
MAX_S3_REF_KEYS = 200


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
                  redact_set="payload = jsonb_build_object('redacted', 'erasure')"),
    TravellerScan("shared.job", "id", "t.result::text", "redact", "a job result with the address",
                  redact_set="result = jsonb_build_object('redacted', 'erasure')"),
    TravellerScan("shared.job", "id", "t.error", "redact", "a job error with the address",
                  redact_set="error = 'redacted'"),
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
    ChecklistItem("AA-Booking", "inbox_events.payload, b2c trip_cases (+ children, kept as history), "
                  "notifications, retired_b2c_record (archived customer invoices and payments: redact the free text, "
                  "the rows stay under the accounting exception); audit_logs before/after redacted, never deleted; "
                  "one Booking audit row for the erasure",
                  "runbook sections 2 (lookup) and 3 (erase): DB Migrate run-script, ids only, AA-Booking lane"),
    ChecklistItem("AA-TripPlanner-Web", "tripplanner.customers, sessions, trip_drafts, trip_events, outbox.payload",
                  "runbook sections 2 and 3: DB Migrate run-script, ids only, TripPlanner lane"),
    ChecklistItem("AA-Partner (crm)", "a tenant's own CRM contacts (DM-6.8: the tenant is the controller)",
                  "forward the request to the tenant; AA erases on the tenant's instruction (AA-Partner runbook)"),
    ChecklistItem("Cognito", "the EXTERNAL pool user, if any", "runbook section 3 step 5 (address at a prompt only)"),
    ChecklistItem("S3", "Booking inbox archives (archive/booking/)", "runbook section 3 step 6"),
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
    ChecklistItem("AA-CIS (operator)", "S3 objects listed in s3_refs[].outside_keys (keys outside the two prefixes "
                  "whose DB rows the apply deletes)",
                  "runbook section 4: copy the list from the approved DRY RUN, remove each object after the apply"),
    ChecklistItem("AA-CIS (operator)", "retention archives s3://<bronze>/archive/{llm_call_log,"
                  "tenant_api_usage,job,decision_log}/ (gzipped JSONL rows that still carry the tenant id)",
                  "runbook section 4: scan for the tenant id (S3 Select), then rewrite or remove the object, or "
                  "record the decision to keep it until its lifecycle expiry"),
    ChecklistItem("AA-CIS (operator)", "golden-set fixture and score files under bronze "
                  "scripts/out/aa729/ (frozen tenant inputs and outputs; the DB rows go with the apply, these "
                  "objects do not), and any repo copy of that fixture",
                  "runbook section 4: cut a new fixture version without the tenant, import it, then delete the old "
                  "objects (WRITE); the importer refuses an offboarded tenant, so a stale fixture cannot come back"),
    # AA-1093 part 3: the tenant_backup job writes the tenant's rows to the gold bucket; the bucket's exports/ rule
    # deletes them after 7 days, so an erasure past the retention window finds none left (listed for an earlier one)
    ChecklistItem("AA-CIS (operator)", "per-tenant backups s3://<gold>/exports/tenants/<tenant>/cis/ "
                  "(tenant_backup job: the tenant's CIS rows as JSONL)",
                  "runbook section 4: the 7-day exports/ rule deletes them; an erasure inside 7 days of a backup "
                  "removes the prefix by hand after the DB apply (WRITE)"),
    ChecklistItem("AA-Partner (crm)", "the tenant's CRM rows (DM-6.8 hard delete on offboarding)",
                  "AA-Partner tenant erasure, owner crm; cis_app has no grant on crm.*"),
    ChecklistItem("Cognito", "tenant portal users (once built)", "runbook section 4"),
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
