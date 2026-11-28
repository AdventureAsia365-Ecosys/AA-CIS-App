# Contracts with other apps

> Detail page of [CONTEXT.md](../../CONTEXT.md): the views, HTTP APIs and events CIS publishes or reads. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Contracts with other apps

CIS is the content system of record (ADR 0001 / 0002). Other apps read what it publishes; they never
write CIS schemas. Owners per schema: `AA-Ecosys/docs/database/schema-ownership.md`.

| Consumer | Reads from CIS | Writes into shared | How it is granted |
|---|---|---|---|
| AA-TripPlanner-Web | the Model Gateway config `shared.llm_role_config` / `shared.llm_model_catalog`; all content through the `catalog.*` views (row below) | `shared.llm_call_log` (rows tagged `quality_signal.app = "tripplanner"`) | CIS migration 173 (gateway tables + log) to role `tripplanner`. The pilot's direct reads are revoked: 254 (AA-847) `raw_tours` (179) and `tour_atoms` (180); 270 (AA-712 X4) `gold_aa_internal.published_tours`, `shared.destinations` (201), the photo views `shared.v_destination_photos` / `v_tour_photos` (201), `acp_contract.v_active_tour_atoms` (216) and USAGE on `gold_aa_internal` / `acp_contract` |
| AA-TripPlanner-Web | Model Gateway config `shared.llm_role_config`, `shared.llm_model_catalog` (stages `tp_*`) | — | CIS 173 |
| AA-Booking, AA-TripPlanner-Web | **Catalog read model**, schema `catalog` (AA-679): `v_tour` (live AA tours, key `(source_system, source_id)` = `('cis', raw_tours.source_group_id)`, `content_hash`), `v_tour_day` (parsed canonical itinerary), `v_tour_place` (active-atom places + place card + destination lat/lng), `v_tour_photo`, `v_destination`, `v_destination_photo`. **Coordinates carry an attribution duty**: wherever a consumer shows `lat`/`lng` from these views (map pins, area circles), it must show "Powered by Geoapify" (link) + "© OpenStreetMap contributors" (AA-712, Geoapify Free-plan + ODbL terms); the place views of migration 228 expose `located_by` / `geo_precision` so a consumer draws `area` points as areas, not pins. Contract `AA-Ecosys/docs/contracts/catalog.tour.v1.json`; `catalog.tour.v1.3` (CIS 274, AA-986 L4) adds `v_tour_retired` (tombstone: retired / paused / restored per key, derived from the served state) and `v_tour_lifecycle_outbox` (served-state events, cursor `event_id`); CIS 298 (AA-996 follow-up) adds `v_tour_place_name` (security_barrier, SELECT to `booking` only): the `source_system`, `source_id`, `tour_id`, `day_number`, `position`, `place_name` of `v_tour_place` row for row, without its per-place card / destination / segment lookups, for the Booking trip-plans list; read keyed by `tour_id` only. New consumers read only `catalog.*`; since CIS 270 TripPlanner does too (plus the Model Gateway). Events `catalog.tour.published/updated/trashed` (outbox) are the next AA-679 slice | its own `booking.*` | CIS 225 to roles `aa_app_user`, `tripplanner`, `booking` (unguarded). Views owned by NOLOGIN role `catalog_owner` (no BYPASSRLS, no members), which reads `published_tours` / `raw_tours` through RLS policy `catalog_owner_aa_master_read` (AA master tenant only); consumers need not set `app.tenant_id`. 225 also makes `tenant_isolation` on those two tables treat `app.tenant_id = ''` as no tenant (NULLIF) instead of raising |
| AA-Partner (partner CRM) | **`catalog.http.v1` over HTTP** (AA-976, CRM-B4; root ADR 0010 decision 4: the CRM holds no grant on any CIS schema): `GET /catalog/v1/tours` (keyset cursor by `source_id`, `updated_since`, `country_code`, `limit` ≤ 50), `GET /catalog/v1/tours/cis/{source_id}` (days with per-day and per-place IANA `time_zone`, inclusions, exclusions classified for pay-on-arrival R25, facets for system tags R29, photos), `GET /catalog/v1/destinations/{id}`. Served from the `catalog` views read as `aa_app_user` on the tenant pool (one connection per api task at most, READ ONLY, `app.tenant_id` = ''), so it shows exactly what Booking / TripPlanner see. Liveness per root ADR 0015 decision 5: `retired` → 410 `gone` + `reason` (the CRM marks lines "no longer offered"), `paused` → 404 + `reason`, never served → 404. Strong `ETag` (a tour's `content_hash` = sha256 over the served body), `Cache-Control: private, max-age=60`, 304 on `If-None-Match`. Code: `api/routers/catalog_http.py`, `services/catalog_http/`; contract copy `docs/contracts/catalog.http.v1.openapi.yaml` (byte-identical to the root file, conformance test `tests/unit/test_aa976_catalog_http_contract.py`). **AI mark** (contract 1.3, AA-1040, EU AI Act Art. 50(2)): every served `Tour` carries `ai` = `ai_marking.tour_mark(v_tour.text_updated_at, published_at)` (`ai_generated`, `generator` "Adventure Asia catalog writer", `model` null, `generated_at` = the last Master publish of the text, `human_edited` null), inside the hashed body (the ETag moves with a Master publish). **Enabled currencies** (contract 1.5, AA-1191): `GET /catalog/v1/currencies` answers `{enabled, base, cache_s}` (USD first, the setting `cis.billing.enabled_currencies` in force; `cache_s` = the key's cache window, how long the CRM keeps it), ETag / 304 like the other reads. **Overnight place** (contract 1.4, AA-1152): every `TourDay` carries `overnight_destination_id` (`catalog.v_tour_day_route.overnight_destination_id`, the value TripPlanner's day cards read; null when CIS has none), which may be a place that is not among the day's `places`; the CRM's add-on drawer offers add-ons there too. `text_updated_at` (CIS 359) is the Master row's `updated_at`, the value the tenant reads use; `v_tour.updated_at` also moves with a photo change, so it is used only while 359 is not applied (the reader probes `pg_attribute` once per process until the column is there and selects NULL in its place before). **AA-authored content** (AA-977, contract 1.2): `GET /catalog/v1/aa-content` (latest published version of each item that is not retired; `kind`, `locale`, keyset cursor by `content_id`, `limit` ≤ 50; ETag / 304) and `GET /catalog/v1/aa-content/{content_id}/versions/{version}` (any published version, also after a newer publish or retire; a draft / unknown version → 404), read from `gold_aa_internal.v_aa_content_published` on the main pool (cis_app); see "AA-authored content" below | — | M2M client-credentials token of the `aa-m2m-<env>` pool, CRM client + scope `cis.catalog/read` (root ADR 0006b decision 7, `api/core/m2m_auth.py`, same shared verifier as staff tokens); API Gateway route `/catalog/{proxy+}` (AA-CIS-Infra) |
| AA-Partner (partner CRM) | **`tenant.membership.v1` over HTTP** (AA-978, CRM-P1; root ADR 0006b decision 4, ADR 0010 decision 5): `GET /tenant/membership/v1/tenants/{id}/members` (invited + active members and `tenant_state` active / locked / deleted; ETag over the body without `as_of`, 304), `GET /tenant/membership/v1/memberships?iss=&sub=` (keyed by iss AND sub), `GET /tenant/membership/v1/events?after=&limit=` (cursor feed, `limit` ≤ 500, ordered by `(txid, seq)` below the snapshot horizon so a late commit is never skipped). Ids, identity and the Content studio role only: no e-mail, name or CRM role. Read from `shared.tenant_members` / `shared.tenant_membership_event` (CIS 276) on the main pool (cis_app); see "Tenant membership directory" below. Code: `api/routers/tenant_membership.py`, `services/tenant_membership/`; contract copy `docs/contracts/tenant.membership.v1.openapi.yaml` (test `tests/unit/test_aa978_tenant_membership_contract.py`) | — | M2M token of the `aa-m2m-<env>` pool, CRM client + scope `cis.tenant/read` (`api/core/m2m_auth.require_m2m`, the same dependency factory as `/catalog/v1`); API Gateway route `/tenant/membership/{membership_proxy+}` (AA-CIS-Infra, AA-978 S1) |
| AA-Partner (partner CRM), later AA-Booking and TripPlanner | **`tenant.entitlements.v1` over HTTP** (AA-1028 R1, root ADR 0019 d1 / d2): `GET /tenant/v1/tenants/{id}/account` (account status, `in_service`, kind, pilot marker, data region, time zone, every subscription, the resolved entitlements; ETag over the body without `as_of`, 304) and `GET /tenant/v1/account-events?after=&limit=` (cursor feed of `account.status_changed`, `subscription.changed`, `entitlement.changed`, the membership feed's horizon and cursor). Apps check feature keys, never plan names; no bill-to field, price, billing reference, reason or actor. Read from `shared.tenants`, `shared.tenant_subscription`, `shared.v_tenant_entitlement_v1` and `shared.tenant_account_event` (CIS 353-355) on the main pool (cis_app); see "Tenant control plane" below. Code: `api/routers/tenant_account.py`, `services/tenant_account/`; contract copy `docs/contracts/tenant.entitlements.v1.openapi.yaml` (test `tests/unit/test_aa1028_tenant_account_contract.py`) | — | M2M token, CRM client + scope `cis.tenant/read` (same dependency as `/tenant/membership/v1`; the Booking client joins with `bk_tenant_pull`); API Gateway route `/tenant/v1/{tenant_v1_proxy+}` (AA-CIS-Infra) |
| AA-Partner (partner CRM) | **`tenant.erasure.v1` over HTTP** (AA-1093 lane L2): `GET /tenant/v1/erasure-requests?after=&limit=` (cursor feed of the erasure requests that changed, each with the CRM step only: status waiting / active / done / failed, its not_before, its approved plan hash; snapshot horizon on the row's txid) and `POST /tenant/v1/erasure-requests/{id}/reports` (the contract's ErasureReport; idempotent on `report_id`: 201 / 200, 409 `report_conflict` / `round_superseded` / `phase_mismatch`, 403 `wrong_app`). Booking and TripPlanner read the same requests from `catalog.v_tenant_erasure_v1` (CIS 410). See "Tenant offboarding erasure saga" in the job runner section. Code: `api/routers/tenant_erasure.py`; contract copy `docs/contracts/tenant.erasure.v1.openapi.yaml` | — | M2M token, CRM client + scope `cis.tenant/erasure` (AA-CIS-Infra `identity_m2m.tf` adds it); the same API Gateway route `/tenant/v1/{tenant_v1_proxy+}` |
| AA-Partner (partner CRM) | **`tenant.addons.v1` over HTTP** (AA-1036 R3, root ADR 0019 d7): `GET /tenant/v1/tenants/{id}/add-ons` (the same list as the studio's `/v1/account/add-ons`: each add-on active / requested / available, the pending request, the last decision) and `POST /tenant/v1/tenants/{id}/add-on-requests` (`{product, note?, crm_member_id}`: 201 a new request, 200 the open one with `created` false). The CRM relays its **CRM owner's** request for an agency with no Content studio membership; the CRM checks the owner role, CIS records `requested_by` = `crm-member:<id>` (never a name or e-mail) and applies the studio's rules (`services/tenant_account/addon_requests.py`: 409 `already_subscribed` / `tenant_not_in_service`, one pending request per add-on, 10 a day, 500-character note). One store (`shared.tenant_addon_request`, CIS 374) and one staff decision path (Tenant 360 › Subscription). Code: `api/routers/tenant_addons.py`; contract copy `docs/contracts/tenant.addons.v1.openapi.yaml` (test `tests/unit/test_aa1036_tenant_addons_contract.py`) | — | M2M token, CRM client + scope `cis.tenant/addons` (`api/core/m2m_auth.require_m2m`; AA-CIS-Infra `identity_m2m.tf`); the same API Gateway route `/tenant/v1/{tenant_v1_proxy+}` |
| AA-Booking | **Supply content read-back** (AA-835, root ADR 0004a): `catalog.v_supply_content` (CIS 278), one row per Booking hotel / service CIS knows, key `(source_system 'booking', source_id = the Booking id)`: `content_status`, `content_version`, and `description`, `highlights`, `good_to_know`, `photo_urls` **only while published** (an approved version of an active entity; draft, in_review, stale and unpublished rows carry NULL text), `edit_url` = `/admin/supply-content/<id>` (Booking prefixes the CIS admin origin). Booking's hotel / service pages show it read-only and proposals carry only published text (AA-Booking `app/supply/content.py`, feature-detected) | — | CIS 278 to role `booking` only, under the `catalog.*:booking` contract grant (catalog_owner view, security_barrier) |
| Ops-probe Lambda (AA-CIS-Infra) | `POST /admin/dfs-balance/check`, `/admin/jev-canary/check`, `/admin/jev/402-sweep`, `/admin/waves/check` daily | — | admin secret |

CIS as a consumer (AA-858): the `/admin/overview` business view reads `booking.v_ops_funnel_daily`,
`booking.v_ops_case_status` (Booking bk-017) and `tripplanner.v_ops_traffic_daily` (TripPlanner tp-015) — aggregate
views the producers own and grant to `cis_app` in their own migrations (contract `ops.overview.v1`, AA-Ecosys
`docs/contracts/ops.overview.v1.md`; AA-CIS-Infra `db/owners.yaml` consumer_grants). CIS never reads `booking.*` /
`tripplanner.*` tables. The `/admin/data-coverage` page (AA-992) reads the family `ops.data_coverage.v1` the same way:
`tripplanner.v_ops_data_totals`, `v_ops_pin_components`, `v_ops_component_id_map` (tp-020) and
`booking.v_ops_data_totals`, `v_ops_trip_plan_keys` (bk-025), plus the CRM's counts over HTTP (`crm.ops.v1` 1.4
`GET /crm/ops/v1/data-coverage`, staff token relayed; no AA role reads schema `crm`).

CIS as a consumer of TripPlanner's AI notice events (AA-1126 L2 / L4): TripPlanner records `ai.allowance_used` (an
agency's monthly AI sessions are used up) and `ai.spend_degraded` (a spend check degraded under the prod spend rules) in
its own outbox and grants `cis_app` SELECT on `tripplanner.v_ai_notices_v1` (tp-034). The worker's ops tick
(`services/tripplanner_notices.py`) writes the bell rows as CIS: `tripplanner.ai_allowance_used` for the agency's
Content studio bell (Upgrade link) and `platform.tripplanner.spend_degraded` for the admin bell, once each (CIS 447
once-only indexes). TripPlanner holds no privilege on `shared.notifications` (CIS 274 / 351 guards).

CIS as a consumer of the public "from" price (AA-1029, contract `price.from.v1`, AA-Ecosys
`docs/contracts/price.from.v1.md`; Booking bk-040 grants `booking.v_from_prices` to `cis_app`): `GET /v1/tours/pool`
(the Content studio Marketplace) reads the suggested retail price per person of the page's tours by catalog key, on MAIN
after the tenant block, and returns it as `from_price` (null when Booking has none or the view cannot be read: fail
soft). `GET /v1/marketplace` (the studio's "In my catalog") reads it the same way (AA-1188): `from_price`, and
`price_usd` = its amount when the currency is USD. CIS never computes a price and never shows `raw_tours.price_raw` as
one (ADR 0005 d7); since AA-1188 no tenant read selects it.

**Tour reference prices (AA-1091, root ADR 0005 d7; CIS 402 / 403).** The structured REFERENCE price of an AA tour
(amount, currency, basis `per_person` | `per_group` | `from`) lives in `silver_aa_internal.tour_reference_price`, one row
per catalog key (`raw_tours.source_key`, stable across source versions); the free text stays in `raw_tours.price_raw`
for audit. `services/reference_price/`: `parse.py` (pure: the first amount and its currency, the basis words and the
doubt codes `several_amounts`, `tiered`, `basis_assumed` …; never invents a currency), `store.py` (scope = AA master
active rows with a key; status `confirmed` / `parsed` / `needs_review` (doubts, or the text changed since the value was
set: `price_raw_seen`) / `suggested` / `missing`; save, accept, clear, "read source texts" (DRY RUN first, confirmed and
staff entries never touched), Excel import plan / apply), `workbook.py` (openpyxl export with a How-to sheet, import by
column header). Admin page AA Catalogue › Reference prices (`/admin/reference-prices`, `api/routers/
admin_reference_prices.py`): server filters (status, country, search, live only) and paging, inline edit per row, bulk
accept, Excel export / import as base64 JSON (the REST API Gateway is not binary safe) with a DRY RUN before any write;
one `acp_shared.audit_log` row per changed key (`reference_price.*`, resource `tour_reference_price`). Capabilities
`reference_price.read` (admin, content_lead, content, customer_success, finance) and `reference_price.write` (admin,
content_lead, customer_success: the customer-service team). AA-Booking reads the saved values through
`catalog.v_tour_price_reference_v2` (CIS 403, catalog recipe, SELECT to `booking` only): one row per live tour with the
source text, amount, currency, basis and the same status rule; `v1` (CIS 365, text only) stays until Booking stops
reading it. A reference price is never a selling price: Booking never quotes it to an agency or a traveller.

CIS as a consumer of Booking supply facts (AA-835, root ADR 0004a; contracts `supply.hotel.v1` / `supply.service.v1`,
Booking bk-023 grants the four `booking.v_supply_*` views to `cis_app`). `services/supply_content/`:
- `pull.py`, one batch per api ops-sampler tick (`api/core/ops_metrics_sampler.py`, off with `SUPPLY_FEED_PULL=off`):
  `booking.v_supply_outbox` after the cursor in `shared.supply_feed_cursor` (row locked, SKIP LOCKED), the current
  facts from `v_supply_hotels` / `v_supply_services`, the inbox `shared.supply_event_inbox` (key = Booking
  `event_id`: a duplicate changes nothing; `applied` only when the facts' `supply_version` is newer, else
  `skipped_older`; `missing` when the entity left the view). An applied version stores the facts in
  `shared.supply_entity`, syncs `shared.supply_media` and queues one `supply_content_draft` job
  (`supply_content_draft:<source_id>:<supply_version>`); published content turns `stale` (no text served); a
  deactivated or missing entity turns `unpublished` (rows kept). A CIS photo copy whose media lost its usage rights
  or left Booking gets one `supply_media_purge` job (`services/jobs/supply_media_purge_job.py`: deletes both WebP
  objects, clears the copy columns, invalidates `/supply/<type>/<media_id>-*` on the photos CDN when
  `PHOTO_CDN_DISTRIBUTION_ID` is set; needs the task role's `s3:DeleteObject` on `photos/supply/*`). Lag, the last
  error and the purge jobs: `GET /admin/supply-content/status`.
- `supply_content_draft` job (`services/jobs/supply_content_draft_job.py`, background, concurrency 1, 2 attempts):
  kill switch `SUPPLY_CONTENT_LLM=off`, at most `SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY` (20) paid writer calls a UTC day plus
  the cost guard row `bedrock / job:supply_content_draft`; copies rights-cleared photos (`photos.py`: source bucket /
  key from `booking.v_supply_media`, checked against the bk-023 naming rule and the sha256; WebP 1600 / 600 under
  `photos/supply/` + the img. CDN); one call on stage `supply_content_writer` (`writer.py`: facts quoted as untrusted
  data, source fidelity, logged to `shared.llm_call_log` with the job id; route Claude Sonnet 5.5 `sonnet-5-5` →
  legacy `sonnet` since CIS 282, S251: the 278 seed was the legacy key, which lost the schema); writes a new `llm`
  draft version. Never publishes.
- Review (`review.py`, `/admin/supply-content`): an edit is a new `human` version; publish (review.act) refuses an
  inactive entity, a version written against older facts (409 `outdated`) and a photo without usage rights or copy
  (409 `photo_rights`); reject, unpublish, regenerate (content.write). AA-1151: publishing passes the two-person gate
  of tour content (`cis.content.publish_two_person`, `api/services/content_publish_gate.py`): with the switch on, a
  caller without `content.publish` gets 403 `two_person_required` and the author of a human version 403 `same_person`
  (an LLM draft has no human author); the Booking add-on offers (activities sold per place) take this text. Version content is immutable in the DB (CIS
  278 guard trigger). Admin UI (CIS Content › Assets › Supply content): `/admin/supply-content` (list with the
  feed board, server-side filters and paging) and `/admin/supply-content/<id>` (the page `edit_url` names: status,
  open draft with its diff and publish blockers, published version, photo rights state, Booking facts, version
  history; Publish / Reject / Unpublish for review.act, Edit / Regenerate for content.write). Every staff role reads.

Platform services other apps may reuse (already used by TripPlanner where noted): the Model Gateway
and `llm_call_log` (TripPlanner, through the shared core and the 323 views; the CRM vendors the core with its own
ledger), the job table template of `shared.job` (each app has its own table from it: `booking.job`, `crm.job`,
`tripplanner.job`; ADR 0003a), the cost guard `shared.spend_budget`, the Jev decision layer, and rerun
waves (`/admin/waves`, AA-727).

LLM operations (AA-900): `docs/llm/prompt-register.md` maps every gateway prompt to its stage, file and symbol, version
rule and eval baseline (guard `tests/unit/test_aa900_prompt_register.py`); `docs/llm/eval-runbook.md` is the by-hand
eval of a prompt, model or route change: an R2 run on the AA-729 golden set (`services/writing_memory/r2.py --stage
adhoc_aa900`, label `adhoc_aa900`, per-run cap <= 10 USD, the 25 USD writing-memory month cap) whose run ID and cost go
in the PR (template section "LLM change"). The route and model table is generated in the root repo
(`docs/generated/llm-routes.md`).

### AA-authored content (AA-977, CRM-B5)

AA staff write "Before you go" prep packs (R38), starter templates (R9) and campaign content blocks (R14) in CIS;
the partner CRM reads them over `catalog.http.v1` (row above) once published. CIS 275:
`gold_aa_internal.aa_content_item` (one locale of one piece; the EN and VI copies share `group_key`, same kind) and
`aa_content_version` (at most one draft, always the newest; published versions never change and are never deleted:
trigger `aa_content_version_guard`, rule E), view `v_aa_content_published` (published versions only: rule R). Grants:
`cis_app` only (no RLS: no tenant column, no personal data). Editor API `/admin/aa-content`
(`api/routers/admin_aa_content.py`, `services/aa_content/store.py`): drafts need `content.write`, publish / retire /
restore `master.admin`; every write carries the item's `updated_at` as `expected_updated_at` (409 `stale` otherwise).
At publish a prep pack whose visa section has no https link is refused (422 `visa_needs_official_link`: visa = an
official link only). Retire hides an item from the served list; its published versions stay fetchable by version.
`content_hash` = sha256 over the served AaContent body, computed once at publish (the ETag).
Editor page `/admin/aa-content` (CIS Content › Assets › AA content (CRM); every staff role reads, controls by
`can()`): a server-paged list, and a drawer per `group_key` with the EN and VI copies side by side (draft form, plain
preview, visa guidance mirrored from `validate.py` in `frontend/lib/aaContent.ts`, version history).

### Tenant membership directory (AA-978, CRM-P1)

Who belongs to which agency, with the CIS Content studio role (owner / editor / viewer; never a CRM role). CIS 276:
`shared.tenant_members` (identity `(cognito_iss, cognito_sub)` tagged `pii:online_id`, NULL while invited and bound
once at activation; status invited -> active -> removed, removed is final; one live row per identity and agency) and
the append-only `shared.tenant_membership_event`, written ONLY by triggers: on `shared.tenant_members` (invited /
activated / role_changed / removed) and on `shared.tenants` (created, and locked / unlocked / deleted when
`is_active` / `cancelled_at` change the agency state), so every admin tenant write path feeds it with no code
change. Deletes only 90 days after removal / occurrence or for an offboarded tenant (DM-7, AA-839 erasure). Grants:
`cis_app` only, no RLS, nothing for `aa_app_user`; the triggers run as the writer (not SECURITY DEFINER, MIG-6), so
a new writer of `shared.tenants` needs INSERT on the event table (migration guard + standing test). Staff routes
(Tenant 360 Members, `api/routers/admin_tenant360.py`, `services/tenant_membership/writer.py`): `GET
/admin/tenants/{id}/members` (identity shown as a 12-hex fingerprint only), `PATCH .../members/{membership_id}`
(Content studio role) and `POST .../members/{membership_id}/remove`, both `tenant.write` (AA-1035: or
`tenant.member.write` of the caller's book, never for the owner seat), audited
(`tenant.member_role_changed` / `tenant.member_removed`). The partner CRM applies a removal (member disabled and
signed out; last owner -> agency locked until owner recovery) and agency lifecycle within a minute; it never takes a
CRM role from CIS. Invitation and activation of identities came with AA-979 (partner-pool re-enrolment, below).
**Admin UI (AA-1031, S264):** Tenants › Tenant 360 › Members (`frontend/app/admin/tenants/_components/Tenant360Members.tsx`,
helpers `frontend/lib/tenantMembers.ts`) lists the memberships (fingerprint, role, status, invited / last change) and,
with `tenant.write`, invites (e-mail + role + mail language), sends an invitation again (the address is typed again:
CIS keeps none), changes the role and removes (warning when it is the last active owner), all over the routes above;
the mail result of an invitation stays on the page (sent / created but not delivered). Which write controls show is
the API's own answer for the session, `GET /admin/tenants/{id}/members/access` (`tenant.read`; `{read_only, actions:
{invite, send_again, change_role, remove}, invite_roles}` from `staff_authz.denial_reason` on each write route plus the
E2E invitation rule; hook `frontend/app/admin/_components/useMembersAccess.ts`, also used by the agency drawer's
"also invite to the Content studio" box): a read-only session (the Dev E2E admin) sees no write control, nothing shows
while the read loads, and only a failed read falls back to the role's `tenant.write` (AA-1031 S264 F3).
Tenant read (AA-978 follow-up): `GET /v1/account/members` (`api/routers/v1_account_members.py`,
`services/tenant_membership/portal.py`), the portal's Account › Members page: the agency's invited + active rows
(fingerprint, role, status, dates; no staff actor, no invitation id), tenant id from the tenant JWT only, main pool.

**Partner-pool Content studio sign-in (AA-979 P2b, root ADR 0010a).** The portal (AA-Partner `portal/`) swaps the
person's partner-pool access token (partner-web client) for a short tenant JWT at `POST /auth/tenant-session`
(`api/routers/tenant_session.py`; verifier `api/core/partner_identity.py`, its own `MultiIssuerVerifier`, never a staff
issuer; WORKFORCE / EXTERNAL / M2M tokens 403). It answers from the active `shared.tenant_members` rows of `(iss, sub)`
on active agencies (`services/tenant_membership/session.py`): none 403 `no_membership`, several 409 `choose_tenant`
(chooser `GET /auth/tenant-memberships`). The token is the legacy tenant token's shape (`_create_jwt`) plus `client=portal`, `amr=partner`,
`mid`, `csr`; at most 15 min and never past the access token. Throttled per identity in Redis, not per IP. The Content
studio role is enforced by `api/middleware/tenant_role.py` on every tenant-JWT path (`/v1/*`, `/admin/brand-identity`):
viewer read only, editor no writes under `/v1/integrations`, `/v1/account`, `/v1/webhooks`; owner and tokens without
`csr` unchanged. Invitations: `POST /admin/tenants/{id}/members/invitations` (`tenant.write`) creates the `invited` row and
relays the e-mail once to the CRM (`crm.ops.v1` `POST /tenants/{id}/studio-invitations`, staff token, through
`api/services/crm_console.py`); CIS stores no e-mail; `.../members/{membership_id}/invitation/send` sends again. The
CRM binds the accepted identity with `POST /tenant/membership/v1/invitations/{invitation_id}/activate` (M2M scope
`cis.tenant/activate`, contract 1.1; idempotent for the same identity, 409 otherwise). **No tenant API keys
(AA-1036 R3 slice 3, root ADR 0019 d6):** `POST /auth/tenant-login` (both handlers, `api/main.py` and the unmounted
`api/routers/auth.py` router) answers 410 Gone with `{"detail": "tenant_api_key_removed", "message": …,
"sign_in": "partner"}` for any body, reads no table and writes no audit row; `verify_tenant_api_key`, the Lambda
authorizer code, `POST /admin/tenants/{id}/generate-key`, `GET /v1/account/api-key`, the key in the create / restore
answers and the E2E key copy sync (`aa-cis/dev/e2e-tenant/*`) are gone. `tests/unit/test_aa1036_no_tenant_api_keys.py`
keeps it so (410 for any body, no key route, a source guard over `api/`, `services/`, `shared/`, `worker/`). CIS 368
sets `shared.tenants.api_key_hash` NULL; the column DROP is a separate destructive migration (DRY RUN, tech lead).
The 30-day `audit_log` check before the removal found only synthetic E2E tenants signing in with a key.

### AA Academy learning API (AA-1105, root ADR 0022, contract learn.http.v1)

`/learn/v1/*` (`api/routers/learn.py`, logic `services/learn/`) serves the AA Academy host of the AA-Partner CRM web
(`academy.<env domain>`), whose server side forwards the learner's token. **Its own route class:** only a WORKFORCE
access token of the app client `academy-web` (`api/core/learn_identity.py`, its own verifier, Dev client id a code
constant with the env override `COGNITO_ACADEMY_WEB_CLIENT_ID`); it is never a CIS staff client (a test keeps it out of
`cis_staff_issuers`), and a `cis-admin`, `breakglass`, `crm-staff`, M2M token is 403 `forbidden`, a partner-pool token
403 `partner_not_open` (P7). **The learner** is the ACTIVE `shared.admin_users` row bound to the verified `(iss, sub)`,
any role (else 403 `not_a_learner`); `learn.learner` is upserted on every call with the directory's `display_name`
(the certificate name; the client never writes it). Draft modules need `learn.preview` (`staff_authz`, admin and
content_lead). A Dev-only E2E identity is a test learner (`is_test`), refused outside Dev.
**Content** comes from the learn bundle under the docs reader prefix (`<prefix>/learn/`, `services/learn/bundle.py`,
cached 5 min, objects read only by manifest keys); quiz answers stay server side until a submission is graded.
**Records** live in schema `learn` (CIS 420; CIS 421 ties each child row's tenant to its learner's); every write passes
the learner's tenant (NULL for staff). **Thresholds** are the `cis.learn.*` settings (pass marks, retake delay, items,
recertification, video URL lifetime, review cap and target, verify rate). **Videos** (`learn-media/`, decision D8):
presigned GET URLs; the academy web fetches the `.vtt` captions server side and serves them from its own origin (no
bucket CORS change). **Public verify** `GET /learn/public/v1/certificates/{code}`: SHA-256 lookup, holder display name,
rate limited per client address (rightmost global X-Forwarded-For entry, `login_throttle.client_ip`: API Gateway
appends the real source, the NLB adds nothing, the ALB appends a private address), limit
`cis.learn.verify_requests_per_minute`, Redis window with an in-process fallback; every verify answer carries
`X-Learn-Visitor-Limit` (contract 1.1: setting `cis.learn.verify_visitor_requests_per_minute`, CIS 425, at most the
address limit minus one), the limit the academy web applies to each of its visitors. Labs answer 409 `lab_closed` until
P4b. Tests: `tests/unit/test_aa1105_learn_api.py` (bodies validated against the contract copy
`docs/contracts/learn.http.v1.openapi.yaml`, pinned by sha256), `tests/unit/test_aa1105_learn_rules.py`,
`tests/integration/test_aa1105_learn_store.py`.
