# AA-1087 — migration 401 (catalog.v_tenant_account_v1 appends is_synthetic)

Part of AA-1087 (Dev-only write-capable E2E staff identities, root ADR 0006a A10b-3 / A10e). The CIS code half
(A10b-3, the Compliance release) is `docs/implementation-notes/AA-1087.md` in the code PR.

## Decisions
- Booking needs CIS's synthetic rule (`is_canary IS TRUE AND NOT is_pilot`, `api/core/e2e_staff.SYNTHETIC_TENANT_SQL`)
  to hold its E2E write identities to synthetic agencies. Booking reads the tenant account only through
  `catalog.v_tenant_account_v1` (357), so the rule becomes an appended view column `is_synthetic`, computed in CIS, not
  re-implemented in Booking. No new view: contract v1 allows appended columns (tenant.entitlements.v1 1.6).
- Only `is_canary` is newly readable by `catalog_owner` (column grant); booking still has no privilege on
  `shared.tenants`.

## Changed
- `api/migrations/401_tenant_account_is_synthetic.sql` (CONTRACT: column GRANT + replace of a catalog view).
- `tests/integration/test_aa1087_migration_401.py`; conftest skips 401 in the replay (catalog recipe, like 357).
- `docs/contracts/tenant.entitlements.v1.openapi.yaml` 1.6 (byte copy of the root file, same root PR).

## Tradeoffs
- A Booking-owned `is_synthetic` mark on its own rows was weighed and dropped: two marks for one fact, and a staff
  member could set it on a real agency's case from the Booking admin.

## Should know
- Number: 400 is the highest on main (12/11); renumber to owner main max + 1 at merge if needed (`git grep '401_'`).
- Rollback needs a new forward file (a view cannot drop a column in place); see the file header.
