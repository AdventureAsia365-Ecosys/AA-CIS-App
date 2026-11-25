# Staff roles and permissions

> Detail page of [CONTEXT.md](../../CONTEXT.md): staff roles and permissions, administrator MFA, the admin navigation, the audit trail, cost per app, the alerts catalogue and the retired Booking staff token. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Staff roles and permissions (AA-656)

One staff identity model. Every staff user is a row in `shared.admin_users` (role enum
`shared.admin_role`: `admin`, `content` — migration 218; its `reviewer` value is unused and forbidden since CIS 375 — and the read-only `viewer` — migration
249, AA-878; identity columns from migration 247:
`email` lower-case `@adventure.asia` only — one TEMPORARY exception until the Cognito E2E work is done (AA-816,
S243 option B, root ADR 0006a): the exact address `e2e@e2e-mail.lumiguides.it.com` as `content` (reviewer until CIS 375), not an owner,
no password, no Booking grant (migration 250 CHECK + `staff_directory.E2E_TEST_STAFF_EMAIL` /
`require_e2e_test_scope` + the WORKFORCE triggers); and, **Dev only** (A10b, AA-816 S252, root ADR 0006a A10b),
`e2e-cis-admin@e2e-mail.lumiguides.it.com` as an admin, not an owner, no password, no Booking grant (migration 283
CHECK + `api/core/e2e_staff.py` / `staff_directory.require_e2e_test_scope` / `require_dev_only_e2e_environment` +
the WORKFORCE triggers' dev-only map); and, **Dev only** (A10d, AA-1035 S270), `e2e-cis-cs@e2e-mail.lumiguides.it.com`
as `customer_success`, same scope rules (migration 378 CHECK; A10e-2, AA-1091 S279: its one possible Booking grant is
`contracting`, the "Pricing team" preset, Dev only — `e2e_staff.e2e_booking_role_allowed`; no migration: the 378 CHECK
constrains `admin_users` only, `staff_app_grant` has no E2E rule) —, `display_name`, `cognito_iss` + `cognito_sub`, `last_login_at`
("last seen"), `is_owner`; `password_hash` nullable, NULL fails closed). A staff member's role in another app
lives in `shared.staff_app_grant (admin_user_id, app, role)` (`app` = `booking`, roles `admin` / `sales` /
`contracting` / `finance` (CIS 363, group `booking-finance`); `contracting` is shown as "Product & operations";
`content_creator` is retired (AA-1150): never granted again, the CHECK still accepts it until no grant holds it, and
Sync deletes such a grant (audited, `details.retired_booking_role`) and removes `booking-content_creator`). AA-Partner (CRM) staff roles are a set per person in
`shared.staff_crm_grant (admin_user_id, role)` (AA-1031, migration 330: `platform_admin` / `ops` / `support` =
WORKFORCE groups `crm-platform-admin` / `crm-ops` / `crm-support`, root ADR 0006b decision 6), written by the same
directory writer with its own audit field and synced (added and removed) like the CIS / Booking groups; the E2E
directory addresses hold none, and the A10c CRM staff E2E user stays outside the directory. The Staff page's "Platform
owner" preset sets CIS admin + Booking admin + CRM `platform_admin` in one change (never the owner flag). The "Pricing team"
preset (AA-1091, `preset="pricing_team"`) gives CIS `customer_success` (Reference prices) and Booking `contracting`
(group `booking-contracting`: Booking `pricing.write`, `supply.write`, `trip_plan.write`). It only adds: it fills an empty
slot (CIS `viewer`, no Booking grant), keeps a role that already has those rights, and refuses (409
`preset_would_replace`) rather than replace any other role (one role per app: a CIS finance or Booking finance member
would lose those rights); CRM roles are untouched; one audit row with `details.preset`. **Lean people-role presets
(AA-1150, S286)**: the Staff page leads with three presets, "Administrator" (the `platform_owner` preset above),
"Business owner" (`preset="business_owner"`: CIS admin + Booking admin, CRM unchanged) and "Content + Product &
operations" (`preset="content_ops"`: CIS `content_lead`, no Booking role and no CRM role until custom roles exist,
tech lead decision Q1); every other role (the three fields and the Pricing team preset) is behind "Show all roles".
Add staff offers the same presets (the create fills the role fields from them). **Presets as data (AA-1155 T14,
CIS 465)**: the presets above are rows of `shared.staff_role_preset` (code, label, description, the CIS role or a CIS
custom role, the Booking mode keep / set / clear + role, the CRM mode keep / replace / add + roles, `additive_only`
with its keep lists, sort order, `archived`), seeded with those four and edited by an `is_owner` user in the Staff
page's "People-role presets" section (`GET/POST /admin/platform/presets`, `PATCH /admin/platform/presets/{code}`,
audited `staff.preset_created` / `staff.preset_updated` with before and after); `PATCH …/roles {"preset": code}`
reads the active row in the write's transaction (`api/services/staff_presets.py`). The code keeps only the guards: no
preset field can set the owner flag, the self-demote and last-owner checks run on what it changes, and an
additive-only preset refuses (409 `preset_would_replace`) to replace a held role. **CIS role editor (AA-1155 T13)**:
the same page's "CIS custom roles" section lets an `is_owner` user clone a built-in CIS role (never admin, viewer or
reviewer) into a `custom_<name>` role, tick its capabilities, relabel, archive or restore it (`GET/POST
/admin/platform/roles/cis`, `PATCH /admin/platform/roles/cis/{code}`, `api/services/staff_role_editor.py`, the same
rules as the Booking editor: approve-class or unknown capability 400, audited `staff.role_created` /
`staff.role_updated`); a role an active account holds is not archived (409 `role_in_use`), nor one an active preset
grants (409 `role_in_use_by_preset`). A preset edit re-checks a custom role only when its field changes or the preset
is restored, so a preset whose custom role was archived since (a Booking role is archived in Booking) can still be
relabelled or archived; applying it answers 400. Root ADR 0006 +
amendments 0006a / 0006b are the design; tenants are separate (`cis_tenant_token`, role `tenant`).

**R2 roles and tenant scope (AA-1035 slice 1, root ADR 0019 d3 / d4; CIS 362-364).** `shared.admin_role` also has
`content_lead`, `tenant_qa`, `customer_success`, `support`, `finance` and `ai_ops` (WORKFORCE groups `cis-<role>`,
ranked 10-39; `api/core/staff_identity.py GROUP_ROLES`). `reviewer` merged into `content` (CIS 363) and the alias is
gone (CIS 375, S269: the A10 E2E row is `content`, `admin_users_role_not_alias_chk` forbids `reviewer`, the group
`cis-reviewer` holds no role and the Staff page "Sync" removes a leftover membership). Each role is one explicit capability set in `api/core/staff_authz.py ROLE_PERMS` (the matrix of
root `docs/audits/2026-11-03-S265-ecosystem-restructure/clarifications.md` section 3), deny by default: a POLICY entry
is one capability or `any_of(...)`; a GET with no entry needs `content.read` (so `customer_success`, `support` and
`finance` reach only their listed routes). `tenant.read` still means "every tenant" (admin); the split adds
`tenant_content.read` (tenant text: Tenant QA, Content Trace, A4 logs, Writing Lab text), `catalog.read` (the atom
store), `tenant.book.read` / `tenant.member.write` (the tenant desk: directory and the non-money Tenant 360 tabs;
member actions customer_success only), `tenant.member.resend` (support: send an editor / viewer invitation again,
nothing else), `tenant.book.write` (customer_success: config, brand identity, RPM and publish limit of its book; plan,
suspend, key, offboard / restore and overrides stay `tenant.write` / `billing.write`), the owner seat (invite as owner,
change to / from owner, remove an owner, send an owner invitation again) only with `tenant.write`
(`admin_tenant360._require_owner_tier`: a send-again re-seals to the typed address),
`tenant.book.account.read` (customer_success only: plans and add-ons, subscription, usage, cost, Tenant QA verdicts,
the AI budget tile of the own book; support never, and its directory rows and Tenant 360 header carry the price,
overage and LLM cost as null, `staff_authz.sees_tenant_money`), `tenant.assign`, `billing.read` / `billing.write`
(PATCH `/admin/tenants/{id}` is field-level: finance only `plan_tier` / `is_active`, customer_success every other
field; finance opens Tenant 360 on `/details`, which answers it the header only), `wave.run` and `lab.read`. The
shared tenant picker reads `GET /admin/tenant-names` (id, name, slug, status; scoped like the directory) so the
Tenant QA, Writing Lab and AI & spend roles can pick a tenant without the directory. The
content-quality writes check `quality.write` (admin, content_lead, tenant_qa) instead of the literal admin role.
**Tenant scope:** `api/core/tenant_scope.py require_tenant_scope` runs on every staff route with `{tenant_id}`
(`dependencies=TENANT_SCOPED`; `tests/unit/test_aa1035_tenant_scope_routes.py` fails CI on a route without it):
admin, finance, ai_ops, content_lead and tenant_qa reach every tenant (their capabilities still decide the routes);
`customer_success` only tenants in `shared.staff_tenant_assignment` for them (kind owner / backup / support, one owner
per tenant), `support` those plus the pool (agency / demo tenants without an owner or in the `tech_touch` segment);
content and viewer only the aa_internal tenant (Master Content). The staff member is the verified Cognito
identity, or with the main admin secret the `x-admin-user-id` header; a book role without one gets 403. Refusals are
403 `Tenant outside your scope` whether the tenant exists or not. `GET /admin/tenants` lists only the caller's scope.
Assignments: `GET|PUT|DELETE /admin/tenants/{tenant_id}/assignments[/{admin_user_id}]` (writes `tenant.assign`,
audited `tenant.assignment_set` / `_removed`; since slice 2 the PUT takes only admin / customer_success / support
staff). The frontend matrix (`frontend/lib/rbac.ts`) mirrors the roles and capabilities; the role x route decisions
are pinned in `tests/unit/snapshots/aa1035_role_route_matrix.json`.

**Custom staff roles (AA-1155 spec tasks T8-T11, root ADR 0006a A14 / A10f / A10g; CIS 462).** Built-in roles stay
in `staff_authz.ROLE_PERMS`; CIS custom roles are data (`shared.staff_role` + `staff_role_capability`, code
`custom_<name>`), read by `api/core/staff_roles.py`. A token whose only CIS group is `cis-custom` resolves to the
row's `admin_users.custom_role`: the session's role is that code and its capabilities the role's rows the code knows
(never the approve class, `staff.write` / `staff.admin`, or an unknown name); `admin_users.role` keeps the role's
`based_on` and authorisation never falls back to it. Fail closed for these holders only (no custom role, unknown or
archived role, no usable capability, a read error: 403 `custom_role_unavailable`); built-in holders never read the
tables. `staff_roles.role_perms()` is the one lookup (a custom code holds capabilities only with its verified Cognito
session, so the secret cannot claim one); a custom role's tenant scope comes from its capabilities
(`tenant_scope.principal_scope`: `tenant.read` / `tenant_content.read` / billing / spend reads -> every tenant, the
book reads -> its book, `content.read` -> internal). Seed `custom_content` "Content" = content_lead minus
`content.settings.write` plus `tenant.read` (Q7; a unit test compares the migration with the code). Note:
`tenant.read` means "read every tenant", so it opens the Tenant 360 reads too, not only the directory.
`/auth/verify-admin` returns `capabilities` (and `custom_role_label`); the BFF caches them, sets the display cookies
`cis_caps` / `cis_role_name`, and `lib/rbac.ts` decides every page (`PAGE_RULES[].caps`), BFF rule (`API_RULES`,
`API_READ_RULES`, `API_WRITE_RULES`) and `can()` by capabilities; the role lists beside them are documentation kept
equal by `lib/rbacCaps.test.mjs`. **Staff page (T9):** the CIS and Booking pickers list each app's custom roles (`GET
/admin/platform/roles`, `staff.read`: CIS built-in + custom, Booking built-in names + custom roles from the bk-062 view
`booking.v_staff_role_capability`, the CRM groups, TripPlanner); a custom CIS role writes `custom_role` (role =
`based_on`) and the group `cis-custom` (`api/services/staff_custom_roles.py`), a Booking custom code is the `staff_app_grant` row and the group
`booking-custom` (Sync does the same); the business-owner marker (`admin_users.is_business_owner`, one holder) is
`PUT /admin/platform/staff/{id}/business-owner` (`staff.write`, owners only, audited `staff.business_owner_changed`
before / after, never an E2E address). **Roles & permissions (T10):** Platform › Roles & permissions
`/admin/platform/roles` (admin), read-only, from the same catalogue (`lib/rolesMatrix.ts`; no person, agent or E2E row,
no issue id). **AI controls (T11):** every route under `/admin/budgets` and `/admin/ai-gateway` (six routers) also runs
`api/core/cognito_session.require_cognito_session` (403 for the secret with any `x-admin-role`, a legacy session, the Preview
credential or the machine); `PUT` / `DELETE /admin/budgets/...` write `spend_budget.updated` / `.deleted` audit rows
with before and after in the write's transaction. A10f (`e2e-cis-content-lead@`, content_lead) and A10g
(`e2e-custom-maker@`, a custom role based on content_lead, Booking custom grant on Dev) are Dev-only and read-only in CIS
(`e2e_staff.DEV_ONLY_E2E_STAFF_ROLES`). Readiness requires 462.

**R2 slice 2 (AA-1035, S268; no migration).** *Assignments page* `/admin/tenants/assignments` (admin, customer_success,
support): `GET /admin/tenant-assignments` (every partner tenant of the caller's scope with owner / backup / support and
the pool flag) and `GET /admin/tenant-assignments/staff` (the assignable staff, `tenant.assign`); writes stay the
per-tenant routes (admin only). *View as tenant* (root ADR 0019 d5, capability `tenant.view_as`: admin,
customer_success own book, support own book + pool): `POST /admin/tenants/{tenant_id}/view-as` (scoped; reason 3-500,
optional ticket, 5-60 minutes; one open session per staff member; refused 409 for the AA internal tenant, an
offboarded one or one that is not live, `view_as.tenant_refusal`; refused 409 when the caller's `shared.admin_users`
row differs from its sign-in role) writes `shared.staff_view_as_session` + audit
`tenant.view_as_started` and returns a short-lived tenant JWT (`api/core/view_as.py`: `csr` viewer, `client` portal,
`vas` session, `stf` staff, `srl` the start role = the verified group role the start was authorised with); `GET /admin/view-as/sessions[/{id}]` and `POST .../{id}/end` (own; admin every one, an
admin end of another's is `revoked`). Every path that ends a session (the end route, a new start closing the caller's
other sessions, the gate) writes one `tenant.view_as_ended` row under that session's id with its end reason and cause.
`api/middleware/view_as.py ViewAsGateMiddleware` (around the tenant role middleware and the rate limiter; its own
per-session limit, 120 requests a minute, answers 429 before any check or audit row) lets such a token make only GET /
HEAD on `view_as.READ_ROUTES` (12 side-effect-free tenant reads; never `/v1/slate`, `/v1/billing`, exports,
integrations or the brand branch), re-checks the open session, that the staff row's current role (and the verified
group role the BFF forwards in `x-view-as-staff-role`, which can only end a session) is still the start role `srl`
(else cause `role_changed`), the tenant (the start's rule: offboarded / suspended / internal ends the view) and the
start role's tenant scope on every request (a lost right ends the session for good: `revoked`, or `expired` past its
expiry), and refuses (403 `view_as_not_for_role`) a read the start role may not make (`view_as.ROUTE_CAPS`: `/v1/quota` and `/v1/account/add-ons` need `tenant.read` or `tenant.book.account.read`, so
support never sees the plan, usage or add-on subscriptions and decisions; view-as never widens a role's own reads), and writes one `acp_shared.audit_log` row per request
(`tenant.view_as_request`, BEFORE a served read: no unlogged read, 503 when the row cannot be written); everything
else is 403 `view_as_read_only` (logged), an ended session 401 `view_as_ended`. `tenant_role.enforce` repeats the
read-only check on the dependency side. The token never reaches the browser: the start answers it only with the
request header `x-view-as-client: bff` (403 `view_as_bff_only` before any row otherwise), which only the view-as BFF
sets, and the generic `/api/admin/[...path]` proxy refuses the start path (`lib/viewAs.ts isViewAsStartPath`). The
admin BFF (`frontend/app/api/view-as/*`) keeps the token in the httpOnly cookie `aa_view_as` (path `/api/view-as`) and
forwards only the allowed reads of the signed-in member's role (`VIEW_AS_ROUTE_ROLES`, the mirror of `ROUTE_CAPS`); Tenant 360 › "View as partner" opens
`/admin/tenants/view-as` (banner: tenant, time left, reason, End view; tabs over the partner's reads).
*View as a lower staff role* (AA-1157, root ADR 0019 d5 as amended; CIS 470): an owner (`is_owner` on a Cognito session
holding the admin role: capability `staff.view_as_role` in `OWNER_PERMS`; the business owner marker alone does not
qualify) starts `POST /admin/view-as/roles` (`{role, reason 3-500, minutes 5-60}`, BFF only) on a built-in CIS role or
an active CIS custom role whose read surface (`api/core/staff_view_as.read_surface`: the GET POLICY routes, the unlisted
GETs, the read capabilities) is a subset of the owner's (403 `view_as_not_downward`). It ends the owner's other open
sessions (either kind), writes a `shared.staff_view_as_session` row with `tenant_id` NULL and `view_role`, one
`staff.view_as_started` audit row with the target role's FULL capability list, and answers a signed token (aud
`cis-staff-view-as`, never a tenant token) that the staff BFF keeps in the httpOnly cookie `aa_staff_view_as` (path `/`)
and forwards as `x-staff-view-as` on staff API calls. `StaffBearerMiddleware` resolves it after the Cognito check
(`staff_view_as.resolve`: own token, open row, still owner + admin, role still usable and downward; else 409
`staff_view_as_ended` and the session ends with one `staff.view_as_ended` row): the request then runs as the view role
(capabilities target ∩ viewer, `is_owner` False, no relay token: the crm.ops.v1 relays answer "unavailable"). Every
non-GET/HEAD/OPTIONS request is refused 403 `view_as_read_only`: in the browser flow by the staff BFF first (never
forwarded; one structured log line `staff_view_as_write_refused` in the admin web's log, no API row), and for a write
that reaches the API by `api/middleware/staff_view_as.py` before any route (inside the staff audit middleware: a
`staff.request` row with `details.view_as`) and again by `denial_reason`; the header on a non-Cognito request is 403
`staff_view_as_needs_cognito`. A custom target role that cannot be read right now answers 503 and leaves the session
open (no revocation is recorded); starts take a per-member advisory lock. `GET /admin/session` says `read_only` /
`view_as`. Back = `POST /admin/view-as/sessions/{id}/end` as the owner (the session read / end serve both kinds,
`kind` and `view_role`; the session list stays tenant sessions only); sign-out and staff disable close it too
(`view_as.close_open_sessions`, one ended row of the session's own kind), and so does the owner's read of the session
once it expired (the banner's check at 0 minutes; the token cookie outlives the view by 5 minutes for that read only)
or the member's session list (sign-out reads it first, so a view whose cookie is gone still gets its end row). The BFF (`lib/auth-server.ts requireAdmin`)
refuses writes during the view, checks rules and pages (`proxy.ts`) with the view role (claimed capabilities ∩
verified, only from a Cognito admin session) and the shell shows `StaffViewAsBanner` (role, read-only, time left,
Back); the Cognito cookies are never touched. Data scoped to assignments (customer_success / support books) follows the
owner's own assignments, so it may be empty.
*Logged Lab read*: ai_ops holds `lab.tenant_text.read` (never `tenant_content.read`), which opens only the tenant-text
GETs of `/admin/writing-memory/*` (not the Tenant 360 voice route); `api/core/lab_read_log.py LAB_TEXT_LOGGED` writes
an audit row `lab.tenant_text_read` before each such read (503 when it cannot). *Bell*: `notifications.read` (every
role) on the four `/admin/notifications` routes; customer_success / support / finance see only rows targeted at their
role, and of a tenant row only those of a tenant their scope reaches (`tenant_scope.tenant_scope_sql`: book; support
also the pool; finance every tenant; the platform rows of the AA internal tenant stay role-targeted). **R2 close-out (AA-1035, S269; CIS 375).** *CRM L1 console for the desk roles*: `/admin/platform/crm/*` GETs take
`tenant.book.read` (usage / entitlements `tenant.book.account.read`, customer_success only), `{tenant_id}` reads pass
`require_tenant_scope`, the agency list is filtered to the scope; the CRM itself serves a desk caller only the tenants
CIS reports through `tenant.membership.v1` 1.3 `GET /tenant/membership/v1/staff-scope?iss=&sub=&desk_role=`
(`tenant_scope.desk_scope_of_identity`, M2M `cis.tenant/read`), never a scope the caller sent. *Tenant QA own book*:
customer_success holds `tenant_content.book.read`: the Content quality lists, details and stats of its book (a piece or
version of another tenant is a 404), no labels, place cards, compliance or writes. *Send again to the kept address*:
`POST .../invitation/send` without `email` relays `keep_address: true` (crm.ops.v1 1.12); support may only do that
(403 with an address), customer_success and admin may also name a new one (`members/access` `send_again_to`).

**Dev-only E2E CIS admin (A10b, AA-816 S252, root ADR 0006a A10b).** The agent's own admin identity for evidence
screenshots of the admin-only pages and the AA-916 WM-07 sentence-action sample. `api/core/e2e_staff.py` holds the
rules: the directory creates, re-enables or syncs it only where `ENVIRONMENT` is exactly `dev` (403
`e2e_not_allowed_here`; disable stays allowed everywhere); `staff_identity.resolve_staff` refuses its token (403
`e2e_not_allowed_here`) unless `ENVIRONMENT` is exactly `dev`, else flags the identity `e2e_read_only` (log
`staff_identity_e2e_read_only`, owner flag forced false); the bearer middleware answers every non-GET/HEAD/OPTIONS on a
staff path with 403 `e2e_read_only` before the route runs, except POST
`/admin/content-quality/{pieces,tour-versions}/{id}/sentences/apply` (Cut / manual Edit with a reason chip; the
routes accept only `cut` / `manual` from it, `…/suggest` stays refused) and, since AA-979 (root ADR 0006a A10b-1,
S252), POST `/admin/tenants/{id}/members/invitations` and `/admin/tenants/{id}/members/{membership_id}/invitation/send`
(the phase 2 re-enrolment E2E); those two routes then refuse it (403 `e2e_read_only`, before any row, audit or relay)
unless the tenant is synthetic (`shared.tenants.is_canary IS TRUE`), the address is on the E2E mailbox domain
(`e2e-mail.lumiguides.it.com`) and the invited role is `viewer` / `editor`; `staff_authz.denial_reason` refuses the same
writes again by route template. AA-1035 slice 2 (proposed A10b-2) adds POST `/admin/tenants/{id}/view-as` and
`/admin/view-as/sessions/{id}/end`, synthetic tenants only (the routes refuse any other with 403 `e2e_read_only`), so
the evidence run can show the view-as banner; the session it opens is read-only like every view-as session. AA-1087
(root ADR 0006a A10b-3, S278) adds POST `/admin/tenant-qa/compliance/{flag_id}/release` (release a held piece, the
staff review "approve" of the synthetic pilot rehearsal): the route refuses it (403 `e2e_read_only`, log
`compliance_release_e2e_refused`, before the flag is resolved or audited) unless the flag's tenant is synthetic
(`is_canary IS TRUE`, not the pilot); Playwright `admin-evidence/compliance-release.spec.ts`. AA-1094 (root ADR 0006a
A10b-4, S278) adds POST `/admin/tenants/{id}/provision` and `/admin/tenants/{id}/provision/cancel` (the
`tenant_provision` attach / cancel behind Tenant 360 › Subscription "Add an add-on" / "Cancel"), so the agent can serve
a synthetic agency on TripPlanner: the routes refuse it (403 `e2e_read_only`, log `tenant_provision_e2e_refused`,
before a job is enqueued) unless every product of the request is `tripplanner` (`e2e_staff.E2E_ADDON_PRODUCTS`) and
the tenant is synthetic; the create route `POST /admin/tenants/provision` stays refused; Playwright
`admin-evidence/tp-addon.spec.ts`.
Dev-only fixtures for these checks (AA-1087 S278 l18): `python -m services.tenant_account.e2e_fixtures` (ECS Exec on
the API task, DRY RUN until `--apply`): `register-crm-agency --key sen-vang` enqueues the `tenant_provision` saga with
the CRM E2E agency's own id (a `create` may carry `tenant_id` on Dev, `demo` only: CIS, Booking `bk_tenant_pull` and
the CRM then name one synthetic tenant); `subject-gate-flag` adds one attempt with an invented off-catalogue booking
link to a synthetic tenant's latest request and runs the real subject gate, which opens the flag the spec releases.
AA-1086 G1: `rehearsal-tenant --run <run>` enqueues the same saga for the pilot rehearsal agency `pilot-rehearsal-01`
(`demo`, add-ons `crm.core` 3 seats + `tripplanner.hosted`) under the id AA-Partner's seed gives the agency key
`pilot-rehearsal-01`, so the CRM seeder adds its E2E owner and agent under the tenant CIS holds; a tenant that exists is
reported, never created twice (root `docs/ops/runbooks/pilot-rehearsal.md`).
`/auth/verify-admin` returns `admin`, so the BFF opens the admin pages. Its sign-in
codes land in the mailbox prefix `inbound-admin/`, readable only by the MFA admin role, so only the orchestrator signs
in with it, locally (Playwright project `admin-evidence`, `E2EADMIN=1`, never CI). Removed with A10 / A10a at the end
of project code.

**Dev-only E2E customer_success (A10d, AA-1035 S270, root ADR 0006a A10d).** The agent's own customer_success identity,
to prove the tenant-book scoping with a real Cognito session (Playwright `admin-evidence/cs-scope.spec.ts`, `E2ECS=1`,
never CI). Same directory, environment and read-only rules as A10b, but **no write allow-list**: the identity carries
`e2e_read_only` and not `e2e_allowlisted_writes`, the flag every A10b exception keys on (bearer middleware,
`staff_authz.denial_reason`, `docs_owner_read_via`, the view-as / studio-invitation / sentence-apply route checks), so
every write is 403 `e2e_read_only`. Its book holds synthetic tenants only: `PUT /admin/tenants/{id}/assignments/{staff}`
answers 409 for any Dev-only E2E row (A10b, A10d) on a tenant that is not synthetic. `/auth/verify-admin` returns
`customer_success`. Codes in `inbound-admin/` (MFA admin reads only). Removed with A10 / A10a / A10b / A10c.

**Dev-only E2E owner (A10h, AA-1176 S288, root ADR 0006a A10h; CIS 473).** The agent's administrator identity "like the
tech lead" for owner-only live checks (staff view-as, Platform › Compliance › Refresh, Staff › Add, Booking sale
exceptions on synthetic plans; Playwright `admin-evidence/owner-live.spec.ts`, `E2EOWNER=1`, never CI).
`e2e-owner@e2e-mail.lumiguides.it.com`, groups exactly `cis-admin` + `booking-admin`, directory row `admin` without a
custom role, owner marker set by the AA-CIS-Infra run-script `aa1176_e2e_owner_row`, never the business owner (CIS 473
CHECK). It is **not read-only**: not in `e2e_staff.DEV_ONLY_E2E_STAFF_ROLES`; the identity carries `e2e_owner` and keeps
`is_owner`. `resolve_staff` refuses it outside `ENVIRONMENT` exactly `dev` (403 `e2e_not_allowed_here`) and refuses any
other row / token shape (403 `e2e_owner_scope`). `staff_mfa.is_admin_session` exempts it (its code is readable only by
the MFA admin role). On the Staff page it never changes an owner, the business owner or itself (403 `e2e_owner_scope`),
never sets the business-owner marker, never counts as "another active owner"; it never approves a tenant erasure
(`e2e_tenant_writes.refuse_any_e2e`). The reconcile lists it as a known E2E identity (A10h). Removal: only Staff ›
Disable removes it at once (row inactive, Cognito disable, global sign-out): Booking and the CRM trust the token's
groups, so a database-only change leaves it working there. The Infra run-script's REMOVE mode is cleanup after Disable
(it refuses an active row); full removal with the A10 family (root ADR 0006a A10h).

**Sign-in (ADR 0006 Phase 1, live on Dev).** Staff sign in with an email one-time code on Cognito WORKFORCE
(pool `us-west-1_bCcFvbSOS`) through managed login on our own domain `https://staff-auth.lumiguides.it.com`
(AA-branded; the prefix `amazoncognito.com` domain tripped a Safe Browsing false positive). `/login` shows
"Sign in with company email" and never redirects by itself.
- The BFF (`frontend/lib/cognito.ts`, `lib/cognitoRoutes.ts`, routes `app/api/auth/{signin,callback}/{cognito,
  cognito-breakglass}`, `app/api/auth/refresh`) runs authorization code + PKCE (state + nonce in the 5-minute
  `cis_oauth_tx` cookie) with a confidential client. Redirect URIs come from a fixed allow-list
  (`aa-cis.lumiguides.it.com`, localhost in development); any other host gets `cognito_host` and no sign-in at all (the
  password form is off since AA-816 S2). Domain, issuer and client ids are code constants with `COGNITO_*` env overrides; the two production client
  secrets come from the task's secret `aa-cis/dev/admin-web` (AA-CIS-Infra `tools/identity/staff_web_secret.py`).
- Cookies: `cis_admin_token` holds the Cognito **access** token (15 min; the cookie lives for the refresh window
  so `proxy.ts` can see it expired), `cis_staff_rt` the refresh token (httpOnly, `path=/api/auth`, 12 h,
  1 h for break-glass; the window is not extended by rotation). Refresh happens only in `/api/auth/refresh`
  (single-flight; `proxy.ts` redirects page navigations there, `SessionKeepAlive` POSTs before expiry).
  `cis_role` / `cis_user` are display hints set by the server, never trusted. Logout (`POST
  /api/auth/admin-logout`, same-origin only — a cross-site POST gets 403, like Booking's) revokes the refresh
  token, clears every session cookie and answers the managed-login `/logout` URL (`client_id` + `logout_uri` =
  `<prod>/login`, registered on both staff clients); the Topbar follows only that URL or `/login`
  (`lib/staffLogout.ts`), so the next sign-in asks for a new email code.
- A sign-in completes only after `/auth/verify-admin` accepts the token; a refusal lands on
  `/login?error=no_access` with no session cookies (AA-1111: a 403 `mfa_required` lands on `/login?error=mfa_required`,
  "sign in again with your password and authenticator code"; `lib/loginErrors.ts`).

**Verification (backend).** `api/core/identity_verifier.py` (app-neutral, to be copied byte-identical into
AA-Booking) + `api/core/identity_config.py` (CIS surface) + `api/core/staff_identity.py` (resolver):
- issuer chosen by the **unverified `iss`**; only the WORKFORCE issuer is accepted for staff (any other `iss` →
  401, a JWT without `iss` → the legacy HS256 path); RS256 only, `client_id` ∈ {`cis-admin`, `breakglass`,
  `cis-admin-preview` only when set through the env (empty by default since the Vercel retirement; capped at viewer,
  below)},
  `token_use=access`, no `aud`, leeway ≤ 60 s; one JWKS cache per issuer, prefetched at start-up
  (`identity_jwks_prefetched`), unknown `kid` refetched at most once a minute, last good keys kept;
- the row is found by `(iss, sub)`; on the first sign-in it is linked **once** by `lower(email)` to an active
  row with no Cognito identity (`email_verified` is trusted from the pre token trigger, which refuses
  unverified or off-domain addresses; WORKFORCE access tokens do not carry the claim, ADR 0006a A7). A row
  already bound to another `sub` is never rebound (401; rebinding is an owner action);
- role = the highest `cis-*` group (`GROUP_ROLES` order; `admin` first, `content` last); no `cis-*` group → 403 even with `booking-*`
  groups; a different DB role is logged as `staff_role_drift`, not denied; inactive or unknown row → 401;
  verifier/JWKS/directory outage → 503. Lookups are cached 60 s per `(iss, sub)`.
- **Administrator MFA (AA-1111 S2, root ADR 0006c, CIS 459; `api/core/staff_mfa.py`).** After the resolver,
  `staff_identity.authenticate` (so both the bearer middleware and `/auth/verify-admin`) checks an administrator
  session (role `admin`, or the owner flag) for an authenticator code: the verified access token's Cognito `amr`
  claim must hold `otp` (password + TOTP gives `["pwd", "otp", "mfa"]`; an e-mail code gives `["email_otp"]`). While
  the platform setting `cis.platform.admin_mfa_required` (Platform › Settings, area platform, maker-checker, no env
  override, default off) is on, a session without it is refused 403 `mfa_required`; off, it is served and logged
  `staff_admin_mfa_check outcome=would_refuse` (`satisfied` when the code was used; one line per identity and
  outcome per 10 minutes). Other CIS roles, the capped Preview viewer and the Dev-only read-only E2E identities are
  never checked. Enrolment and the switch-on order: AA-CIS-Infra `docs/runbooks/staff-admin-mfa.md`.
- `api/middleware/staff_bearer.py` (registered right after the rate limiter) handles every staff path: a
  verified Cognito Bearer **overwrites** `x-admin-role` / `x-admin-user-id` from the verified principal
  (client copies dropped), sets the `current_staff` contextvar and strips `Authorization`; an invalid
  WORKFORCE Bearer → 401; no Bearer or a non-WORKFORCE token → unchanged. The BFF forwards the Bearer on
  `/api/admin/*`, `/api/pipeline/*`, `/api/tenant/pipeline/brand-identity` and the staff prefixes of
  `/api/tenant/*`.
- **Principal-aware secret check (AA-816 C9, `api/core/staff_credential.py`).** On a staff path every admin-secret
  guard (`admin.verify_admin_secret`, `admin_settings.verify_admin`, `v1_pipeline._require_admin`,
  `core/staff_secret.py`, `staff_policy_if_staff`, the staff audit and the rate limiter's admin tracking) accepts a
  request whose Cognito Bearer the middleware verified, **without** `X-Admin-Secret`; POLICY still applies to the
  token's role. The BFF (`lib/auth-server.ts` `staffCredentialHeaders`) sends a Cognito session's Bearer **and no
  secret** on the staff prefixes (`STAFF_API_PREFIXES`, kept equal to `STAFF_PATH_PREFIXES` by a test); a legacy
  HS256 session, and any path outside them (`/api/tour-full` → `/v1/tours/{id}/full`), still sends the secret.
  **Preview credential:** a second secret read from Secrets Manager by the code constant
  `PREVIEW_SECRET_NAME` (no env var; only on the deployed API, `AWS_EXECUTION_ENV`; cached 5 min, a miss 1 min;
  missing, unreadable or equal to the main secret = feature off, never an error). Sent as `X-Admin-Secret` on a
  staff path, it makes the middleware replace `x-admin-role` with `viewer` (no role header → viewer, never the
  machine), so it reads content and is refused on every write and admin-only read; audited as
  `details.auth = preview`. The Vercel Preview BFF held this value until the Vercel retirement (S272; no holder now); the main `ADMIN_SECRET` stays with the
  Production BFF (legacy sessions), the CI deploy gate/smoke and the ops-probe Lambda.

**Legacy password login (off since AA-816 S2, kept as the rollback).** `/auth/admin-login` (HS256 JWT signed with
`JWT_SECRET`) still answers. `LEGACY_ADMIN_LOGIN_ENABLED` gates **only** the CIS UI password form + BFF
`/api/auth/login` (404 when off) and the HS256 path of `/auth/verify-admin` (401 "Password sessions are switched
off; sign in again" when off). **Since S2 it is off by default in both the API and the BFF**: only an explicit
`1` / `true` / `yes` / `on` (case and spaces ignored, same parsing in `api/core/staff_identity.py` and
`frontend/lib/staffSession.ts`, a test keeps the two lists equal) turns it back on; unset, empty or a typo keeps it
off. The deployed API task definition does not set it, so every HS256 staff session is refused on every host,
Preview included; staff sign in with Cognito only. Rollback: revert the S2 commit, or set the flag on the api task
definition (AA-CIS-Infra, hand edit) and on the admin task definition. `/auth/admin-login` is not gated and never calls
`/auth/verify-admin`; the AA-Booking bridge that used it (`/auth/booking-token`) is retired (AA-911, S271).

**Preview staff sign-in — retired (AA-911, S272; root ADR 0006a A11 superseded by A13).** The fixed Preview host
`aa-cis-preview.lumiguides.it.com`, its Vercel project `aa-cis-frontend` and the Preview-only WORKFORCE client
`cis-admin-preview` are gone; `frontend/lib/cognito.ts` has one client (`cis-admin`, plus break-glass) on the production
host. The API keeps the viewer cap for a preview client set through `COGNITO_CIS_ADMIN_PREVIEW_CLIENT_ID`
(`api/core/identity_config.is_preview_client`, `api/core/staff_identity.resolve_staff`: role `viewer`, `is_owner`
false, log `staff_identity_preview_capped`), but the default is empty, so a token of the deleted client is 401. The C9
Preview credential (`aa-cis/dev/admin-secret-preview`, `api/core/staff_credential.py`) has no holder since the
Vercel Preview `ADMIN_SECRET` went with the project; its code and secret are a follow-up removal. Every BFF route that
attaches `X-Admin-Secret` calls `requireAdmin()` first (audited 21/10/2026). For an HS256 session (rollback only, since S2)
`/auth/verify-admin` re-reads `shared.admin_users` (cached 60 s), so a deactivation or demotion lands within
~6 min (BFF verify cache 5 min + 60 s). Cutover preconditions (ADR 0006): every active row has an email and a
`cognito_sub` from that person's own sign-in, managed-login break-glass tested; the secret check is
principal-aware since C9 (above); then `ADMIN_SECRET` is rotated (I6) and the flag goes off. New staff are created on
the Staff page only (the legacy password-row script is retired, AA-816 D1).

**Login throttle (AA-876).** `/auth/admin-login` calls `api/core/login_throttle.py` (so did `/auth/tenant-login` until
AA-1036 made it 410) before the
DB read and bcrypt (bcrypt runs in the threadpool). One pipelined Redis call checks, per source IP and scope: at most
20 **failed** logins per 15 min (`loginfail_ip:*`, successes do not count) and a high ceiling of 100 attempts per 5 min
(`loginrl:*`, protects bcrypt CPU). Admin only, per username (stored as a hash, never in clear): 5 failures from one
IP start an exponential wait for that (username, IP) (30 s doubling, max 15 min; a success clears it), and 50
failures across IPs in 15 min start a 5 min wait for the username everywhere (distributed guessing). Backoff only,
no hard lockout: every key expires by itself. Every 429 has the same body + `Retry-After` and logs
`login_throttled` (scope, reason, IP). Redis missing / failing → fail open (`login_throttle_redis_error`). The IP is
the **rightmost globally routable** `X-Forwarded-For` entry: API Gateway appends the real source and the NLB/ALB hops
are private, so a caller cannot choose its bucket. This depends on the edge being API GW → VPC link (NLB) → ALB; a
proxy in front of API GW or a direct ALB path breaks it. Limitation: through the BFFs (admin, portal, Booking bridge)
the "IP" is a shared Vercel egress address, so an attacker failing through a public BFF can exhaust that bucket for
everyone behind it for up to 15 min (follow-up: a signed forwarded client IP from the BFF, or retiring legacy login).
The BFFs pass a 429 on as a 429 with a readable message (not 502). Cognito sign-in is not affected. The API Gateway
stage also throttles the two login routes (Terraform, AA-CIS-Infra).

**Staff directory writes (AA-816).** `api/services/staff_directory.py`, routes in `api/routers/admin_platform.py`:
create (`AdminCreateUser` with `MessageAction=SUPPRESS` + `email_verified=true`, `@adventure.asia` only, no
password), roles (CIS role + Booking grant, mirrored to `cis-*` / `booking-*` groups, add before remove),
disable (`AdminDisableUser`, then `AdminUserGlobalSignOut`), enable, sign-out ("reset" = re-enable + global
sign-out; staff have no password) and `/sync` (retry). DB first, then Cognito; a failed Cognito step answers
502 "saved, Cognito pending" with an idempotent retry. Every change is audited in `acp_shared.audit_log`.
- `staff.write` = role `admin` **and** a Cognito session (`auth = cognito`; legacy password sessions only read)
  **and** `admin_users.is_owner`. The owner flag is set out of band (`scripts/link_staff_identity.py`), never
  through the API. Writes lock the owner rows and re-read the actor in the transaction; the last active owner
  cannot be demoted or disabled; nobody demotes or disables themselves.
- The break-glass row (`admin@adventure.asia`, username `admin`) is read-only in the UI (403 on every write).
  Break-glass itself is password + TOTP through `/api/auth/signin/cognito-breakglass` (no button), disabled
  between uses; runbook in root ADR 0006a A4.
- The CIS task role (api and worker) may call only the directory allow-list on WORKFORCE (`identity_iam.tf`,
  with an explicit Deny on password/attribute/MFA/auth/delete actions); the writer also refuses other
  operations in code. Residual risk (ADR 0006a A8): IAM cannot stop `AdminCreateUser` with a temporary password
  or `RESEND`; the reconcile job is the detective control.
- New staff get no Cognito mail (SUPPRESS); the staff invitation email is in progress (follow-up).

**Reconcile job.** `staff_directory_reconcile` on `shared.job` (`services/staff_reconcile.py`,
`services/jobs/staff_reconcile_job.py`): report only, `ListUsers` / `ListUsersInGroup` only (no `AdminGetUser`,
it counts as MAU). Reports drift (Cognito users missing from the directory, rows missing from Cognito,
group/role/email drift, rows bound to another sub or issuer, non-`CONFIRMED` users, break-glass state; AA-1155: a
custom role row must be in `cis-custom` alone and a Booking custom grant in `booking-custom`) and
**cutover readiness** (active rows without an email or `cognito_sub`). On demand: Staff page "Check now"
(`POST /admin/platform/staff-directory/reconcile`, read `GET …/health`, `staff.read`); the daily run goes
through the machine route `POST /admin/job-runner/staff-reconcile/schedule` once the ops-probe Lambda calls it
(Infra follow-up). Job results reach non-admin roles redacted. `GET …/health` also returns `readiness_now`:
the readiness recomputed from the directory rows on every read (`staff_reconcile.readiness_from_rows`, no Cognito
call), so it matches the Staff header right after a disable or invite; only the Cognito drift is "as of" the last
run (S243).

The matrix lives in `frontend/lib/rbac.ts` and is read by `frontend/proxy.ts` (pages; the Next 16 Proxy on the Node.js runtime,
`middleware.ts` until S243), the BFF proxies
(`requireAdmin(req, API_RULES.x)` → 401 bad/missing token, 403 wrong role) and the admin nav (an item
shows only when the role may open its page).

**Admin console layout (AA-883, S241 design option A; regrouped by AA-1039 R6 slice 1, S266, root ADR 0019 d13).**
One console (`aa-cis-frontend`). The nav model is plain data in `frontend/lib/adminIa.ts` (`ADMIN_IA`; icons added
in `frontend/app/_kit/adminNav.tsx`), seven groups (d13) plus Trade (d9, AA-1037), each folding on its own (kit
NavGroup, S264; no sections, no footer Settings entry):
- **Home** — Overview, App health (`/admin/platform/apps`), Reports, Data coverage;
- **Tenants** — Directory (`/admin/tenants`, a row opens **Tenant 360**, a route since AA-1039 R6 rest:
  `/admin/tenants/<id>[?tab=cost]`, panel in `tenants/_components/TenantDetail.tsx`; an old `?tenant=<id>` link is
  replaced in the browser by the route), CRM agencies
  (`/admin/tenants/agencies`), Data erasure (`/admin/tenants/data-erasure`), Plans & add-ons
  (`/admin/tenants/plans`, AA-1028 R1 slice 3: the plan catalogue, see "Tenant control plane");
- **AA Catalogue** — Upload (S0), Rewrite (S1), Review Queue, Master Content, Atoms & routes (`/admin/atom-curation`),
  Places, Place cards (`/admin/places/cards`, R6 rest option A: was Content quality's Cards tab; its own page rule =
  admin, content_lead, tenant_qa, before the admin-only `/admin/places` rule; API `/admin/content-quality/place-cards*`
  unchanged), Photos, Facts, Supply copy, AA content, SEO Intelligence (no Spend tab since R6 rest: DataForSEO spend is
  Spend › DataForSEO; `GET /admin/seo-intelligence` no longer sends `spend`), Catalogue settings (`/admin/settings`);
- **Tenant content QA** — Content quality, Content Trace, Review log, Slate (`/admin/tenant-qa/*`, admin only);
- **AI & spend** — AI gateway (`/admin/ai-gateway`, was "AI providers": tabs Providers, Routing incl. "Routing by data
  class", Models & trials, Budgets, Calls, Guide), Spend (`/admin/spend`, R6 slice 3: was Cost Operations at
  `/admin/cost-ops` and Cost per app at `/admin/platform/cost`, now its By app tab `?tab=apps`);
- **Writing Lab** — Stages (`/admin/lab` opens `/admin/lab/a1-s1`; one page per writing-memory writer `a1-s1`,
  `t2-rewrite`, `t9-blog`, `t9-social` with the tabs Eval gate, Samples, Labels, Experiments, Prompt versions, one read
  `GET /admin/writing-memory/lab/{writer}` over the existing learning tables, ids / labels / counts / hashes only, no
  new learning logic: S262 TL-11 freeze unchanged), Writing memory (`/admin/lab/writing-memory`, was
  `/admin/writing-memory`), Eval runs (`/admin/lab/eval-runs`, AA-1169: an evaluation run read by hand, one sample's
  stored output beside the source the judge read with its flagged claims and gate replay, Markdown export; plus the
  lineage reads: coverage, one output, one tour's fan-out), Jev Decisions;
- **Trade** (AA-1037, root ADR 0019 d9 "Screens now") — Booking admin, Agency billing, Agency tiers, B2B desk: pages of
  AA-Booking's admin (`external` items; the URL comes from `lib/apps.ts appPageUrl`; same tab, never the active item),
  shown to `billing.read` or `tenant.book.account.read` (`lib/rbac.ts canSeeNavItem`); Booking checks its own grant;
- **Platform** — Staff & access, Audit log, Jobs, Alerts (Catalogue, Recipients), Settings, Infra limits, API Docs, Data model, Docs,
  UI kit.

Moved pages answer **307** from their old URL with the query string kept (`ADMIN_ROUTE_MOVES`, served by
`next.config.ts` before `proxy.ts`): `/admin/content-quality` → `/admin/tenant-qa/quality`, `/admin/tenant-activity`
→ `/admin/tenant-qa/trace`, `/admin/platform-stats` → `/admin/tenant-qa/review-log`,
`/admin/atom-curation?section=slate` → `/admin/tenant-qa/slate`, `/admin/platform/agencies` →
`/admin/tenants/agencies`, `/admin/data-erasure` → `/admin/tenants/data-erasure`; R6 slice 3: `/admin/writing-memory`
→ `/admin/lab/writing-memory`, `/admin/cost-ops` → `/admin/spend`, `/admin/platform/cost` → `/admin/spend?tab=apps`; R6 rest:
`/admin/seo-intelligence?tab=spend` → `/admin/spend?tab=spend&view=dfs`, `/admin/settings?tab=tenant` → the aa_internal
Tenant 360 `?tab=settings`, `/admin/settings?tab=ui-kit` → `/admin/platform/ui-kit`,
`/admin/tenant-qa/quality?tab=cards` (and `/admin/content-quality?tab=cards`, one hop) → `/admin/places/cards`
(a query of the target wins over the request's, as Next.js merges them). BFF and backend paths keep their
names (`/api/admin/content-quality/*`, `/admin/platform/crm/*`). `frontend/lib/adminIa.test.mjs` checks that every
page URL of the S265 nav still opens a page, nav items never point at an old URL and every admin page has a nav
entry; `tests/e2e/smoke/aa1039-admin-ia-redirects.spec.ts` checks the 307s on the deployed host.
The other apps are links from the typed registry `frontend/lib/apps.ts` (Booking admin, TripPlanner
public site; no tenant-portal link until there is a real support view, since the portal needs a tenant
session); deep links are built only there, never from a return URL, never with a
token. New platform pages live under `/admin/platform/*` (backend `api/routers/admin_platform.py`).

**Data model (AA-894 scope 2).** Developer › Data model `/admin/data-model` (admin only: PAGE_RULES, BFF `API_READ_RULES` "data-model", POLICY `GET /admin/data-model` = `ops.read`; next to API Docs, outside `/admin/platform/*` like it). Router `api/routers/admin_data_model.py`, service `api/services/data_model.py`: reads the ERD model `erd.json` (format 1) that the AA-CIS-Infra nightly DB Schema Dump writes to the bronze bucket at `data-model/erd/latest.json` (+ `previous.json`, the model before the last structural change; prefix `DATA_MODEL_S3_PREFIX`), returns it with a diff summary (tables / columns / keys / FKs added, removed, changed), cached in process 5 min (`?refresh=1` at most every 30 s). 404 when no model is published, 503 when storage fails. No DB query. This repo is public: it ships the reader and the renderer (`frontend/app/admin/data-model/`), never a table list; the names exist only in the S3 object and the admin-only response.

**Docs reader (AA-898; v2 AA-1133).** Developer › Docs `/admin/docs` (**every staff role** since AA-1133 B7, tech lead S283 "all staff after fix": PAGE_RULES and BFF `API_READ_RULES` "docs" = every staff role, POLICY `GET /admin/docs/manifest`, `/page`, `/asset`, `/search` and `POST /admin/docs/feedback` = `docs.read`, held by every role; `GET /admin/docs/feedback` = `ops.read`; the read-only viewer reads but cannot post feedback). **v2 (AA-1133):** the manifest entries carry `nav` / `nav_order` (the list is a topic sidebar: start, guides, operations, architecture, reference, legal and security), `known_issue` (a warning banner), `last_changed` (the file's last commit) and `source.github` (Source / History links for every reader, Edit for admins, only while the repo is public); a page opens with a header (breadcrumb, title, lead, facts), code blocks with a language label and Copy, and a sticky scroll-spy table of contents from 1280 px. **Search:** the root Docs sync publishes `search/index.sqlite` (SQLite FTS5 of every published section, root `tools/docs/docs_search_index.py`); `api/services/docs_search.py` loads it under the same read-only grant after a sha256 check against the manifest, in memory and read only, turns the typed words into quoted prefix terms (AND), ranks with bm25 and drops owner-tier and unlisted pages for the caller; snippets come as `[{text, hit}]` segments. 404 without an index (the list falls back to its title filter), 503 on a hash mismatch or a bad file. **Feedback:** "Was this page helpful?" writes `acp_shared.docs_feedback` (CIS 454; one row per repo, path and staff actor `admin:<id>`, the latest answer and optional note) through `api/services/docs_feedback.py`; the admin view (Feedback button) lists pages by "No" count with the latest notes and counts, never names, pages the admin cannot open; both answer 503 `unavailable` before 454. A reader, never an editor: docs change by PR in git. The root repo workflow "Docs sync" (`.github/workflows/docs-sync.yml`, `tools/docs/build_docs_reader.py`) builds the bundle from a CI checkout of the docs catalog (publication rule v1: catalog pages and ADRs only; journals, audits, security pages and templates never; account and resource IDs redacted, a guard fails the build) and uploads it to the bronze bucket under `docs-reader/` (`manifest.json` + `pages/<repo>/<path>.md`; prefix `DOCS_READER_S3_PREFIX`); the CIS task role may only read that prefix (AA-CIS-Infra `modules/ecs` ReadDocsReader / DenyDocsReaderWrites). Router `api/routers/admin_docs.py`, service `api/services/docs_reader.py`: manifest cached in process 5 min (`?refresh=1` at most every 30 s), page bodies 5 min (64 kept); a page is read only by the `key` its manifest entry names, after a shape check, never by a caller path. Owner-only entries (`access: owner`; any other value counts as owner) are listed and served only to a principal holding `docs.owner_read` (`staff_authz.can_read_owner_docs`: `auth = cognito` and `is_owner` (N-3); TEMPORARY, Dev only, the A10b E2E CIS admin, `is_owner` still false); for anyone else they do not exist (404). Translations (root rule 8): entries carry `lang`, `translation_of`, `translations`; links are re-checked both ways, a copy of an owner page is owner, the list shows English pages and the reader has an EN / VI switch (remembered per browser). 404 when nothing is published, 503 when storage fails. Rendering: the CIS-local kit file `frontend/app/_kit/MarkdownView.tsx` (react-markdown + remark-gfm, raw HTML dropped, images not loaded except the bundle's SVGs below, links resolved by the page: published page → reader, https/mailto → new tab, anything else → plain text; GitHub callouts via `lib/markdownCallouts.ts`; ```mermaid blocks drawn by `app/_kit/MermaidDiagram.tsx`, lazy, strict, DOMPurify-sanitised, self-contained SVG only, source on error); helpers in `frontend/lib/docsReader.ts`. **SVG images (AA-697, root rule 9):** the manifest may list `assets` (SVGs
under `docs-reader/assets/<repo>/<path>.svg`, the architecture diagrams); `GET /admin/docs/asset?repo=&path=` (POLICY
`ops.read`) serves one only when the manifest lists it for the caller, re-checks it on every read
(`docs_reader.svg_is_safe`: no script, `foreignObject`, other active element, event handler, `javascript:`, CSS `url()` /
`@import` or entity; every href a fragment or an inline PNG; no 12-digit run outside the inline PNGs; else 503, never
served) and sends it as `image/svg+xml` with a sandbox CSP + nosniff, which the BFF re-adds (`lib/upstreamHeaders.ts`).
The manifest response lists `assets` as `{repo, path}` (no key). A page's `![alt](relative.svg)` that resolves to a listed
asset (`resolveImage` in `lib/docsReader.ts`) is drawn by `MarkdownView` as `<img>` in a white frame linking to the
full-size image; every other image stays alt text. **Screenshots (AA-1064, root rule 10):** `assets` may also list PNG /
WebP files (`type` must match the suffix), the screenshots a published page links; tier = the most open tier of the
linking pages. The route re-checks the 300 KB cap (`MAX_RASTER_BYTES`) and the signature on every read (else 503) and
sends the type of the suffix (`docs_reader.asset_type`) with the same image headers (5 min private cache). The REST API
Gateway in front has no `binaryMediaTypes` and would corrupt binary bodies, so the admin BFF asks
`?encoding=base64` (JSON `{repo, path, type, bytes, sha256, data}`) and sends the decoded bytes itself
(`frontend/lib/docsAsset.ts`, branch in `app/api/admin/[...path]/route.ts`; length, sha256, type and signature checked).
The AI gateway Guide tab shows its page's images too, through its own route: `GET /admin/ai-gateway/guide` lists in
`doc.assets` the staff-tier bundle images the guide page or its translation links (`docs_reader.linked_images`, resolved
like the builder and `resolveImage`), and `GET /admin/ai-gateway/guide/asset?path=` (spend.read like the tab, so
finance and ai_ops see them without ops.read) serves only a path in that list, never the rest of the bundle, nothing when
the guide is owner tier (`ai_gateway_guide.read_asset`); the BFF decodes it like docs/asset, the tab passes
`guideImage` (lib/aiGatewayGuide.ts) to `MarkdownView`. This repo is public: no page content lives here.

**Product map (AA-1106).** Platform › Product map `/admin/platform/catalogue` (admin only: PAGE_RULES `/admin/platform`, BFF `API_READ_RULES` "platform", POLICY `GET /admin/platform/catalogue` = `ops.read`). A reader of the root repo product catalogue (`docs/catalogue/features.yaml` + `integrations.yaml`: every app's features with their pages, API route groups and enabling conditions — plan feature keys, settings registry keys, AI gateway connections, secret names, tenant kinds —, every link between the apps and to outside providers with its contract, auth, conditions, cadence, failure behaviour and kill switch, and the business journeys). The root guard `tools/catalogue/catalogue.py` (workflow `Catalogue`) keeps it in step with the code: every page and router of this repo is in its surface snapshot and must map to a feature, so **a new admin page or router needs a catalogue row in the root repo** (the nightly surface job turns red otherwise). The root "Docs sync" workflow uploads the checked bundle as `docs-reader/catalogue.json` next to the docs manifest; router `api/routers/admin_product_catalogue.py`, service `api/services/product_catalogue.py` read it under the same read-only prefix grant (no table, no write path), cache it like the docs manifest (5 min, `?refresh=1` at most every 30 s), keep only the known sections and rows with an id, and mask any 12-digit run. 404 when nothing is published, 503 when storage or the format fails. Page `frontend/app/admin/platform/catalogue/` (tabs Apps / Features / Links / Journeys / Graph, URL filters by app, status, role and condition kind, drawers; Vietnamese names from the bundle's `name_vi` / `summary_vi`), helpers `frontend/lib/productCatalogue.ts`. A setting named in a condition links to Platform › Settings, which opens `?app=&area=`.

**Delivery (AA-1108).** Platform › Delivery `/admin/platform/delivery` (admin only: PAGE_RULES `/admin/platform`, BFF `API_READ_RULES` "platform", POLICY `GET /admin/platform/delivery` = `ops.read`). CI/CD and DORA per app (CIS, Booking, Partner CRM, TripPlanner, Infra, root) on Dev: the five DORA keys (deployment frequency, lead time, change failure rate, failed deployment recovery time, rework rate) each with its sample size n, the lead-time segments (open → first green CI → merge queue → merged → deployed → verified), CI health per workflow (p50 / p95, failure rate, re-runs, minutes), the main branch state, merge-queue wait and ejections, flaky jobs, deploys and failed deploys with recovery time, a 12-week trend, links to every GitHub run, commit and pull request. Source: the root workflow "Delivery metrics" (`tools/delivery/delivery_metrics.py`, hourly, the root run's `GITHUB_TOKEN` over the six public repos, raw events kept 400 days) uploads `docs-reader/delivery/summary.json`; `api/services/delivery_metrics.py` reads it under the docs-reader prefix grant (no table, no write path, no GitHub token here), caches it like the docs manifest (5 min, `?refresh=1` at most every 30 s, the page's Refresh button), keeps the known keys and apps with an id, masks any 12-digit run, and colours each metric green / amber / red ("few" under the minimum n) with the settings of area `delivery` (CIS 433, keys in `services/reports/delivery_settings.py`, starting at the DORA "high" band). 404 when nothing is published, 503 when storage or the format fails. Router `api/routers/admin_platform_delivery.py`; page `frontend/app/admin/platform/delivery/` (overview cards + one tab per app, `?app=<id>`), helpers `frontend/lib/deliveryMetrics.ts`. The definitions (deploy, change, failed deploy, recovery, rework, environment) are the root guide `docs/guides/delivery-metrics.md` (`GUIDE-12-delivery`).
**Delivery time range (AA-1135).** One range for every tab (`?range=7d|28d|90d|custom&from=&to=` on the page and the API). Presets come from the collector (`ranges.d7|d28|d90` per app in the same summary). A custom range of whole UTC days is recomputed from the collector's day files (`docs-reader/delivery/events/yyyy/mm/dd.ndjson`, read only) by `api/services/delivery_range.py` in a child process (`api/services/delivery_range_worker.py`; the store is about 50 MB of events, so the memory never stays in the API process) with `api/services/delivery_engine.py`, a **byte copy** of the root `tools/delivery/delivery_engine.py` (lock `api/services/delivery_engine.lock.json`; never edit it here, sync with the root `tools/delivery/sync_engine.py`, which also writes the parity fixture `tests/unit/fixtures/delivery_range/`). Custom ranges pause (409) while the published `definitions.engine_sha256` differs from the copy, and refuse (409 `out_of_step`) day files that do not rebuild the summary's store (`definitions.store_fingerprint`); 422 for a reversed range, one longer than `cis.delivery.custom_range_max_days` (CIS 450) or outside the stored history; results cached per range until the next summary (`cis.delivery.custom_range_cache_entries`). Cold path (S284): the child starts from a boot that never imports the app (`delivery_range._BOOT`, an empty `api` package in place of `api/__init__.py`); the window-independent work (day file reads, fingerprint check, every app's context) is done once per summary and kept as a snapshot in a private temporary folder of the API process, prepared in the background when a preset is served (`delivery_range.warm`), so a custom range of a prepared summary only cuts its window (no S3 read); a summary found out of step is remembered and refused at once. Benchmark: `scripts/aa1135_delivery_range_bench.py` against a local copy of the store. The page adds a DORA trend chart, the delivery flow diagram (median / p95 per step), merge-queue charts (wait histogram, ejection reasons, weekly ejection rate), a churn treemap and card sorting (`?sort=`).
**Compliance readiness (AA-1170, phase 1).** Platform › Compliance `/admin/platform/compliance` (capabilities `compliance.read` / `compliance.write` in `staff_authz`, administrator only at first, a custom role may hold them; PAGE_RULES and BFF `API_READ_RULES` "platform/compliance" before the "platform" rules). Schema `compliance` (CIS 466, CIS-owned, `cis_app` DML): `requirement` (every requirement of ISO/IEC 27001:2022, SOC 2 TSC common criteria, ISO/IEC 42001:2023, OWASP Top 10 2021, OWASP Top 10 for LLM 2025 and the EU AI Act deployer articles, as identifiers with our own short labels; the standards' text is copyrighted and never stored), `control` + `requirement_mapping` (the common controls and what they cover, full / partial; **never committed**: loaded from the private register by `scripts/compliance/load_register.py`, DRY RUN by default, run by the orchestrator as `cis_app`; `scripts/compliance/register.example.json` is an invented example), `evidence` (append-only by trigger: one row per published check result) and `score_snapshot` (one row per UTC day and framework). Evidence, phase 1: the root workflow "Compliance evidence" (`tools/compliance/collect.py`, daily, about ten checks with the run's `GITHUB_TOKEN` and the root registers) uploads `docs-reader/compliance/evidence.json` with the Docs sync role; `api/services/compliance.py` reads it under the docs-reader grant when the page loads (cached 5 min; Refresh = `POST /admin/platform/compliance/refresh`, compliance.write), cleans it (no 12-digit run, no ARN, GitHub links only, 8 KiB detail) and ingests it (unique `(source, check_id, collected_at)`). Score (`api/services/compliance_score.py`, pure): Evidenced % per framework, strict (met / all requirements) and weighted (+ partial x `cis.compliance.partial_weight`); a check counts only when its newest evidence passes and is within the validity window of its source read **at score time** (`cis.compliance.validity_hours.<source>`, so a shorter setting turns evidence stale at once), its collector was heard within `cis.compliance.collector_heartbeat_hours`, and its published fact passes the setting (drill age, access review age, overdue docs). The S286 hand scores are a reference line (`cis.compliance.reference_scores`). Settings: area `compliance` (CIS 466; keys in `api/services/compliance_settings.py`). The Object Lock evidence store, the AWS / DB collectors and the read-only GitHub App are phase 2. Page `frontend/app/admin/platform/compliance/` (Overview, Controls + drawer), helpers `frontend/lib/compliance.ts`; the banner says "Readiness score (Dev environment), not a certification".

**Privacy obligations (AA-1174, CIS 471).** Schema `compliance` is generic over frameworks: `framework` (every framework,
kind `standard` or `law`, `scored`, display name, jurisdiction, citation, official source, `in_force_on` (entered into
force) and `applies_from` (when its duties apply, if later); the six of 466
are seeded, the EU AI Act as a scored law so its card stays; the old framework CHECKs became foreign keys) and
`applicability` (the matrix market x data segment `traveller` / `agency` / `agency_client` / `staff` x law: the test,
the threshold, the verdict `applies` / `not_yet` / `not_applicable`, a primary-source link, the assessment date, a
counsel flag). Law obligations are rows of `requirement` under a law framework with `counsel_review`, `applies_as`
(controller / processor / both), `source_url` and `doc_url` (our own GitHub docs page). Only scored frameworks get a
percentage and a snapshot (`compliance.py` reads the list from the table; names and kinds reach the page as
`framework_info`). Laws are a checklist: an obligation is met (its requirement met on fresh passing evidence),
not yet checked (unmapped, or only controls without an automatic check), else not met (`partly` when partial)
(`compliance_score.obligation_state`). Tab Obligations: `GET /admin/platform/compliance/obligations`
(`api/services/compliance_obligations.py`; compliance.read; syncs the published file first) and
`frontend/app/admin/platform/compliance/ObligationsTab.tsx` (the matrix with Counsel and Review due badges, one checklist
per law, source links only to official hosts `safeSourceLink`, fixed note "Readiness, not legal advice"). Law rows, their
obligations and the matrix come from the private register through `load_register.py` format 2 (law frameworks only;
a law no longer listed is set inactive and its obligations not applicable, never deleted; sources on an official-host
allowlist; no "compliant" claims; every format-2 section is required (a missing key is refused, `[]` is explicit);
a guard refuses turning off more than 5 rows of any section without `--allow-deactivate`).
Evidence: the root collector adds six `legal_*` checks (the `docs/legal` pages of each document, fact `overdue` against
each page's own review cycle, plus their status; a page still in a draft status fails the check) and
`last_erasure_drill`; the API collects two sources itself, **only
on Refresh** (POST, never on a GET, so a read-only view-as session writes nothing) and at most every 10 minutes per
source: `cis_db` (`api/services/compliance_db.py`: `retention_runs_recent` = oldest of the newest retention runs per
kind, `retention_live_enabled` = oldest of the newest LIVE runs per kind, graded on its age) and `crm_ops` (`api/services/compliance_crm.py`, crm.ops.v1 1.18
`GET /privacy-evidence` with the staff member's own token: `campaign_consent_enforced` = sends without a yes (held `send_uncertain` sends count as sent; no send at all in the
window is a `manual` row, never a pass),
`consent_capture_present` = newest consent decision; a 401 / 403 writes no evidence row). The heartbeat applies to the
scheduled source only. Settings: ten more `cis.compliance.*` keys (validity of the two sources, legal pages overdue,
erasure drill age, retention run age and live run age, consent capture age, campaign violations and window,
applicability review age). Refresh evidence reports its counts (toast and a status line): success only when the file
was read and every source collected; nothing published or a skipped source is a warning with a plain reason.

**Compliance phases 2-3 (AA-1173, CIS 479).** *Sealed evidence:* the collectors (root `compliance-evidence.yml` daily,
AA-CIS-Infra `compliance-aws.yml` nightly AWS read-only checks and weekly Prowler) assume the role
`aa-cis-<env>-compliance-evidence` and write write-once objects into the Object Lock evidence bucket (AA-CIS-Infra
`accounts/aa365/compliance_evidence.tf`: GOVERNANCE, retention a variable, bypass / retention change / delete denied to
every principal, a PutObject without `If-None-Match: *` refused): `evidence/<source>/bundles|raw|manifest/<yyyy>/<mm>/<dd>/`,
one manifest per run that lists the SHA-256 of every object and links the previous manifest of the source (hash chain;
writer: root and Infra `tools/compliance/seal.py`). `api/services/compliance_store.py` reads the newest manifest of
`root_daily`, `aws_nightly` and `prowler_weekly` (this month, else last month), checks the bundle and the chain link
(a mismatch = state `broken`, nothing ingested, error log) and the rows carry `object_key` / `object_sha256`; the
docs reader file is still read after it (unique key: no duplicate). Bucket name: `COMPLIANCE_EVIDENCE_BUCKET`, else
`aa-cis-dev-compliance-evidence-<caller account>` (no ID in the repo). *DB checks:* job kind `compliance_db_checks`
(`services/jobs/compliance_db_checks_job.py`, daily from 03:00 UTC via the ops metrics sampler, switch
`COMPLIANCE_DB_CHECKS`, background, deploy-gate exempt, $0) runs `compliance_db.collect`: the retention checks plus
`db_rls_policies` / `db_grants` (the X2 RLS catalogue rules over the LIVE catalog of every app schema, pg_catalog reads
only; `shared/compliance/rls_catalogue_rules.py` is a byte copy of `tests/integration/rls_catalogue_rules.py`, a unit
test keeps them equal) and `monthly_review_recent`. `cis_db` is now a scheduled source (heartbeat); `prowler_weekly` is
graded by its validity window only. *Management layer:* `compliance.exception` (subject control / check / vulnerability,
reason, compensating control, risk, expiry; maker-checker: the proposer approves only while
`cis.settings.two_person_approval` is off, recorded `self_approved`; the expiry is at most
`cis.compliance.exception_max_days`, checked at proposal and approval; transition-only trigger) and `compliance.review`
(one Security posture monthly review per month: the page's numbers + notes; append-only). An approved, unexpired
exception never raises a score: the control keeps its state and shows "Accepted risk". Routes
`GET|POST /admin/platform/compliance/exceptions`, `POST …/exceptions/{id}/approve|reject|revoke`,
`GET|POST …/reviews` (compliance.read / .write; writes need a verified Cognito sign-in, not E2E, not view-as). Tabs
Exceptions and Reviews (`GovernanceTabs.tsx`, `governance.messages.ts`). Settings (CIS 479): validity of `aws_nightly`
and `prowler_weekly`, `prowler_critical_high_max`, `exception_max_days`, `review_max_age_days`.

**Platform apps, audit and cost (AA-884).** Router `api/routers/admin_platform_ops.py` (read only, admin-only POLICY
entries; no deploy/scale button, no AWS/Vercel/GitHub write token):
- `GET /admin/platform/apps[/{app_id}]` (`ops.read`) — `api/services/platform_apps.py` `collect_apps()`: one card per
  app from the backend registry `api/core/app_registry.py` (code constants). CIS: API SHA + task revision, live
  workers' SHA (parity), the deploy gate (`shared/jobs/worker.py` `deploy_gate_status`, same as the CI guard), schema
  readiness. Booking: HTTP `/booking/health` + `/booking/health/ready` (2 s). TripPlanner: no health endpoint yet;
  today's customer-facing LLM spend (TP `ONLINE_STAGES`, `quality_signal->>'app' = 'tripplanner'`) vs the mirrored
  default cap (5 USD; the live cap is a TP env var). Partner CRM (AA-936, AA-Partner): HTTP `/crm/health` +
  `/crm/health/ready` on the partner host, like Booking. Cached 60 s in Redis, built only on a page view (no poller).
  No ECS/Lambda describe calls, no cross-schema reads (CIS never reads schema `crm`).
- `GET /admin/platform/apps/crm/jobs` (`ops.read`, AA-936) — `platform_apps.crm_jobs()`: the CRM's `crm.job` per kind
  from the CRM contract `crm.ops.v1` `GET /crm/ops/v1/jobs/summary`, called with the signed-in staff member's own
  Cognito Bearer (the CRM's OPS route class accepts WORKFORCE `cis-admin` tokens only; a legacy session gets
  `cognito_session_required` and no call). Not cached (per person). The cost page has a `crm` row from the App tag.
  The token comes from `platform_apps.relay_authorization(request)`: StaffBearerMiddleware drops the Authorization
  header after verifying it and keeps the token in `scope["state"]["staff_access_token"]` (AA-941; never logged).
- `/admin/platform/crm/*` (AA-941, CRM-13a S-01; GET `tenant.read`, writes `tenant.write`) —
  `api/routers/admin_crm_console.py` + `api/services/crm_console.py`: the partner CRM's AA L1 console (agency list and
  meters, create agency, lock / unlock, first-owner invite, allowed features, insights opt-out, AA-raised data
  requests, owner recovery, member disable, failed-job retry; S263 crm.ops.v1 1.8: withdraw the first-owner invitation,
  delete a synthetic test agency with no member or traveller, typed name + ticket). A relay only: each route mirrors one `crm.ops.v1` route
  with the staff member's own Cognito token, inside a path allow-list (`ALLOWED`); the CRM's status and body pass
  through (it decides crm-platform-admin writes and filters L1 keys); bodies are never logged (the owner invite
  carries an e-mail). Page `/admin/tenants/agencies` (nav Tenants › CRM agencies; was `/admin/platform/agencies`, AA-1039). AA-1031 (S264): the Create
  agency picker leaves out CIS tenants that already have an agency in the loaded list, and the CRM's 409
  `tenant_deleted` (crm.ops.v1 1.9: the id of a deleted test agency, never reused) reads "create a new CIS tenant".
  When the agency id is an active CIS tenant and the staff member has `tenant.write`, the drawer's "Invite first owner"
  offers "Also invite to the Content studio as owner" (ticked by default): after the CRM invitation is sent, the page
  calls the Tenant 360 route `POST /admin/tenants/{id}/members/invitations` (role owner) with the same address — a
  staff action, so no CRM event grants a CIS role (root ADR 0010a) — and keeps both results in the section.
  **One health definition (S243):** a card's `status` is `overall.status` from `api/services/app_health.py`
  `overall_status(probe, traffic)` — the worst known signal of the app's own check (`probe`; none for TripPlanner) and
  its CloudWatch traffic of the last 24 h (`traffic_section`, the AA-858 health section and its Redis key, shared by
  both pages). A health check that does not answer counts as "degraded" (basis "Health check (did not answer)");
  firing CloudWatch alarms are "degraded", not "down" ("down" = a health endpoint answering with an error). `/admin/overview/ops` copies each card's `overall` onto its health tile, so Overview and Apps always
  show the same status, in the same words (`frontend/lib/platform.ts` `APP_STATUS_LABEL`). Do not judge app health
  another way on a new page.
- **Staff audit trail.** `acp_shared.audit_log` is append-only since CIS 248: UPDATE/DELETE/TRUNCATE raise 42501;
  a BEFORE INSERT trigger chains every new row (`chain_seq`, `prev_hash`, `row_hash` = sha256 of a canonical text,
  under `pg_advisory_xact_lock(884, 1)` — so a writer inside a transaction writes its audit row LAST; needs READ
  COMMITTED). `api/middleware/staff_audit.py` (inside `StaffBearerMiddleware`, outside the rate limiter) adds one
  `staff.request` / `machine.request` row per non-GET call on a staff path that carries a staff credential (the admin
  secret, a verified Cognito Bearer, or the Preview credential, AA-816 C9): actor
  from the verified Cognito identity, else `x-admin-user-id` marked `details.auth = legacy_unverified`, method,
  route template, status (denied calls included); never the body or query values. `GET /admin/platform/audit`
  (`audit.read`, filters + keyset cursor, staff scope by default) and `GET /admin/platform/audit/verify`
  (`audit.read`; Python recompute of the chain, `api/services/platform_audit.py`, must stay equal to the trigger;
  a row without `chain_seq` created after 248 was applied — a trigger bypass — also makes it `ok: false`).
  Since X1 the runtime `cis_app` cannot touch the triggers (S18, not the owner); the owner `cis_owner` (only through a
  reviewed DB Migrate file) and the break-glass RDS master still could, and the chain makes such an edit detectable,
  not impossible. Integration tests delete their own audit rows only through
  `tests/integration/_audit_cleanup.py`.
- **Cost per app** — the one cost module of the console: `api/services/platform_cost.py` `cost_by_app()` (AA-858's
  overview card imports it; do not compute spend another way). Row labels/hints (`ROW_LABELS` / `ROW_HINTS`) and
  `ai_outside_aws_usd` (= AI − AI already on the acc2 bill) come from this module, so both pages show AWS bill + AI
  outside the AWS bill = Total with one wording (`frontend/lib/costWording.ts`); the Cost page's KPI strip shows the
  deduction (S243). `ai_spend_by_provider()` gives the same month's AI per provider family (all `bedrock*` = bedrock;
  `typesafe`, how Jev decisions are logged, = jev)
  for the legacy `/admin/overview` provider table. AWS = acc2 Cost Explorer grouped by the `App` tag
  (`shared.cost_explorer_app_snapshot`, CIS 248; untagged is its own row with services), AI = LLM gateway estimates
  by `quality_signal->>'app'` from `shared.llm_call_log` + `shared.llm_call_daily`; acc2 Bedrock is subtracted from
  AI so it is not counted twice; acc3 Bedrock CE actuals are a cross-check line (`llm_satellites`; a `retired` entry
  only for a month with acc1 history, AA-998). `GET /admin/platform/cost`
  (`spend.read`) refreshes a month whose snapshot misses days or is > 24 h old (and may still change) with one CE
  request (~$0.01) behind a Redis lock, at most once a day per window (a Redis attempt marker, also after an empty
  or failed answer); `POST /admin/cost-explorer/check` (the "Refresh from AWS" button) also
  fetches the App tag (+1 request). No scheduler yet.
  **One spend truth (AA-1127 C2):** the provider families of `platform_cost.provider_family` (`anthropic` is its own
  family) are read by the Spend forecast, the FI-02 weekly report and `ai_spend_by_provider`; the AI gateway Providers
  read returns that total as `totals.llm_month_usd`. A Bedrock row's account follows `cost_ops.ledger_account` (acc2,
  acc3, a live registry satellite, else `retired`) in the forecast split and in FI-02, and `ce.account_key()` names a
  live satellite's Cost Explorer rows by its key, so the forecast and the AWS bill tab show it as its own account.
- **Alerts catalogue (AA-1171, S287 decisions D1–D7).** Platform › Alerts (`/admin/platform/alerts`, tabs Catalogue —
  the default — and Recipients) is the one full list of what the platform watches: one row per alert condition
  (CloudWatch alarm, EventBridge rule that notifies a topic, budget / cost anomaly alert, CIS / Booking / CRM in-app
  check) with app, component, type, severity (`page` / `ticket`, the Infra `Severity` tag), condition and threshold,
  where it is changed (`settings` / `terraform` / `none`; D2: Terraform stays the only writer of AWS alarm
  thresholds, the page links the Infra file), channel, runbook, owner role, live state, freshness and a coverage gap
  note, plus counts and the known gaps. Service `api/services/alerts_catalogue.py`, router
  `api/routers/admin_alerts_catalogue.py`: `GET /admin/platform/alerts/catalogue` and `GET /admin/platform/alerts/
  summary` (the Ecosystem status card; same document, no rows), both `ops.read`. Sources: DescribeAlarms +
  `tag:GetResources` + `events:ListRules`/`ListTargetsByRule` (cached 5 min in Redis; no Redis = no AWS call); the
  CIS checks of `shared/ops_checks.py` with their `cis.ops.*` values and last bell; Booking `GET /booking/ops/checks`
  and CRM `crm.ops.v1 GET /ops-checks` with the staff member's own Cognito token (no cross-schema read); acc2
  budgets from the Platform › Infra read of the Terraform docs output; acc1 / acc3 / management account budgets
  static (D7), account IDs at run time (`shared/aws_client/accounts.py`, env `AWS_MGMT_ACCOUNT_ID`). Overview ›
  Apps health groups alarms by the `App` tag (name prefix as fallback) and shows the account total per type
  (`alarm_totals`). Notes: `docs/implementation-notes/AA-1171.md`.
- **Alert recipients (AA-913).** Who gets the platform alert e-mails, chosen on the admin page instead of a
  Terraform variable. One list for everything: the CIS api manages the **e-mail subscriptions of the SNS alerts
  topic** (CloudWatch alarms of CIS, Booking, TripPlanner and shared; security EventBridge rules; cost anomalies;
  AWS Budgets, which notify the topic since the AA-913 Infra change), and `ops_alert_email` mails the confirmed
  rows. Table `shared.alert_recipient` (CIS 251: e-mail, note, `pending`/`confirmed`/`removed` +
  `removed_reason` `admin`/`unsubscribed`/`expired`, the subscription ARN — never returned to the browser —, who /
  when; rows are history, `cis_app` has no DELETE). Env from Terraform on api + worker: `ALERTS_TOPIC_ARN` and
  `ALERT_BOOTSTRAP_EMAIL` (the one Terraform-managed subscription, `var.alert_email`: shown **locked**, never stored
  or changed by the app, so alarms still reach someone if the app or the DB is down); either unset = page read-only
  "not configured", writes 503 (fail closed: no unsubscribe path can then tell the bootstrap apart). Every
  unsubscribe checks the SNS endpoint against the bootstrap, and the Infra grant denies `sns:Unsubscribe` on its ARN. Code: `shared/alert_recipients.py` (e-mail rule, SNS calls in a thread with 3 s/8 s timeouts,
  pure `plan_reconcile` + `reconcile`, `ops_recipients`), `api/services/alert_recipients.py` (page, add, remove,
  resend, test e-mail, rate limits from `acp_shared.audit_log`), router `api/routers/admin_alert_recipients.py`:
  `GET /admin/platform/alert-recipients[?refresh=true]` (`ops.read`; reconciles with SNS first, throttled 60 s in
  Redis), `POST …` add, `DELETE …/{recipient_id}`, `POST …/{recipient_id}/resend`, `POST …/test-email`,
  `POST …/refresh` (all `alerts.write`). Add / remove / resend / test also need a **Cognito session** (checked in the
  router; the topic carries security alerts), refresh does not. AWS sends and validates the confirmation e-mail
  (no token handled here). Reconcile: SNS confirmed -> `confirmed`; gone after confirmation -> `unsubscribed`;
  never confirmed (AWS drops it after 3 days) -> `expired`; an address removed by an admin whose confirmation was
  clicked later is unsubscribed again — the api's ops metrics sampler reconciles every 5 minutes, so that happens
  without a page view; an address removed while still pending is listed as "leaving" until AWS drops it; other topic
  subscriptions are listed read-only ("unmanaged"). A 429 carries `retry_after_s` in the body (the BFF forwards only
  Content-Type). Limits: 10
  active, 10 adds / 24 h, resend 1 per 10 min and 3 per 24 h per row, test e-mail 1 per 5 min (429 + Retry-After).
  IAM (AA-CIS-Infra): Subscribe (e-mail protocol), ListSubscriptionsByTopic, Unsubscribe, GetSubscriptionAttributes
  on that topic only; no `sns:Publish`, so the test e-mail tests the SES path and SNS delivery is checked with
  `aws cloudwatch set-alarm-state`. Page `/admin/platform/alerts?tab=recipients` (Platform › **Alerts** ›
  Recipients). AA-1171 (D5): a second, page-severity topic (`PAGE_TOPIC_ARN`); each row names its `topic` (CIS 469:
  `alerts` = every alert, `page` = page-severity alerts only), reconcile runs per topic, the bootstrap lives on the
  alerts topic and is refused on both, and the CIS ops e-mails go to the `alerts` rows only. Notes:
  `docs/implementation-notes/AA-913.md`, `docs/implementation-notes/AA-1171.md`.
- **Pages** (admin only, kit + react-query, no polling; types in `frontend/lib/platform.ts`, links from
  `frontend/lib/apps.ts` `APP_REGISTRY`): `/admin/platform/apps` (nav group **Apps**; one card per app, Refresh =
  `?refresh=1`), `/admin/platform/audit` (People & access › **Audit log**; server filters, keyset "load older", chain
  badge, Legacy badge for `legacy_unverified` rows, link to Booking's own audit log) and Cost per app (since AA-1039
  R6 slice 3 the By app tab of AI & spend › **Spend**, `/admin/spend?tab=apps`, `?month=YYYY-MM` opens a month; month picker, "Refresh from AWS" = `POST /admin/cost-explorer/check` for the month,
  ~$0.04). Notes: `docs/implementation-notes/AA-884-ui.md`.

**Ops overview (AA-858).** `GET /admin/overview/ops` (`ops.read`, not a machine route; the CI smoke keeps
`GET /admin/overview`) feeds the business view at the top of `/admin/overview`. Package `api/services/ops_overview/`,
four independent sections, each cached in Redis on its own key (`admin:overview:ops:<section>:v1`) and each degrading
to `{status: "unavailable", reason, message}` instead of failing the call:
- `health` (`cloudwatch.py`, 5 min): one `GetMetricData` call (16 metrics: API Gateway Count/5XX/4XX/Latency p95 for
  `aa-cis-<env>-api` and `aa-booking-<env>-api` with `Stage = <env>`, Lambda Invocations/Errors/Throttles/Duration p95
  for `aa-tripplanner-<env>-browse|assembly`) + `DescribeAlarms` mapped by name prefix (`TargetTracking-*` skipped).
  Partner CRM (AA-1127 C3) is a `probe` source: no metric query (its API is behind the ALB), alarms `aa-crm-<env>-`
  only, numbers `null`; its tile's status is its own health check (`platform_apps._crm_card`) plus those alarms;
  read-only IAM on the task role (AA-CIS-Infra `ecs_task_infra_status_readonly`). Never called without Redis
  (`cache_offline`); a `SET NX` lock lets one request compute while the others get the last good value.
- `tripplanner` / `booking` (`activity.py`, 5 min): only the producer contract views above + `shared.llm_call_log`
  stage `tp_search_embed` for AI searches (cache misses only). A missing view or grant → `contract_view_missing`.
- `cost` (`cost.py`, 15 min): built only on AA-884's `platform_cost.cost_by_app` (this month + last month), relabelled
  for a business reader; no CE code of its own.
Cost: ≈ $0.0002 per health refresh, worst case ≈ $0.60/month; CE as AA-884. No new env var (names derive from
`ENVIRONMENT`).
**Data coverage (AA-992).** `GET /admin/data-coverage` (`ops.read`, admin only, not a machine route) feeds
`/admin/data-coverage` (Platform › Overview): ≈ 140 rows in 9 stages (inputs, raw, S1 / Master, A-series, places,
T-series, TripPlanner, Booking, CRM), counts only. Package `api/services/data_coverage/`: `defs.py` (the live-tour,
active / place atom and served-destination fragments of the S252 audit), `queries.py` (one statement per source),
`registry.py` (sources + rows: kind must / backlog / info, polarity coverage / defect / threshold / equal, expected
gap, drill link), `sources.py` (each CIS statement in its own READ ONLY transaction, `statement_timeout` 4 s,
`lock_timeout` 1 s, `wait_for` 5 s, 3 at a time; the `catalog.*` views as `aa_app_user` through
`catalog_http.reader.catalog_connection`, one statement per connection, the four catalog sources one after another
(`Ctx.catalog_lock`); S3 `ListObjectsV2` of `raw-inbox/<AA>/` and
`brand-identity/`, ≤ 10 pages; the CRM over `crm_console`), `build.py` (tiers on `ops_overview.cache.cached_section`
under `admin:data-coverage:*`: fast 60 s, slow 600 s, CRM 600 s on its own key and only with a Cognito token; last
good 24 h; `?refresh=1` recomputes the fast tier only when older than 15 s). A failing source turns only its rows
`na` with the reason (a tier past its 12 s deadline keeps the sources that finished; only the pending ones are
`timeout: time_budget`); only a final answer is `unavailable` (`contract_view_missing` for an owner view not applied
yet, `cognito_session_required`, no tenant pool), a busy catalog gate or any CRM non-200 is `error` (cached 60 s,
never the last good copy). $0: no model call, no DFS.
**Ecosystem status (AA-1140, S289).** `/admin/overview` is the Ecosystem status page (S285 advisory, mockup v2):
one verdict and its Needs attention list, app tiles, golden-signal charts with deploy ticks, alarm bands, warn /
critical lines and headroom, the services grid with an uptime strip against the objective, shared resources, job
queues and the deploy gate, cost of the month, AI gateway spend against caps, the event timeline, delivery, sources
and freshness. ONE read, `GET /admin/overview/status` (`ops.read`; backend `api/services/ops_status/`, the 5-minute
sampler job `ops_status_sample` writing `acp_shared.ops_sample` / `ops_event`, CIS 475); every section has its own
`as_of`, a signal with no source is "not measured yet" (never green), every limit is a `cis.ops.status_*` setting
(`shared/ops_status_thresholds.py`). The CIS client error rate leaves out the refused staff session checks (the 401s
of `/auth/verify-admin`, counted per 5 minutes in Redis by `api/services/ops_status/session_checks.py`) and shows them
as their own count under the 4xx rate (S290); every other 4xx stays in the rate. Page `frontend/app/admin/overview/page.tsx` + `_status/`, pure helpers and the
URL view state (range, compare, app / severity / status filters, legends, sorts) in `frontend/lib/opsStatus.ts`; smoke
`tests/e2e/smoke/aa1140-ecosystem-status.spec.ts`. The old Overview content moved: the pipeline funnel and content
counts to Data coverage (`data-coverage/ContentPipeline.tsx`), tenant activity and quota outliers to Tenants
(`tenants/TenantActivity.tsx`, ops.read only), both still from `GET /admin/overview`; cost per app and the paid
sources were already on Spend. Their numbers keep a `SourceHint` / KpiTile `info` whose texts live only in
`frontend/app/admin/overview/_components/sources.ts` (guard `tests/unit/test_aa858_overview_sources.py`). The AA-858
business cards (`overview/_components/`) stay in the repo, rendered nowhere; `GET /admin/overview/ops` stays for them.

| Area | admin | reviewer | content | viewer | tenant |
|---|---|---|---|---|---|
| Content work pages: Upload, Rewrite (S1) | ✓ | ✓ | ✓ | — | — |
| Content pages: Review Queue, Master Content, Settings (viewer: read only, write controls hidden by `can()`) | ✓ | ✓ | ✓ | read | — |
| Overview, Photos, Atoms & routes (atom curation), Tenant content QA (`/admin/tenant-qa/*`: Content quality, Content Trace, Review log, Slate), SEO Intelligence, Tenants (`/admin/tenants/*`), Cost Operations, Jobs, Jev Decisions, API Docs | ✓ | — | — | — | — |
| Platform pages `/admin/platform/*` (AA-883 Staff directory; writes need `staff.write`, owners only; AA-913 Alert recipients, changes need `alerts.write` + Cognito) | ✓ | — | — | — | — |
| BFF `/api/admin/openapi.json` and `/api/admin/platform/*` (any method; `lib/rbac.ts` API_READ_RULES, backend re-checks the role) | ✓ | — | — | — | — |
| Backend docs `openapi.json`, `docs`, `redoc` through the staff branch of `/api/tenant/*` (`lib/rbac.ts` isApiDocsPath) | ✓ | — | — | — | — |
| Tenant portal `/portal/*` — **portal app** (AA-Partner `portal/`, `portal.lumiguides.it.com`, AA-885 / AA-979; no staff access) | — | — | — | — | ✓ |
| BFF proxies `/api/admin`, `/api/pipeline`, `/api/tour-full`, `/api/tenant/pipeline/brand-identity`, `/api/tenant/*` (admin app; `/api/tenant/*` is staff-only since AA-885) | ✓ | ✓ | ✓ | GET only | — |
| `/api/tenant/*`, `/api/tenant/me`, `/api/playground/proxy` — **portal app** BFF, tenant JWT only | — | — | — | — | ✓ |

**Read-only `viewer` (AA-878).** The role of every Preview-host Cognito session (AA-911 D-1) and, until AA-816 S2,
of the UI-smoke E2E account (`e2e-claude-code-viewer`, a legacy
password row; password only in Secrets Manager `aa-cis/dev/e2e-test-viewer`; created in S242 with the legacy
row script, retired at AA-816 D1 — a password change for a legacy test row is now a reviewed DB Migrate
`run-script`). Capabilities `content.read` only. It is refused on
**every** non-GET twice: the BFF `requireAdmin()` answers 403 before attaching the secret (`lib/rbac.ts`
`READ_ONLY_ROLES`), and the backend `denial_reason` refuses `READ_ONLY_ROLES` on any method but GET/HEAD/OPTIONS
whatever the route's capability (the notification bell's PUTs need `content.read`, the viewer still cannot clear
it). Admin-only reads stay refused by POLICY; the role-filtered open reads (search, bell, tenant details) treat
it like reviewer/content. No Booking access (Booking reads only `booking-*` groups; the CIS bridge is retired, AA-911). Not
assignable from the Staff page (`staff_directory.CIS_ROLES`, `lib/rbac.ts` `ASSIGNABLE_STAFF_ROLES`) and no
Cognito group (a `cis-viewer` group is deferred to AA-816 E1); the reconcile report lists the row under cutover
readiness (no email / sub), a legacy-only exception. **Session hint (AA-1127 C3):** `GET /admin/session`
(`api/routers/admin_session.py`, `notifications.read`, every role) answers `{role, read_only, reason}`: read-only for
`READ_ONLY_ROLES` (`reason` "role") and for a Dev-only E2E identity (`e2e_read_only`, "e2e"). The admin shell reads it
once (`app/_kit/useSessionAccess.ts`, 5 min) and renders the kit core `ReadOnlyProvider`; pages hide run / save / paid
buttons with `WriteOnly` / `useReadOnly()` (Writing memory, Usage, Spend so far). A UI hint only; the API refuses the
writes anyway. UI smoke runs in **viewer mode** when the smoke session is
the viewer (since the AA-816 S2 path: the Cognito E2E user on the fixed Preview host, capped at viewer by the API;
before it, this legacy account): readable pages get the full page check, admin-only pages must redirect to
`/admin/review`, a BFF write and the staff list must answer 403. A smoke-public test keeps the page list's
`viewerRefused` flags equal to `PAGE_RULES`.

AA-Booking staff roles map onto the same three: `admin` → admin, `sales` → reviewer tier,
`content_creator` → content (the CIS `viewer` maps to nothing: no Booking token). Reviewer and content reach the backend through the same admin secret;
what they may call there is narrowed by the backend capabilities below (AA-882), not by the BFF yet
(`canWriteAdminPath` still allows writes by default until GĐ4b).

**Backend capabilities (AA-882, S241 GĐ1 + GĐ3).** `api/core/staff_authz.py` holds `ROLE_PERMS` (role →
capabilities: `content.read/write` and `review.act` for every staff role; `master.admin`,
`atoms.write`, `photos.write`, `a4.unpublish`, `quality.write`, `content.settings.write`, `tenant.*`,
`ops.*`, `ai.admin`, `spend.*`, `staff.read`, `audit.read`, `alerts.write` (AA-913; the alert-recipient changes
also need a Cognito session, checked in their router) for admin (AA-977: AA-authored content drafts need
`content.write`, its publish / retire / restore `master.admin`); `staff.write` is an owner flag,
not a role permission) and `POLICY` keyed by `(method, route template)`. The router-level dependency
`staff_policy` (secret check, then policy) refuses with 403 a role that lacks the route's capability;
`staff_policy_if_staff` does the same only for a request carrying a staff credential (the valid secret, or since
AA-816 C9 a verified Cognito Bearer or the Preview credential), on the routers
whose own guard also admits a tenant JWT or answers 401 (`admin_pipeline`, `admin_atoms`,
`/v1/pipeline`, `/v1/rules`, `/v1/social`). The role is the `x-admin-role` the BFF forwards
from the verified token; for a Cognito Bearer the `staff_bearer` middleware overwrites it from the
verified principal (header trust remains only for legacy and secret-only requests until the cutover; the Preview
credential is always `viewer`). The main secret **without** a role header is the *machine*
principal (CI deploy preflight/smoke, the ops-probe Lambda), allowed only on `MACHINE_ROUTES`.
Every staff proxy that carries the secret (`/api/admin/*`, `/api/pipeline/*`, `/api/tour-full/*`,
`/api/tenant/pipeline/brand-identity` and the staff branch of `/api/tenant/*`) forwards `x-admin-role`
(a test lists them); a staff proxy without it would act as the machine. Since GĐ3 every staff write
(`/admin/*`, `/v1/pipeline|rules|social/*`) has a POLICY entry, and a GET has one only when its data
is admin-only (tenant accounts and billing, spend incl. the Jev credit ledger and the shadow cost
report, system overview/infra, rerun waves, every tenant's A4 logs / content quality / atoms (`tenant.read`),
the staff directory `GET /admin/platform/staff[/{staff_id}]` = `staff.read`, AA-883 GĐ2); other GETs
stay open to every staff role. Three open reads filter by role inside the handler: `GET
/admin/tenants/{id}/details` is open only for the aa_internal tenant (Master Content) and `tenant.read`
otherwise; `GET /admin/search` returns tenants only with `tenant.read` and jobs only with `ops.read`;
the notification bell (`/admin/notifications*`, one global list with one `is_read`) shows, counts and
clears for a role without `ops.read` only the rows targeted at a content role, never a `platform.*` or
`spend_budget_alert` row; no staff role sees or clears the tenant-only rows (`target_roles` {tenant}: the tenant's
bell, `GET /v1/notifications`, AA-986 L3). The UI mirrors the capabilities in `lib/rbac.ts` `ROLE_PERMS` / `can()` (kept
equal by a test) to hide controls the backend would refuse. `tests/unit/test_aa882_route_policy.py` enforces it (every staff write
has a policy, every staff router runs the policy, `/admin/platform/*` has no open read, snapshot
`tests/unit/snapshots/aa882_staff_route_policy.json`). Behaviour change for reviewer/content: Master
Content lifecycle (activate/deactivate/trash/restore) and Settings brand writes are admin-only (§5.1
of the S241 admin-console design). Staff roles here and in `lib/rbac.ts` are kept equal by a test. Every secret-bearing proxy builds its backend path with
`frontend/lib/bffPath.ts` `proxyPath()`, which refuses a dot, empty or slash-carrying segment (Next.js decodes
`%2F` inside a segment) and percent-encodes the rest, so the path the rbac rules checked is the path the backend routes. Booking staff log in through the CIS
legacy password login (`/auth/admin-login`, kept for Booking until ADR 0006 Phase 2); Booking keeps a thin
profile keyed by `admin_users.id` and verifies a CIS-issued JWT with `aud=booking` (root ADR 0004, O1).

**My work (AA-1156, S286; no migration).** `/admin/my-work` is the landing page of `content`, `content_lead` and
`tenant_qa` (`lib/rbac.ts landingPage`; admin keeps `/admin/overview` and finds My work in the Home nav group, the
viewer keeps the Review Queue). `GET /admin/my-work/summary` (`api/routers/admin_my_work.py`, POLICY
`review.act | content.write | quality.write`) answers one tile per work queue the caller's capabilities allow, never
per person: Review Queue waiting (`review.act`), supply copy drafts to review (entities with an open draft version,
whatever their content status: a redraft of published or stale content keeps that status; list filter `has_draft`)
and hotels / services with no description (`content_status = 'draft'`; stale ones waiting for their automatic redraft
are left out), AA content drafts (`content.write`), place cards to review and compliance holds (`quality.write`). Each
tile has `count`, `oldest_at` and the target page's `filter`; the count runs the page's own list filter (shared WHERE
text next to each list query: `_review_queue_where`, `supply_content.review.LIST_FILTER_SQL`, `aa_content.store
._LIST_FROM`, `acp_shared.place_card_list.list_filter`, `admin_tenant_compliance.FILTER_SQL`), so a tile equals the
total its page lists for that filter (`tests/integration/test_aa1156_my_work_counts.py`). The page adds the filter as
the link's query (`frontend/lib/myWork.ts`); Supply copy reads `?status=` and `?has_draft=`, AA content and Place
cards read `?status=`. Refresh, no
polling; a "Booking: My work" link opens the Booking staff dashboard through `appPageUrl`.

### Booking staff token (AA-680) — retired (AA-911, S271)

CIS no longer mints AA-Booking tokens. `POST /auth/booking-token` and `GET /auth/.well-known/jwks.json` are gone (404)
and `api/core/booking_jwt.py` is deleted: since the staff frontends run on ECS on their own hosts (root ADR 0019 d23),
Booking staff sign in with the Cognito WORKFORCE `booking-staff` client only, and Booking accepts only the WORKFORCE
issuer, its roles (finance included) from the `booking-*` groups. The signing-key secret
`aa-cis/dev/booking-jwt-signing-key` has no reader and is scheduled for deletion (root secrets register).
