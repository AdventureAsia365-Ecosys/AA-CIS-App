# AA-1035 — Tenants Directory hides write controls the role cannot use (S270, lane directory-capability-ui)

The AA-1035 close-out recorded a UI-only follow-up: the Tenants Directory (`frontend/app/admin/tenants/page.tsx`) showed
New tenant, Offboard / Restore and a clickable status pill to every desk role, and fetched the admin-only unmapped
markets banner. The API refuses all of them (403), so this is not a security gap; it is noise and dead buttons. No API
change, no migration.

## Decisions
- **One pure helper, `frontend/lib/tenantDirectory.ts`** (not `rbac.ts`): `tenantDirectoryControls(role)` returns
  `{ create, offboardRestore, statusToggle, unmappedMarkets }` and `canPatchBillingFields(role)` holds the field-level
  rule of `PATCH /admin/tenants/{id}` (`plan_tier`, `is_active` = `BILLING_PATCH_FIELDS` in `api/routers/admin.py`:
  tenant.write or billing.write). It imports `can` from `./rbac.ts` with the extension so `node --test` loads it, like
  `lib/aiGateway.ts`. Kept out of `rbac.ts` because that file is the role/capability matrix itself, mirrored by the
  backend test; page-specific views of it belong next to the page logic.
  - create = offboardRestore = `can(role, 'tenant.write')` (POST tenants, /offboard, /restore are TENANT_WRITE);
  - statusToggle = tenant.write or billing.write (admin, finance); customer_success's tenant.book.write excludes
    `is_active` in `_require_tenant_patch`;
  - unmappedMarkets = `can(role, 'tenant.read')` (GET /admin/unmapped-market-requests is TENANT_READ: admin only).
- **Hidden, not disabled**, for create and offboard / restore: a desk role can never use them, so a disabled button would
  only invite a question. The Detail button stays for every role.
- **Status pill without the toggle = the plain `Badge`** (no `Pressable`, no suspend / resume hint title), the same chip
  and label.
- **The unmapped-markets fetch has its own effect keyed on the flag** (`fetchUnmapped`, separate from `fetchTenants`).
  `useAdminIdentity()` answers the logged-out shape (role `""`) during hydration and the cookie role right after, so a
  single `fetchAll` depending on the flag would load the tenant list twice for an admin; with two effects the tenants
  load once and the banner call goes out only once the role is known to hold tenant.read. A row-action refresh
  (`load(true)`) re-reads the banner only for such a role. The banner render also checks the flag.
- **Tenant 360 Settings plan select**: customer_success reaches the Settings tab and may PATCH the RPM and publish limit
  (tenant.book.write), but a plan change is refused (403 "Your role may not change the plan …"). The fix was small, so it
  is in: `PlanForm` disables the plan `Select` when `!canPatchBillingFields(role)`; the RPM input and Save stay, so a
  customer_success RPM change still works (the body carries `plan_tier` only when the plan is dirty, which it cannot be).

## Changed
- `frontend/lib/tenantDirectory.ts` (new) + `frontend/lib/tenantDirectory.test.mjs` (new, `npm run test:unit`): flags
  for admin, finance, customer_success, support, viewer, unknown / empty roles, every role against the capability map,
  and the plan rule.
- `frontend/app/admin/tenants/page.tsx`: New tenant + CreateModal only with `create`; Offboard / Restore + their modals
  only with `offboardRestore`; `TenantStatusToggle` gets `canToggle`; the unmapped-markets fetch and banner only with
  `unmappedMarkets`.
- `frontend/app/admin/tenants/_components/Tenant360Settings.tsx`: plan select disabled without billing-field rights.
- `tests/e2e/admin-evidence/cs-scope.spec.ts` (agent-only, A10d customer_success): on /admin/tenants, no New tenant,
  no Offboard / Restore, no clickable status pill, Detail visible, no same-origin 4xx response while the page loads;
  then Detail opens the own tenant's Tenant 360.
- `tests/e2e/admin-evidence/admin-pages.spec.ts` (agent-only, A10b admin): new step 7a on /admin/tenants: the admin
  still sees New tenant, an Offboard or Restore button and a clickable status pill (nothing is clicked), one screenshot.
  The spec did not visit the Directory before.
- No new UI copy (no message keys added).

## Tradeoffs
- An unknown role (empty cookie before hydration, or a role the UI does not know yet) gets no controls. Tenant 360's
  tabs take the opposite default (`!known ||` shows the tab) because a hidden read tab costs more than a hidden write
  button; for writes, showing nothing until the role is known is the safe side. For an admin the buttons appear one
  render after hydration.
- The UI reads the display-hint role cookie (`cis_role`); a stale cookie can only hide or show buttons, the backend
  still decides every call.

## Should know
- support still sees the Settings tab of Tenant 360 with editors it cannot save (support holds no tenant write at all;
  config / RPM / plan all 403). Not in this lane's scope (the brief asked only about the plan editor for
  customer_success); a follow-up could hide the Settings editors for a role without tenant.write / tenant.book.write /
  billing.write.
- finance never reaches the Settings tab (Tenant 360 shows it only Billing / Subscription), so its plan editing stays on
  the Subscription tab / plan catalogue; nothing changes there.
- Live check after the Vercel deploy: run `cs-scope.spec.ts` as the A10d identity and `admin-pages.spec.ts` as the A10b
  admin (commands in each file's header).

## Follow-up (S270 live check): the shell's Review badge
The cs-scope evidence spec, run live after #1331, still counted one refused call on the Directory:
`403 /api/admin/review-queue`. It came from the shell, not the page. `useReviewPendingCount` (the Sidebar and BottomNav
badge) polled the Review Queue count for every staff role, and customer_success / support / finance hold no
content.read.
- **Decision:** the hook reads the role (`useAdminIdentity`) and enables its query only when
  `canSeePage(role, "/admin/review")`, the same rule that hides the Review nav item. A role that cannot open the page
  never asks. With no role yet (first paint), the query waits.
- **Should know:** no API change. The badge for content roles is unchanged: same key, same 180 s poll.
