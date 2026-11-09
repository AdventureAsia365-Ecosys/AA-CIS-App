# AA-911 — retire the CIS -> Booking sign-in bridge, CIS side (lane l7-aa911-staff-ecs, S271)

Merge only AFTER the staff web cut-over (root runbook `docs/ops/runbooks/staff-web-ecs-cutover.md`) and AFTER the
AA-Booking PR that turns the bridge issuer off (otherwise Booking's legacy password sessions see 404 from CIS while
Booking still offers the form). The parked branch `refactor/aa-911-retire-booking-bridge` (S248) was 420+ commits
behind and mixed with the retired Preview sign-in design; this is a fresh slice on main.

## Decisions

- Remove `POST /auth/booking-token`, `GET /auth/.well-known/jwks.json`, `api/core/booking_jwt.py`, the response
  models (`BookingTokenResponse`, `Jwk`, `Jwks`) and `staff_authz.NO_BOOKING_ROLES` (its only reader was the mint).
- `/auth/admin-login` stays (legacy sign-in behind `LEGACY_ADMIN_LOGIN_ENABLED`, AA-816); `_load_active_staff` stays
  (the legacy `/auth/verify-admin` path reads it).
- OpenAPI regenerated with the pinned fastapi 0.161.1 / pydantic 2.14.1 (a scratch venv from requirements.txt).

## Changed

- Tests that asserted the bridge (S2 legacy-off, viewer role, preview cap, route-auth inventory) now assert 404;
  the bridge tests (`test_aa680_booking_jwt.py`, `test_aa680_booking_staff_lookup.py`) are deleted; new guard
  `tests/unit/test_aa911_bridge_gone.py`.

## Should know

- Removing public `/auth/*` paths is a breaking change for the API contract check: the PR title carries `!`
  (`refactor!:`) or the label `api-breaking`.
- After deploy: `curl -s -o /dev/null -w '%{http_code}' -X POST https://api-cis.lumiguides.it.com/auth/booking-token`
  = 404; the signing-key secret `aa-cis/dev/booking-jwt-signing-key` is then scheduled for deletion (7-day recovery,
  orchestrator, root secrets register).
