# AA-1038 R5 slice 3 — leads, brand and the per-tenant AI cap (AA-CIS-App part)

Session S272 (lane l10). Root ADR 0019 d10, d11, d2 / b19; contracts `lead.submitted.v1` (new) and
`tenant.entitlements.v1` 1.5 in the root repo. Other parts: AA-TripPlanner-Web (the lead flow, the M2M pull, the AI
cap, the brand), AA-Partner (`crm_tp_lead_pull`), root (contracts, ADR amendment, DM-7.1 row text).

## Decisions

- **Migration 390 (contract class)** appends to `catalog.v_tenant_tripplanner_v1` instead of a second route view: the
  planner already reads that row per request (BFF cache 60 s), and the route belongs to the site. `lead_channel` is
  resolved in the view (crm when `crm.core` is live, else the site's webhook / email when its target is set, else
  none), so TripPlanner applies one rule and no CIS logic is copied there. Only the resolved channel's target is
  served.
- **No secret in CIS.** The webhook signature key is TripPlanner's (per tenant, derived from a TripPlanner master
  secret); CIS stores only the agency's https URL and business inbox.
- **`catalog.v_tenant_ai_budget_v1` is a separate view**: it changes with every model call (spend), unlike the site
  row, and TripPlanner caches it 30 s per tenant. It mirrors `shared/cost_guard.py`: the resolved key (which already
  includes the AA-1016 row as an override, 356), a disabled override served as 0 USD, and no key = `none` (the
  consumer fails closed, lesson S266). The spend rule is Tenant 360 › Cost's (all apps, shadow rows out, UTC month).
- **Brand layer in kit core** (`brandTheme.ts`, tokens profile): only the accent family is themable, so every
  neutral / text / semantic pair of `tokens.css` keeps its checked contrast. Pairs checked: label on accent and
  accent-deep on card / bg / tint / soft (4.5:1). The accent against the page is not a pair: the kit's own gold is
  2.4:1 on white and the kit relies on borders and `accent-deep` focus rings.

## Changed

- `api/migrations/390_tenant_tripplanner_lead_route_ai_budget.sql` + `tests/integration/test_aa1038_migration_390.py`
  (conftest skips 390 in the replay, as 379).
- `frontend/app/_kit/core/brandTheme.ts`, `check-kit-copy.mjs` (`TOKENS_PROFILE`), `MANIFEST.json`,
  `frontend/lib/kitBrandTheme.test.mjs`.
- `docs/contracts/tenant.entitlements.v1.openapi.yaml` copied from root (1.5) and its sha256 pin.
- `services/erasure/inventory.py` description (the site row now holds the lead route; still erased with the tenant).

## Tradeoffs

- TripPlanner stops at 100 % of the budget whatever the AA-1016 row's thresholds say: a traveller-facing stage
  degrades (AA text, keyword search), it never waits for staff approval.
- The view computes the month spend per row; read by `tenant_id` only (index `idx_llm_call_log_tenant_created`).

## Should know

- 390 is the number after 389 (held by another lane of the wave); renumber per the preflight if main differs.
- The kit sync PR to AA-TripPlanner-Web must merge before the TripPlanner code PR (it imports
  `ui/core/brandTheme.ts`; lesson "consumer PR after Kit Sync").
- No Tenant 360 editor for the theme yet: set it by a reviewed DB Migrate `run-script` on Dev (follow-up). The lead
  route has one since the S272 follow-up below.

## Follow-up (S272, lane l14): the lead route editor refuses a non-public webhook URL

### Decisions

- **The editor did not exist yet**, so the write-time guard comes with it: Tenant 360 › Subscription › "TripPlanner
  lead route" (`Tenant360LeadRoute.tsx`, `lib/tenantLeadRoute.ts`) over `GET` / `PUT
  /admin/tenants/{id}/tripplanner-site/lead-route` in `admin_tenant_subscription.py` (the Subscription tab already
  holds the TripPlanner add-on). Read: the Subscription tab's rule; write: tenant.write + `_require_tenant_admin`.
- **The rule lives in `services/tenant_account/tripplanner_site.py`** (`webhook_url_problem`): https; an IP literal must
  be global unicast and not multicast (TripPlanner's `public_address_check` rule), IPv6 that carries IPv4
  (mapped, NAT64 64:ff9b::/96, 6to4, IPv4-compatible) is judged by the IPv4, and the legacy spellings a resolver reads
  as IPv4 (`127.1`, `2130706433`, `0x7f.0.0.1`, octal) are parsed with `inet_aton` (parsing only); a host name is
  refused when it is one label or ends in `localhost`, `local`, `internal`, `localdomain`, `home.arpa`, `lan`,
  `intranet`, `corp`, `home`. Then the 390 CHECK shape (fullmatch, so no CHECK violation reaches Postgres as a 500).
- **No DNS in CIS** (the task): a name can resolve differently at send time; TripPlanner resolves and checks again
  before storing and before each POST (`webhook_target_problem` / `public_address_check`), and the test
  `test_never_resolves_dns` pins that CIS does not.
- **A target is checked whenever it is given**, also when the channel does not use it: it is stored and goes live the
  moment the channel is switched.
- **A trailing root dot is stripped before the host checks** (review fix): `127.0.0.1.`, `10.0.0.1..`, `0x7f.1.` and
  `2130706433.` are still the IPv4 address to getaddrinfo and to WHATWG URL parsers, so they are `not_public`; a public
  FQDN with its root dot (`hooks.example.com.`) still passes (the 390 CHECK takes it); a host that is only dots is
  `invalid`, and `hooks.example.123.` is `invalid` like `hooks.example.123`. `[::1].` fails `urlsplit` (`invalid`).
- **422 shape** `{detail: {detail: <code>, field, message}}` + `X-Error-Code` (the Subscription routes' shape plus
  `field`); the form maps the code to its field (`fieldOfCode`) and shows the EN / VI message under it.

### Changed

- `services/tenant_account/tripplanner_site.py` (new), `api/routers/admin_tenant_subscription.py` (2 routes),
  `api/core/staff_authz.py` (2 POLICY rows), `services/acp_shared/audit_log.py` (`TRIPPLANNER_LEAD_ROUTE_CHANGED`),
  snapshots `aa1035_role_route_matrix.json` / `aa882_staff_route_policy.json`, `docs/api/openapi.json` (pinned
  FastAPI / Pydantic), `CONTEXT.md`.
- Frontend: `Tenant360LeadRoute.tsx`, `leadRoute.messages.ts`, `lib/tenantLeadRoute.ts`, mounted in
  `Tenant360Subscription.tsx`.
- Tests: `tests/unit/test_aa1038_lead_route_editor.py` (TripPlanner's vectors 127.0.0.1, 10.0.3.7:8443,
  169.254.169.254, [::1], http; plus CGNAT, multicast, unspecified, IPv4-mapped / NAT64 / 6to4, legacy IPv4, internal
  names; routes, POLICY, admin only), `tests/integration/test_aa1038_lead_route_editor_pg.py` (store / read / audit on
  the 390 table; a URL the CHECK accepts but is not public is refused before any write; every accepted URL satisfies
  the CHECK), `frontend/lib/tenantLeadRoute.test.mjs`.

### Tradeoffs

- The host-name list is "plainly internal" only. A public-looking name that resolves to a private address is
  TripPlanner's to catch at send time (it is: 503 `lead_channel_unavailable`, nothing stored).
- A public IPv6 literal is refused as `invalid`: the 390 CHECK takes `[A-Za-z0-9.-]` hosts only (no migration in this
  follow-up). An agency gives a host name.

### Should know

- No migration. Rows written before this follow-up are not re-checked (Dev: only the demo tenant's `none` row).
