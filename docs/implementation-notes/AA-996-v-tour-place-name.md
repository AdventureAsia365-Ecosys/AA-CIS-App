# AA-996 follow-up (S256): `catalog.v_tour_place_name` (CIS migration 298)

AA-Booking's trip-plans list reads each tour's places for its `destinations` column. It read them from
`catalog.v_tour_place` keyed by `tour_id = ANY($1)`: 380 ms for 25 tours and 994 ms for 100 on Dev (28/10, role
`booking`), against the 1 s timeout of a list extra. Booking #134 bounded the read (chunks of 25, a 2 s page budget, a
5-minute cache). This migration gives Booking a lighter source, so the bounds become a safety net and the read stops
being the slow part of the list.

## Decisions

- **Same rows as `v_tour_place`, not a new meaning.** The view is the 256 `atom` / `spelled` / `grouped` steps of
  `v_tour_place` with only `source_system, source_id, tour_id, day_number, position, place_name`. It keeps the day text
  per tour (the 235 `LATERAL` over `_tour_day_text`), because `place_name` (the spelling the day text uses wins) and
  `position` (where the day text names the place first) both depend on it. Dropping the day text would have been
  cheaper but would change names and order compared with the detail page (`v_tour_day.places`) and TripPlanner.
- **What it drops:** the place card join (`atom_place_card` / `place_card`) and the four lookups per place: the place
  pipeline link (`geo.tour_place` + `geo.name_norm` on both sides), the legacy name + country match and the chosen
  destination in `shared.destinations`, and the current build's stretch in `geo.graph_build` / `geo.tour_segment`.
  PostgreSQL does not remove an unused `LEFT JOIN LATERAL … LIMIT 1`, so a reader of `v_tour_place` paid for them all
  even when it selected only names. `atom_place_card` has `atom_id` as its primary key, so removing the join cannot
  change the row count.
- **The migration proves the equality.** The closing check, as `catalog_owner` (the role the view runs as), compares the
  six columns of both views over every live tour with `EXCEPT ALL` both ways in one statement (one snapshot, so atom
  writes during the apply cannot cause a false difference). Any difference rolls the file back.
- **Grant to `booking` only.** Its one reader. TripPlanner and `aa_app_user` read `v_tour_place` for coordinates and
  keep doing so; the closing check refuses a grant to `tripplanner`, `aa_app_user` or `cis_app` (the
  `v_destination_shape` / `v_component_redirect` least-privilege pattern).
- **Number 298** was reserved in S255 for this view. DB Migrate (`run_migration.py`) keys a run by its version and only
  skips a version already in `shared.schema_versions`; there is no "must be above the head" rule. The role lint applies
  from `role_lint_from` 243 for `cis/`, so it covers 298 (the Infra lint passes on the copy).

## Changed

- `api/migrations/298_catalog_v_tour_place_name.sql` (own commit): the view, its comment, `GRANT SELECT … TO booking`,
  closing checks, registration.
- `tests/integration/conftest.py`: the replay skips 298 (it needs 253 / 256 / 289 and the catalog recipe).
- `tests/integration/test_aa996_migration_298.py`: applies 253 → 289 then 298 the DB Migrate way on the RDS-like state
  and rolls back. Checks: registered and re-runnable, owner and barrier, the six columns with `v_tour_place`'s types,
  row-for-row equality (spelling, order, a day without text, a card and a pipeline link that must not multiply rows, a
  trashed tour), Booking's exact destinations statement on the view, the grant, and the plan of the consumer read
  (`tour_id = ANY`): pushed below the barrier and without any lookup table, while `v_tour_place`'s plan still has them.

## Tradeoffs

- **A second view to keep in step.** A later change to how `v_tour_place` picks a name or orders places must restate
  both. The 298 closing check only guards the moment of apply; a later migration that restates `v_tour_place` should
  run the same `EXCEPT ALL` check (as 298 does).
- **Still parses the day text.** The remaining cost per tour is the itinerary parse plus the atoms; the measured win on
  Dev comes after apply (script below). If it is still too slow, the next step is a per-tour aggregate, not a change
  of meaning.

## Should know

- **Ship order (D8):** this migration-only PR merges first, then the byte-identical Infra copy
  (`db/migrations/cis/298_catalog_v_tour_place_name.sql`), then DB Migrate `check` → `apply`, then the Booking PR that
  reads the view. Until the Booking PR deploys nothing reads the view.
- Between the CIS merge and the apply, the nightly DB Schema Dump replay sees an unregistered file below the CIS head
  (299) and replays it as a legacy file; it is gone from the report once 298 is registered. Apply soon after merge.
- Live measurement after apply: `AA-Ecosys/.tmp-session/S256/aa996/live_place_name_budget.py` (read only, run in the
  Booking container) EXPLAIN ANALYZEs the destinations read on both views for 25 and 100 tours.
- Root docs to follow (not in this repo): `docs/contracts/README.md` (catalog.tour: the new projection view) and
  `docs/database/schema-ownership.md` (`catalog` row: `v_tour_place_name` to `booking` only).
