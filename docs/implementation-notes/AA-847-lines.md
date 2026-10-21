# AA-847 — Problem 1: linear places drawn as lines (CIS side)

Owner decisions S246 (Nghiệp): **D1** rivers, roads and trekking stretches are DRAWN correctly on the TripPlanner map —
never a pin, never a circle, never hidden (also before the flip and while a shape is missing); **D2** duplicates are
merged automatically, an admin is not the normal path. Plan: root `docs/audits/2026-10-21-S246-aa847/
design-linear-places.md` + the revised plan (skeptic pass S246, §0–§10). Kept in its own notes file so the parallel
AA-847 lanes do not edit the same file (lesson S242); `AA-847.md` has a one-line pointer.

Branches (stacked; the orchestrator merges in this order):
1. `feat/aa-847-lines` (on `feat/aa-847-places-validity`): migration 255 (storage) + §1 (no hiding, `line`, graph
   skip, labelled phrase set, gate). Deploy unit = the validity branch: it must not deploy without this (D1).
2. `feat/aa-847-place-shape` (on 1): `shape.py`, sources, job `place_shape`, `tour_segment` in `swap_graph`, line
   duplicate rule, `/admin/places` Lines tab, backfill dry run.
3. `feat/aa-847-lines-views` (on 2 + `feat/aa-847-places-l4` merged in): migration 256 (catalog views), contracts v1.2.

## Decisions
- **255 = storage only, before the validity deploy** (critique "ORDER/CHECK CLASH"): the CHECK widening, the two
  tables, the geocode_call columns and the budget row. Views are a separate migration after 253 (256 here).
- **`geo.geocode_call.credits` is nullable, not `NOT NULL DEFAULT (= requests)`** (a default cannot read another
  column): the code running during the deploy window inserts rows without it, so the guard sums
  `COALESCE(credits, requests)`; existing rows are backfilled `credits = requests`. `endpoint` is `NOT NULL DEFAULT
  'search'` with a CHECK (search / place_details / overpass).
- **`geo.place_shape` bbox is NOT NULL from the start**: a pending row carries its point as the bbox, so the view of
  Migration B needs no COALESCE on the shape side and the tile filter stays a plain float comparison.
- **CHECKs carry the size caps** (`geo.line_vertex_count(jsonb)`, IMMUTABLE SQL): geom ≤ 2,000 vertices, geom_mid ≤
  300 and ≤ 12 KB, geom_lo ≤ 60, segment ≤ 300; a drawn status (ok / approximate / manual) ⇔ the three levels +
  source + length are set; approximate ⇔ source `anchors`, manual ⇔ source `manual`.
- **`geo.tour_segment` is build-versioned** (FK `geo.graph_build` ON DELETE CASCADE), not columns on `geo.tour_place`
  (critique "WRONG TABLE FOR STRETCHES").
- **`linear_feature` stays a gate reason with a new meaning**: a linear name NOT stored as a line (precision point /
  area — the pin or circle D1 forbids, e.g. a Dev row geocoded before this deploy). The plan said "remove it from
  GATE_REASONS"; removing it would let a trail pin through the gate. The new `linear_without_shape` (a line whose
  shape is not ok / manual / approximate) is added. Both clear without an admin: the `linear_feature` fix stores the
  row `line` and queues its shape; the `place_shape` job draws it.
- **Word list** (§1): + stream, creek, waterway, expressway, path, circuit; `range` → area; + `rapids` as a point on the
  feature (a rapid is a spot). Not `kora`, not `cruise`.
- **`linked_feature`** (descriptive phrase → the feature it names, deterministic): only Capitalised words make the
  feature's name; leading activity / verb / adjective words and numbers are cut ("Hike the 10km Pingxi Historic
  Trail" → "Pingxi Historic Trail", "Kilimanjaro Trekking Via Machame Route" → "Machame Route"); "River X" form
  ("Kanchanaburi River Kwai" → "River Kwai"); a point word anywhere, an area word after the feature, or a proper
  place before "on / along / by / near the X River" ("Luang Prabang on the Mekong River") keeps the phrase as it is.
  The component id keeps the itinerary spelling (no id-map impact); only the link key changes.
- **No `RULE_VERSION` bump**: bumping it would re-extract every built tour's days (paid model calls). The new link
  rule applies to tours extracted after the deploy (Dev has ~27 built; the paid run AA-905 has not run yet).
- **Graph:** a line destination is skipped by `main_component_destinations`, an overnight on a line is treated as no
  overnight (the day's main point place becomes the stop), and `build_graph` drops any line stop a caller passes
  (defence in depth). `store.graph_inputs` loads `geo_precision` into `dests`.
- **Outlier rule:** `store.night_stops` skips line destinations (simplest correct choice; distance to the line can
  come later, the shape is not needed for the rule's purpose).
- **Point-distance same-place rule never applies to a line** (`validity.is_same_place` returns False when either is a
  line): line duplicates go through the line rule (branch 2).
- **Validity fix `linear_feature`**: stores `line`, un-hides a row the first validity branch hid (suspect +
  `review_reason='linear_feature'`), inserts the pending shape row; undo restores the destination and deletes the shape
  row it created while that row is still pending. `LINEAR_FEATURES_HIDDEN` and `Located.hidden` are gone.
- **Duplicate merge and shapes** (`validity_fix.shape_merge`): the keeper keeps its shape when it is drawable (or
  neither is); otherwise the duplicate's row moves to the keeper; a deleted row is saved whole (`to_jsonb`) in the
  audit details and undo re-inserts it (`jsonb_populate_record`). Undo walks the changes backwards.
- **Branch 1 enqueues no `place_shape` job**: that kind is registered on branch 2; a job of an unregistered kind would
  fail. Pending rows wait for branch 2 (its job and backfill pick them up).

## Changed
- `api/migrations/255_geo_place_shape.sql` (new) + `tests/integration/test_aa847_migration_255.py` (new).
- `services/places/geomatch.py`: word lists, `LINE_CLASS_WORDS` / `line_class`, `linked_feature`.
- `services/places/locate.py`: `Located.precision` 'line' for a linear kind, no `hidden`, no review reason for a line.
- `services/places/store.py`: insert / write SQL never write `suspect` (un-hide of an old hidden row),
  `ensure_pending_shape`, `night_stops` skips lines, `geocoder_requests_today` sums credits, `log_geocode_calls`
  takes `credits` / `endpoint`, `graph_inputs` loads precision.
- `services/places/pipeline.py`: `geocode_pending` creates the pending shape row (`shapes_pending` count);
  `linked_to_feature` count from `components.build_components`.
- `services/places/graph.py`: lines never stops / nodes.
- `services/places/validity.py`: V3 reasons, `v3_reason`, `DRAWN_SHAPE_STATUSES`, `Place.shape_status`, gate,
  `is_same_place` line guard, COMPONENTS_SQL reads `geo.place_shape.status`.
- `services/places/validity_fix.py`: `linear_feature` fix → line, `shape_merge` / `_apply_shape_merge`, undo order.
- Tests: `test_aa847_lines.py`, `test_aa847_linked_feature.py` (new); `test_aa847_geomatch_kind.py`,
  `test_aa847_validity.py` (docstring); integration `test_aa847_admin_places_validity.py` (line fix + shape, applies
  255 itself), `test_aa712_migration_226.py` (credit guard counts credits; applies 255).
- `CONTEXT.md`: place kinds (lines drawn), line geometry, gate reasons.

## Tradeoffs
- `linked_feature` labelled set: 52 link + 31 keep phrases (83). Measured: **0 false links, 0 wrong features, 0 missed
  links** on the set — the set was written alongside the rule (author-labelled, real atom places of the catalog
  calibration data + the design's examples + adversarial "Town on the X River" cases added after the first pass), so
  0/0/0 is a regression floor, not a field accuracy. The rule is conservative by design: "Huay Xai to Pak Beng on the
  Mekong River" stays as it is (a missed link costs a second destination for that phrase, a false link would draw a
  river for a town). Before the paid run, extend the set with the Dev atom places of the §9 probe.
- Skipping lines in the outlier rule instead of measuring distance to the line: no shape dependency in
  `place_geocode`; a wrong line point is caught by the shape acceptance rule (distance to the anchors) instead.

## Should know
- **Hard order:** apply 255 (Infra copy `feat/aa-847-cis-255-copy`) BEFORE deploying this branch: `place_geocode`
  writes `line` (the old CHECK raises), `store.geocoder_requests_today` / `log_geocode_calls` read and write
  `credits` / `endpoint`, the validity report reads `geo.place_shape`.
- Do NOT apply the old linear-hide fix on Dev. If it was applied, undo it by audit id first (or the `linear_feature`
  fix un-hides those rows — they are not in the report while suspect, so the undo is the path).
- `test_aa712_migration_226.py` drops schema geo and re-applies 226 alone; the modules that need 255 after it
  (`test_aa847_admin_places_validity.py`, `test_aa847_migration_255.py`, the 226 credit test) re-apply 255 (idempotent).
- Local evidence (throwaway PostgreSQL 16, lesson S246): integration 226 + 228 + 255 + admin_places_validity 43
  passed; unit places tests 745 passed + the new files.
