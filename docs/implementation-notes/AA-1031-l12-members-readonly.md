# AA-1031 (S264 lane l12) — Members tab and agency studio box follow the API, not the role cookie

Live check S264 finding F3: Tenant 360 › Members (CIS #1256) and the agency drawer's "Also invite to the Content
studio" box (CIS #1257) showed their write controls to the read-only Dev E2E admin, because they checked only the role
capability `tenant.write` (the role cookie says "admin"). The API then answered 403 `e2e_read_only`.

## Decisions

- Same pattern as the AI providers page (CIS #1248, `GET /admin/ai-gateway/access` + `usePageAccess`): the page asks
  the API which writes THIS session may use. No generic staff-write probe exists, so a tenant-scoped read was added:
  `GET /admin/tenants/{tenant_id}/members/access` (`tenant.read`), answering
  `{tenant_id, read_only, actions: {invite, send_again, change_role, remove}, invite_roles}`.
- Each action is `staff_authz.denial_reason` on that control's own route (`MEMBER_WRITES` in
  `api/routers/admin_tenant360.py`), so it follows the route policy exactly.
- Tenant-scoped on purpose: the two invitation routes are on the E2E admin's write allow-list (root ADR 0006a A10b-1),
  so `denial_reason` lets them through, and the routes then accept that session only on a synthetic tenant
  (`is_canary IS TRUE`) and only as viewer / editor. The access read applies the same rule (one
  `is_synthetic_tenant` read, only for the E2E session), so on a synthetic tenant the E2E admin still sees Invite with
  editor / viewer only, and a send again only on editor / viewer invitations; never change role or remove.
- `invite_roles` drives the invite dialog's role list and the agency drawer box (it invites as owner, so it shows only
  when `owner` is in the list).
- Loading: nothing is offered (and no "read only" note) until the read settles. Failure: the role's `tenant.write`
  decides (lib `memberPermissions`), like `usePageAccess`; the API still refuses what it must.
- 404 for an unknown tenant (same `_require_tenant` as the other member routes).

## Changed

- Backend: `api/routers/admin_tenant360.py` (route + pure `members_access`), `api/core/staff_authz.py` (POLICY row),
  `tests/unit/snapshots/aa882_staff_route_policy.json`, `docs/api/openapi.json` (fastapi 0.161.1 / pydantic 2.14.1),
  new `tests/unit/test_aa1031_members_access.py`.
- Frontend: `lib/tenantMembers.ts` (`membersAccessPath`, `MembersAccess`, `memberPermissions`, `memberRowActions`,
  `mayInviteAs`, `anyMemberWrite`) + tests, new hook `app/admin/_components/useMembersAccess.ts`,
  `Tenant360Members.tsx`, `AgencyDrawer.tsx`, `lib/crmConsole.ts` (`studioInviteOffered` takes "may invite an owner").
- `CONTEXT.md` (Tenant membership directory, admin UI).

## Tradeoffs

- A per-tenant read instead of reusing the AI providers probe: that one is page-specific (`spend.read`, ai-gateway
  routes) and cannot express the tenant-dependent E2E invitation rule.
- One extra small GET per opened tenant (cached 5 minutes in react-query); the E2E branch adds one indexed read.

## Should know

- The rest of the agency drawer (lock, features, recovery…) still follows the CRM write check (`canWrite` from the
  console), not this read: the CIS relay routes refuse the E2E session too. Out of this lane's scope (F3 named only the
  studio box); see follow-ups.
- UI hint only: every write keeps its own checks (BFF `API_WRITE_RULES` "tenants", `staff_policy`, the E2E route
  rules).
