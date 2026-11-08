# AA-1046 follow-up: RLS catalogue allowlist loses the booking inquiry grants (S273 lane l8)

## Decisions

- Remove the two rule-g entries `tripplanner:booking` and `tripplanner.v_inquiry_outbox:booking` from the shared
  allowlist of `tests/integration/rls_catalogue_rules.py`. They covered TripPlanner tp-009's grants to role `booking`
  for the retired inquiry.submitted contract; TripPlanner tp-027 revokes both. A grant back is a violation again.
- Removed, not kept with a "revoked, goes stale" reason (the CIS 254 precedent): the entries name a cross-app read of
  every tenant's traveller data, so the guard should flag it the moment it reappears.

## Changed

- `tests/integration/rls_catalogue_rules.py` (canonical; AA-CIS-Infra copies the bytes to `db/rls_catalogue_rules.py`
  in its registry PR of the same wave, Copy Guard compares with this repo's `main`).
- `tests/unit/test_rls_catalogue_rules.py`: a booking grant on the view and the schema is a violation; the CIS ops
  view reads of tripplanner stay allowlisted.

## Tradeoffs

- Between this merge and the Dev apply of tp-027 the nightly dump reports the two live grants as unallowlisted
  violations (warnings on Dev, never fatal). The wave applies tp-027 right after, so the window is short; after the
  apply nothing matches.

## Should know

- The CIS CI replay never creates the tripplanner grants (it replays CIS migrations only), so CIS CI is unaffected.
- Merge order: this PR, then the AA-CIS-Infra PR (byte copy + owners.yaml + isolation probe), then the Migration
  Apply of TripPlanner tp-027.
