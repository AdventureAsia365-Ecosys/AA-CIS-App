# AA-1036 R3 — tenant.addons.v1: the CRM relays its owner's add-on request (lane crm-addon)

Found live on Dev (S269): an agency owner with the CRM but no Content studio membership clicked the CRM sidebar entry
"Trip planner: request add-on" and landed on the studio's `/portal-access/none` ("No Content studio access"). A CRM-only
agency had no way to ask for an add-on, although root ADR 0019 d7 says add-ons switch on by entitlement or request.

## Decisions

- A new versioned contract, `tenant.addons.v1` (root `docs/contracts/`, byte copy here pinned by sha256), not a new
  `/v1/account/*` path: the CRM has no CIS tenant token (CRM sessions are partner-pool tokens of the AA-Partner web
  client, `/v1` accepts the studio client only). Service to service with the M2M pool, like `tenant.membership.v1` and
  `tenant.entitlements.v1`.
- Its own scope `cis.tenant/addons` (AA-CIS-Infra `identity_m2m.tf`): `cis.tenant/read` is read-only, and CIS pins one
  scope per route class.
- One store and one decision path: the routes call the unchanged studio service
  (`services/tenant_account/addon_requests.tenant_view` / `create_request`). The list body is byte-for-byte the studio's
  `/v1/account/add-ons`; a request answers `{created, request}` like the studio's.
- The CRM decides who may ask (its owner; CRM roles are the CRM's, root ADR 0010 decision 5) and sends the CRM member
  id. CIS records `requested_by` and the audit actor as `crm-member:<uuid>` (47 characters, within the 64 CHECK of CIS
  374), never a name or an e-mail.
- `/tenant/v1` is neither a staff path nor `/v1`: no tenant role middleware, no tenant rate limiter. The studio's
  10-a-day cap per tenant (in the service) is the limit.

## Changed

- `api/routers/tenant_addons.py` (new), `api/main.py` (router), `api/core/m2m_auth.py` (`SCOPE_TENANT_ADDONS`).
- `docs/contracts/tenant.addons.v1.openapi.yaml` (byte copy), `docs/api/openapi.json` (regenerated with FastAPI 0.161.1 /
  Pydantic 2.14.1), `CONTEXT.md` (the integration row).
- Tests: `tests/unit/test_aa1036_tenant_addons_contract.py` (contract, auth, refusals, bodies validated against the
  contract schemas), two integration tests in `tests/integration/test_aa1036_addon_requests_pg.py` (one row, the audit
  and staff bell rows, the studio's list sees the same request; the studio's rules hold), the M2M route inventories of
  `test_s241_admin_routes_require_auth.py` and `test_aa1035_tenant_scope_routes.py`.

## Tradeoffs

- No migration: CIS 374 already holds `requested_by` as free text (1 to 64 characters).
- The staff bell names the requester as a tenant actor (`actor_type = 'tenant'`), as for the studio: staff see the
  agency, not which app it asked from. The actor string tells them on the request's audit row.

## Should know

- Ship order: root contract PR, AA-CIS-Infra scope (two applies: the resource server, then the client), this CIS PR,
  then the AA-Partner PR. Until CIS serves the routes the CRM page says add-ons cannot be read (404 from CIS = 503 in the
  CRM); until the client holds the scope the token endpoint answers `invalid_scope` (same message).
- The API Gateway route `/tenant/v1/{tenant_v1_proxy+}` is ANY already (AA-1028); nothing to add there.
