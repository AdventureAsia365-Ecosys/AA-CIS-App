# AA-1155 — custom staff roles, CIS code (S287, lane l2-cis-roles; spec tasks T8, T9, T10, T11)

Spec: root `.kiro/specs/AA-1155-custom-staff-roles/`. Foundation (CIS 462, Booking bk-062, Infra groups) shipped in
S286 (`AA-1155-custom-roles-foundation.md`). No migration in this lane.

## Decisions
- **Role of a custom holder = the custom code** (`custom_content`), not its `based_on`. Every role check goes through
  `staff_roles.role_perms()` (called by `staff_authz.has_perm` / `has_any_perm`): built-in -> ROLE_PERMS, custom code -> the capabilities of this request's verified
  Cognito identity (contextvar), nothing otherwise. So a forged `x-admin-role: custom_x` with the admin secret holds
  no capability ("Unknown staff role"), and code that still tests `role == "admin"` never admits a custom holder.
- **Built-in group outranks `cis-custom`** (same precedence rule as today); `booking-custom` alone is no CIS role.
- **Custom role read with the row** (one 60 s cache entry). A read error marks the grant unreadable and refuses only
  the custom holder (403 `custom_role_unavailable`), never a built-in holder (they never read the tables).
- **Unknown capability names in the table are dropped and logged**, approve class too (the table CHECK refuses it
  already): "each capability validated against the code's capability set".
- **Tenant scope of a custom role from its capabilities** (`tenant_scope.principal_scope`), never from `based_on`.
- **Frontend decided by capabilities** (R5.2): every PAGE_RULES / API_* rule carries `caps` (any of); the role lists
  stay as documentation and `lib/rbacCaps.test.mjs` proves each list equals the built-in roles holding the caps, so no
  built-in role gained or lost a page. Client components keep `can(role, cap)`: for a custom role the admin shell
  registers the capability list from the `cis_caps` display cookie (`useAdminIdentity`). Server code passes the
  verified session `{role, capabilities}`.
- **One catalogue route** `GET /admin/platform/roles` (staff.read) feeds both the Staff page pickers and the Roles &
  permissions page.
- **Business owner:** a second holder is 409 `business_owner_taken` (clear the first), not a silent move.
- **T11 scope:** all six routers under `/admin/budgets` and `/admin/ai-gateway` (`admin_budgets`, `admin_ai_gateway`,
  `_budgets`, `_spend`, `admin_llm_route`, `admin_llm_route_control`): R5.5 says "AI gateway route". 403 (not 401):
  the request is authenticated by the secret but lacks the session kind.
- **A10f / A10g read-only in CIS** (`e2e_read_only`, no write allow-list).
- **Reconcile A10g group set:** exactly `cis-custom` + `booking-custom` (root ADR 0006a A10g: "one exact set, no
  extra set"), not "cis-custom with an optional booking-custom" as the review suggested; the WORKFORCE trigger refuses
  a token for any other set, so a known-E2E listing for it would explain an account that cannot sign in.

## Changed
- A10f "holds its writes to synthetic test tours (publish, review)" (ADR 0006a A10f) is not built: CIS has no
  synthetic-tour marker. Both identities are read-only in CIS for now (safer); the two-person publish proof needs a
  follow-up (spec amendment or new task).
- Booking built-in roles show names only on the matrix page: their capability lists live in the Booking code and no
  contract exposes them to CIS (follow-up: a Booking built-in rows contract).
- Review fix: the staff reconcile job (C7, `services/staff_reconcile.py`) reads `cis-custom` / `booking-custom` and
  `admin_users.custom_role`. A row with a custom role must be in `cis-custom` alone; a built-in `cis-*` group left
  beside it (a failed remove step) is `cis_role` drift, because that group outranks `cis-custom` at sign-in. A
  Booking custom grant must be in `booking-custom`. A10f (`cis-content_lead`) and A10g (exactly `cis-custom` +
  `booking-custom`, the ADR's one set) are known Dev E2E identities. Sign-in logs `staff_role_drift` (with
  `custom_role`) when a custom role row resolves to a built-in group.

## Tradeoffs
- Contextvar lookup in `role_perms()` keeps ~20 `has_perm(role, ...)` call sites unchanged; outside a verified request
  a custom code holds nothing (fail closed), which is the desired default for jobs and scripts.
- View-as (`api/core/view_as.py`) keeps ROLE_PERMS: a custom role never starts a view-as session even with
  `tenant.view_as` (fail closed; widen when a custom role needs it).
- Q7 gives the Content role `tenant.read`, which in CIS means "read every tenant": it opens the Tenant 360 reads and
  other `tenant.read` GETs (route matrix test lists the difference to content_lead). Flagged for the tech lead.

## Should know
- Module layout (code-health ratchets: `staff_authz.py` and `staff_directory.py` may not grow): custom-role reads in
  `api/core/staff_roles.py`, the Cognito-only dependency in `api/core/cognito_session.py`, the directory's custom-role
  helpers and the business-owner write in `api/services/staff_custom_roles.py`.
- Tests: `tests/unit/test_aa1155_custom_roles_authz.py` (T8), `test_aa1155_staff_directory_custom.py` (T9),
  `test_aa1155_ai_controls_cognito.py` + budget audit in `test_aa1025_admin_central_budgets.py` (T11),
  `frontend/lib/rbacCaps.test.mjs` + `rolesMatrix.test.mjs` (T10), `tests/integration/test_aa1155_custom_roles_pg.py`.
- Tests that drive the AI routes with the secret now assume a Cognito session through
  `tests/unit/aa1155_cognito_session.py` (dependency override).
- Readiness adds 462 to `REQUIRED_CIS_ABOVE_GAP` (already applied on Dev).
- New admin page `/admin/platform/roles` needs a root catalogue row (root `docs/catalogue/features.yaml`).
