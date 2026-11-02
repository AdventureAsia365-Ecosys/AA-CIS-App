# AA-1025 (C1 follow-up, S264 lane l3-ui-fixes): AI providers page, write controls for read-only sessions + 1440 width

## Decisions

- **Who is "read-only" here.** `/admin/ai-gateway` is admin-only (`lib/rbac.ts` PAGE_RULES), so the viewer role never
  reaches it. The session that saw Switch / Re-queue was the Dev E2E CIS admin: its role is `admin` (cookie and
  `/auth/verify-admin`), but its verified identity carries `e2e_read_only`, and the bearer middleware refuses every
  write with 403 `e2e_read_only`. A role check in the browser (`can(role, ...)`) cannot see that flag.
- **The API says what the page may show.** New `GET /admin/ai-gateway/access` (spend.read, like every read of the
  page) answers `{read_only, actions: {provider_switch, connections, connection_test, requeue, routing, catalog,
  budgets, jev_credit}}`. Each action is `staff_authz.denial_reason(principal, method, route)` on the route that
  control calls (`PAGE_WRITES` in `api/routers/admin_ai_gateway.py`), so the page hides exactly what the API would
  refuse: capability, the read-only role and the E2E flag, one source. The writes keep their own checks (403 stays).
- **Hide, with one line saying why.** Overview: Switch buttons are not rendered, Re-queue has no button and shows the
  existing read-only sentence, a status line at the top says the controls are hidden. Connections: on / off shown as
  a badge, Save replaced by a sentence, form fields disabled, Run test and Check now hidden. Jev credit: alert check,
  add form and delete column hidden. Budgets: Add and Edit hidden (row click does nothing). Routing: selects locked,
  Save hidden, the route editor is a disabled `fieldset` (Save / Cancel hidden). Models: the catalog switch disabled.
- **Loading and failure.** While `/access` loads, write controls stay hidden (no flash of a button a read-only
  session cannot use). If the read fails, the role cookie decides (`lib/rbac` `can()` with the capability of each
  action, `ACTION_CAPABILITY`); the API still refuses what it must.
- **1440 width.** The provider tables had `tableMinWidth` 1160. At 1440 px the column is 1440 - 236 (sidebar) - 72
  (page padding) - 6 (scrollbar) - 2 (card border) = 1124 px, so Balance was cut by about 32-36 px. Now 1080
  (`PROVIDER_TABLE_MIN_WIDTH`), 44 px spare; narrower screens still scroll inside the card.

## Changed

- Backend: `api/routers/admin_ai_gateway.py` (`PAGE_WRITES`, `page_access`, `GET /access`), POLICY entry in
  `api/core/staff_authz.py`, route snapshot `tests/unit/snapshots/aa882_staff_route_policy.json`,
  `docs/api/openapi.json` (regenerated with the pinned fastapi 0.161.1 / pydantic 2.14.1).
- Frontend: `lib/aiGateway.ts` (`ACCESS_API`, `PAGE_ACTIONS`, `ACTION_CAPABILITY`, `mayUse`),
  `app/admin/ai-gateway/usePageAccess.ts`, and the six components above; messages `readOnlyNotice` (M) and
  `readOnly` (CONNECTIONS), EN + VI.
- Tests: `tests/unit/test_aa1025_providers_access.py`, `frontend/lib/aiGatewayAccess.test.mjs`, route lists in
  `test_aa804_ai_gateway_api.py` and `test_aa882_gd3_router_samples.py`.

## Tradeoffs

- A new endpoint (one more route in the policy snapshot and the OpenAPI file) instead of a field on
  `/providers`: the Budgets, Routing and Models tabs need the answer too, and `/providers` is a heavy read (CRM call).
- Not a cookie set at sign-in: that would touch the kit `useAdminIdentity` (vendored to other apps) and the
  `/auth/verify-admin` contract for one page.
- 1080 px least width was computed, not measured locally (no signed-in admin locally). The live step measures the
  Balance column's right edge at 1440.

## Should know

- Adding a write control to this page: add its route to `PAGE_WRITES`, the key to `PAGE_ACTIONS` and
  `ACTION_CAPABILITY` (the frontend test compares the two lists), and read `usePageAccess().can(...)`.
- The E2E admin evidence run (`admin-evidence`, local only) now shows the page without write buttons; the status line
  `ai-gateway-read-only` is the evidence.
