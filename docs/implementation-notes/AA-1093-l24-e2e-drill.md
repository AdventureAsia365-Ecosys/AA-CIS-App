# AA-1093 lane l24 (S279) — the E2E admin may run the erasure saga DRY RUN drill (root ADR 0006a A10b-5)

Problem: the live DRY RUN drill of the tenant offboarding erasure saga could not run as the Dev-only E2E CIS admin
(A10b): offboard, restore and `/admin/job-runner/erasure/requests/{drill,advance,stop}` answered 403 `e2e_read_only`
because they were not on the A10b write allow-list. Root ADR: `docs/adr/0006a-identity-phase1-amendments.md` A10b-5
(root PR of this lane).

## Decisions

- **Five writes join the allow-list** (`api/core/e2e_staff.E2E_ERASURE_DRILL_WRITES`, route templates and concrete
  path regexes): POST `/admin/tenants/{tenant_id}/offboard`, `/admin/tenants/{tenant_id}/restore`,
  `/admin/job-runner/erasure/requests/drill`, `…/{request_id}/advance`, `…/{request_id}/stop`. Approve, new-round and
  the subject erasure `/admin/job-runner/erasure/run` stay off: only a person approves an erasure, a new round is the
  tech lead's decision.
- **Each route checks the tenant itself** (the A10b-1 / -3 / -4 pattern), 403 `e2e_read_only` before any write, job or
  audit row unless the tenant is synthetic (`is_canary IS TRUE` and not the pilot, `e2e_staff.is_synthetic_tenant`):
  - offboard / restore: the `{tenant_id}` path parameter, through a route dependency
    (`api/core/e2e_tenant_writes.SYNTHETIC_PATH_TENANT`, appended to `TENANT_SCOPED`). It reads
    `request.path_params` rather than declaring the parameter, so the OpenAPI document does not change, and it keeps
    `api/routers/admin.py` under its AA-1097 file-size baseline (no new lines there);
  - drill: the body's tenant, first thing inside the transaction (the existing 409 "only a synthetic tenant may be
    drilled" stays for everyone);
  - advance / stop: the request's tenant, after the 404 (so the spec's probe on a random id still reads 404).
- **Approve and new-round refuse every Dev-only E2E identity inside the route too** (`refuse_any_e2e`), behind the
  middleware and the route policy: defence in depth for the one irreversible step.
- **One pure helper** `e2e_staff.synthetic_write_refusal(identity, conn, tenant_id)` returns the reason
  (`not_allowlisted`, `tenant_not_synthetic`) or None; the FastAPI side (403, log line) lives in the new
  `api/core/e2e_tenant_writes.py` because `e2e_staff` is imported by `staff_authz` (no circular import) and stays
  FastAPI-free. No DB read for a caller that is not a Dev-only E2E session.
- Log events: `tenant_offboarding_e2e_refused` (offboard / restore), `tenant_erasure_e2e_refused` (saga routes), each
  with `reason`.

## Changed

- `tests/e2e/admin-evidence/erasure-drill.spec.ts` (cherry-picked from the local drill-spec branch): header updated for
  A10b-5; when the probes pass it now also expects advance / stop 404 and restore 409; the drill tenant select is
  picked by its test id (`tenant-erasure-drill-tenant`). Paths checked against the routes: BFF
  `/api/admin/job-runner/erasure/requests*`, `/api/admin/tenants/{id}/offboard|restore|members/access`, the page
  `/admin/tenants/data-erasure?tab=offboarding` and its test ids.
- `tests/unit/test_aa979_a10b_studio_invites.py`: its "other tenant writes stay refused" lists named offboard; offboard
  is now allow-listed, so they name the add-on request approve instead.
- `tests/unit/test_aa816_e2e_cis_admin.py`: the reviewed allow-list grows by the five routes.

## Tradeoffs

- A dependency for offboard / restore instead of an in-function call: the check runs before the route body (before
  `verify_admin_secret`), which for the E2E session is already verified by the middleware; a missing tenant answers
  403 to the E2E session instead of 404 (a missing row reads as not synthetic, the A10b-1 rule).
- No new threshold, limit or setting is added.

## Should know

- Live drill (after merge + deploy): `E2EADMIN=1 E2E_DRILL_FULL=1 DRILL_SLUGS=audit-wildkind-travel
  EVIDENCE_DIR=<root>/docs/audits/2026-11-13-S279-session/verify/aa1093-saga-drill npx playwright test --project
  admin-evidence erasure-drill` with `AWS_PROFILE=aa365-admin`. It offboards, drills, runs, stops and ALWAYS restores
  the synthetic tenant; it never approves.
- Removal: with the rest of A10b at the end of project code (drop `E2E_ERASURE_DRILL_WRITES`,
  `api/core/e2e_tenant_writes.py` and the calls in the two routers).
- Tests: `tests/unit/test_aa1093_a10b5_erasure_drill.py` (real app through the middleware and the policy),
  `tests/integration/test_aa1093_a10b5_erasure_drill_db.py` (Postgres as `cis_app`: full drill on a synthetic tenant;
  every write refused on a real tenant and on the pilot, nothing written).
