# AA-816 A10b — Dev-only E2E CIS admin (S252)

Root ADR 0006a A10b. The orchestrating agent needs its own CIS **admin** sign-in to screenshot the admin-only pages
(`/admin/content-quality`, `/admin/writing-memory`) and to produce the AA-916 WM-07 sample (admin sentence actions
through the UI, on synthetic Dev tenants). The A10 account `e2e@` is a reviewer only. A third WORKFORCE E2E address,
`e2e-cis-admin@e2e-mail.lumiguides.it.com`, exists on Dev only. Two PRs: PR-1 is migration 283 alone (CHECK); this
note covers PR-2 (code), which merges after 283 is applied.

## Decisions

- **One shared module, `api/core/e2e_staff.py`.** The address, the exact-`dev` environment test, the 403 details
  (`e2e_read_only`, `e2e_not_allowed_here`) and the write allow-list live there, so `staff_identity` can read them
  without importing `staff_directory` (which imports `staff_identity`).
- **Directory.** `staff_directory` keeps `E2E_TEST_STAFF_EMAIL` and adds `E2E_CIS_ADMIN_TEST_STAFF_EMAIL` (role
  `admin`); a read-only `{address: role}` map drives `is_e2e_test_email`, `require_staff_email` and
  `require_e2e_test_scope` (the role must be the address's one role, no Booking grant; one message per address).
  `require_dev_only_e2e_environment` (403 `e2e_not_allowed_here` unless `os.environ.get("ENVIRONMENT") == "dev"`,
  no default) runs in `create_staff`, `change_roles`, `enable_staff`, `sign_out_staff` (it re-enables) and the
  active-row branch of `sync_staff`. `disable_staff` and the inactive-row sync (which disables) stay allowed
  everywhere, so the account can always be switched off. `staff_invite.is_staff_address` is unchanged: create with
  `send_invite=False`.
- **Identity.** `StaffIdentity.e2e_read_only`. `resolve_staff` sets it when the bound row's email OR the token's
  `email` is the A10b address (the token check is belt and braces; the row is what binds). Outside an exact `dev`
  environment it refuses instead (403 `e2e_not_allowed_here`, log `staff_identity_refused`). The flag also forces
  `is_owner` false (the CHECK already forbids an owner row; a hand-edited row still cannot write the directory).
  `staff_identity_e2e_read_only` is logged once per cache load.
- **Read-only cap in the bearer middleware**, before routing: any method outside GET / HEAD / OPTIONS on a staff path
  is 403 `{"detail": "e2e_read_only"}` (log `staff_bearer_refused reason=e2e_read_only`) unless the concrete path
  fully matches one of `E2E_ALLOWED_WRITE_PATHS`.
- **The allow-list (S252 orchestrator decision, D1 amended):** exactly POST
  `/admin/content-quality/pieces/{piece_id}/sentences/apply` and POST
  `/admin/content-quality/tour-versions/{version_id}/sentences/apply` — the routes behind the Content quality
  drawer's Cut and Edit buttons with their reason chip (`frontend/app/admin/content-quality/SentenceActions.tsx`,
  `TourVersionDrawer.tsx`). Path ids must be UUIDs, full match (no trailing slash, no extra segment). Not allowed:
  `…/sentences/suggest` (the paid model call behind Rewrite), the exemplar PATCH on `…/signals/{id}`, labels,
  place-card writes, everything else.
- **Mode check in the two handlers** (`_refuse_e2e_mode`): an A10b session may send `mode` `cut` or `manual` only.
  `rewrite` records a model suggestion it could not have asked for (suggest is refused), which would put a fake
  "model rewrite" signal into writing memory.
- **Defence in depth in `staff_authz`.** `StaffPrincipal.e2e_read_only` comes from the verified identity;
  `denial_reason` refuses every non-read method unless `(method, route template)` is in `E2E_ALLOWED_WRITES`, so a
  route reached by any future non-middleware path is covered too.
- **Agent-only Playwright project `admin-evidence`** (`tests/e2e/admin-evidence/admin-pages.spec.ts`): skipped
  unless `E2EADMIN=1`; baseURL the production CIS host; retries 0; screenshot / trace / video off; ignored by the
  `chromium` project. The mailbox helpers take a prefix (`listMailObjects`, `mailboxBaseline`, `waitForSignInCode`;
  defaults keep `inbound/`) and `signInByEmailCode` takes `email` / `prefix` (defaults: `e2e@`, `inbound/`). A unit
  guard checks that no workflow sets `E2EADMIN`.
- **Creation** is an S3-mediated ECS exec script calling `staff_directory.create_staff(..., role="admin",
  booking_role=None, send_invite=False)` with the active owner row (Nghiệp) as the audited actor, under his S252
  standing approval (design §5; the orchestrator's runbook holds the script). Not Terraform, not the bootstrap tool.

## Changed (vs. the design)

- The allow-list was added (orchestrator decision S252); the design's D1 was "read-only, no exception".
- The mode restriction (`cut` / `manual`) is an addition the design did not spell out; it follows "Cut / manual edit".
- The read-only flag also follows the token's `email` claim, not only the row's.
- `reason_code` is not required for the A10b session: the UI preselects a chip, and refusing a null code would add
  a rule the routes do not have for other admins.

## Tradeoffs

- A path regex in the middleware AND a template set in `staff_authz`: two lists to keep in step (a guard test checks
  both name real POLICY routes and have the same length), in exchange for refusing before any route code runs.
- The cap keys on the address, not on a new role: no enum change, no frontend change (`rbac.ts` already maps
  `admin`), and the removal is a deletion.

## Should know

- **Order:** PR-1 (migration 283) → Infra copy + DB Migrate apply → this PR → deploy (the cap is live before the user
  exists) → create the user. The guard test `test_e2e_helper_and_migration_name_the_same_address` reads migration
  283, so this PR's CI is green only once PR-1 is on `main` (update the branch first).
- The API task's `ENVIRONMENT` must be exactly `dev` (Terraform `modules/ecs`: `var.environment`), or the account
  can neither be created nor sign in. Unit CI runs with `ENVIRONMENT=test`, so the tests set it themselves.
- Cross-app: no `booking-*` group, so Booking answers `no_booking_role`; the CIS → Booking bridge mints only from
  password sessions; AA-Partner `/crm/ops/v1` gives `cis-admin` reads only (writes need `crm-platform-admin`, which the
  trigger never allows on this address).
- **Removal (end of project code, with A10 / A10a):** drop `api/core/e2e_staff.py` and every use (`staff_directory`
  map entry + env guard, `resolve_staff` flag, middleware cap, `staff_authz` field + check, `_refuse_e2e_mode`), the
  `admin-evidence` project, spec and helper parameters' admin constants, and these tests; restore-247 migration after
  deleting the row.
