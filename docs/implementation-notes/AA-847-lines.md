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
- **Validity fix `linear_feature`**: stores `line`, inserts the pending shape row; undo restores the destination and
  deletes the shape row it created while that row is still pending. `LINEAR_FEATURES_HIDDEN` and `Located.hidden` are
  gone. Its "un-hide a row the first validity branch hid" branch (suspect + `review_reason='linear_feature'`) is
  unreachable (review S246): `COMPONENTS_SQL` joins `d.geo_status <> 'suspect'`, so a hidden row never becomes a
  finding and `plan()` never sees it. Recovery from the old hide fix is its undo by audit id (see Should know).
- **Duplicate merge and shapes** (`validity_fix.shape_merge`): the keeper keeps its shape when it is drawable (or
  neither is); otherwise the duplicate's row moves to the keeper; a deleted row is saved whole (`to_jsonb`) in the
  audit details and undo re-inserts it (`jsonb_populate_record`). Undo walks the changes backwards.
- **Branch 1 enqueues no `place_shape` job**: that kind is registered on branch 2; a job of an unregistered kind would
  fail. Pending rows wait for branch 2 (its job and backfill pick them up).

### Branch 2 `feat/aa-847-place-shape` (shape.py, sources, job, segments, Lines tab)
- **Country polygons**: Natural Earth admin-0 1:10m v5.1.2 (public domain), only the 18 catalog countries,
  simplified at 0.01° and rounded to 4 decimals → `services/places/geodata/country_polygons.json` (469 KB; builder
  `scripts/aa847_country_polygons.py`, offline). Point-in-polygon by latitude bands (0.25°), pure Python: a 2,000-vertex
  line clips in ~20 ms. A **3 km border buffer** counts a point near the border as inside: the Mekong IS the
  Laos / Thailand border, so its border stretch belongs to both countries (Vientiane is "in" Thailand by 3 km —
  deliberate). The cut at a border is bisected to ~50 m.
- **Name rule for lines is stricter than the geocode rule**: every distinctive token must match in BOTH directions
  (`shape.names_match`), after dropping the bake-off stop words, the linear words, the region's river words (nam,
  song, mae, sungai, jiang …), local road words (đường, jalan, thanon …), abbreviations (hwy, rd) and activity words
  (trek, hike). "Nam Ou" ≠ "Nam Khan" (the geocode rule's 0.5 with two tokens would pass them); "Nam Ou" ≠ "Nam Ou
  Bridge Road". The query's class must fit the feature's OSM tags (a river needs a waterway; a road a highway /
  railway; a trail or conceptual route may follow a hiking route, a path or a road).
- **Simplification is Visvalingam-Whyatt with a heap** (O(n log n)), not Douglas-Peucker with a growing tolerance:
  DP degenerated to O(n²) per try on dense lines (83 s on a 5,000-vertex test). The byte cap (12 KB, measured like
  `octet_length(jsonb::text)`, `", "` separators) sets the vertex target (≈ 23 bytes / vertex) and shrinks 15 % per try.
- **Approximate route = the longest anchored polyline of the line's tour-days** (previous stop → the day's point
  places ordered by distance from it → the day's stop), ≥ 2 anchors ≥ 2 km apart, points outside the country dropped.
  An approximate row is re-checked for an OSM feature every 30 days (it stays approximate when none is found);
  not_found retries after 30 days, failed after 1 day, at most 4 tries (`store.SHAPE_MAX_TRIES`).
- **`tour_segment` rows for every line use, even without a shape**: basis `feature`, no segment, `from` / `to` = the
  previous and this day's stop, so TripPlanner can draw the provisional dashed line between them (D1: never nothing).
  With a shape, `from` / `to` are the anchors the stretch was cut between (both the one anchor for a window).
- **Kill switch `PLACE_SHAPES_ENABLED` defaults OFF** (⚠️ underscore name — Nghiệp sets it by hand): a deploy never
  starts Place Details calls by itself; the 5-name trial turns it on. `GEOCODER_PROVIDER=model_only` turns off both
  Geoapify and Overpass (only the no-call anchored route is built).
- **Mutual exclusion via one session advisory lock** (`services/places/geolock.py`, key 847712001), tried, never
  waited: the run that finds it taken re-enqueues itself (place_shape 120 s, place_geocode 60 s). Chosen over a shared
  limiter + credit reservation: simpler, and the guard's "finished runs only" blind spot disappears.
- **Credits**: Place Details is ASSUMED 1 credit for `features=details` and +1 for `details.full_geometry`
  (`shape_sources.DETAILS_CREDITS` / `FULL_GEOMETRY_CREDITS`); `full_geometry` is asked only when the details answer is
  a point or cut short. Logged per run in `geo.geocode_call` (provider geoapify, endpoint place_details, credits);
  Overpass rows are provider overpass, credits 0 (they never count toward the Geoapify guard).
- **Line duplicate rule (D2)** `shape.same_line_place`: same country + same kind + same class + two-way name match +
  (same OSM id OR ≥ 70 % of the shorter geom_mid within 1 km of the other); two different names merge only when both
  are names of the same OSM feature (`osm_names` = place_shape.meta.names: "Red River" / "Sông Hồng"). **This is the
  codebase's ONE line-duplicate rule** (reconciled S246): the Problem 2 engine (`feat/aa-847-dedupe`, `dedupe.decide`
  step 2) calls `shape.same_line_place` for its auto-merge verdict and only adds a model band (shared OSM object or
  ≥ 30 % of geom_lo within 1 km → llm); its earlier "≥ 60 % of geom_lo vertices → auto" rule was dropped. This lane
  has no caller of `same_line_place` outside `tests/unit/test_aa847_line_pairs.py`: the automatic merge (D2) is
  delivered by the dedupe lane (job `place_dedupe`, CIS 257).
- **No dedicated LLM step for lines in this lane**: a line pair the deterministic rule cannot decide (no geometry yet,
  spelling variant without OSM names) is left to the Problem 2 engine's model check (it runs on every pair it caps at
  `llm`); duplicating that step here would create a second judge.

### Branch 3 `feat/aa-847-lines-views` (Migration B = CIS 256, on 2 + `feat/aa-847-places-l4` merged in)
- **256, not 255+n shared with the probe**: the probe's "255+" view follow-ups are not written; whichever lands later
  restates 256's bodies of `v_tour_place`, `v_tour_component`, `v_destination`, `_live_stop` and keeps the order.
- **Bbox columns: COALESCE inside the view, NOT NULL in the table**: `place_shape.min_lat …` are NOT NULL (pending rows
  carry the point), so COALESCE only covers destinations with no shape row (points). EXPLAIN as tripplanner shows the
  tile bbox filter pushed into the destinations ⋈ place_shape join below the barrier, and NO photo LATERAL planned for
  a read that selects no photo column (the S238 worry did not materialise: an unused one-row LATERAL is removed).
- **`v_destination_shape` granted to tripplanner only** (Booking does not draw maps); `REVOKE ALL FROM PUBLIC`.
  Rows for every usable line destination, also pending (shape NULL): the status tells the reader what to draw.
- **`length_km` / `segment_km` cast to double precision in the views** (numeric in the tables): JSON numbers for
  TripPlanner without Decimal handling.
- **The 256 test applies 253 + 256 inside ONE transaction and rolls back** (as the superuser with `SET LOCAL ROLE
  cis_owner` = DB Migrate's live-owner switch): appended columns cannot be dropped by CREATE OR REPLACE, so a
  committed apply could not be restored for the later modules.
- **The view reads of `v_tour_place` stay per `tour_id`** (TripPlanner's shape): a `source_id` filter is not pushable
  through the row_number window (partition `tour_id, day_number`), unchanged since 253.

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

- Branch 2: `services/places/shape.py`, `shape_sources.py`, `shape_run.py`, `geolock.py` (new);
  `services/places/geodata/country_polygons.json` + `scripts/aa847_country_polygons.py` (new);
  `services/jobs/place_shape_job.py` (new kind, registered); `place_geocode_job.py` (lock, enqueues place_shape when
  it stored a new line); `enqueue.SHAPE_KIND`; `store.py` (due_shapes, shape_uses, write_shape / write_shape_miss,
  line_shapes, lines_report, force_shapes_due, write / clear manual shape; swap_graph inserts geo.tour_segment);
  `graph.line_segments` + `Graph.segments`; `pipeline.build_tour_graph` cuts the segments (stats `segments`);
  `api/routers/admin_places.py` (GET lines, POST shapes/backfill, PUT / DELETE {id}/shape) + `staff_authz` POLICY
  (OPS_WRITE) + route snapshot; `frontend/app/admin/places/LinesTab.tsx` + the Lines tab; CONTEXT.md.
  Tests: `test_aa847_shape.py`, `test_aa847_shape_sources.py`, `test_aa847_shape_run.py`, `test_aa847_line_pairs.py`
  (unit, recorded-format fixtures `tests/unit/fixtures/aa847_shapes/`, no network), integration
  `test_aa847_place_shape.py` (255 CHECKs, store SQL, swap_graph → tour_segment, routes); `test_aa737` background set.

- Branch 3: `api/migrations/256_catalog_line_places.sql` (new), `tests/integration/test_aa847_migration_256.py` (new),
  conftest replay skips 256, CONTEXT.md (catalog line places). Root (`feat/aa-847-places-l4`): contracts
  `catalog.tour.v1.2.json`, `catalog.graph.v1.2.json` + README registry, ADR 0007 amendment (precision line; lines
  drawn; line duplicates merged automatically), two lessons, the ODbL note for Ms. Thư.
- Review fix (S246): the root ADR 0007 amendment now states only what is built — D1's real scope (CIS views after
  256; browse / trip maps after the flip) with gaps (a)–(c) listed open, and D2 for lines delivered by the dedupe lane
  with `shape.same_line_place` as the one line rule; the admin merge moves the DUPLICATE's shape row to the keeper
  (when the keeper's is not drawable), not "the keeper's row".

## Tradeoffs
- `linked_feature` labelled set: 52 link + 31 keep phrases (83). Measured: **0 false links, 0 wrong features, 0 missed
  links** on the set — the set was written alongside the rule (author-labelled, real atom places of the catalog
  calibration data + the design's examples + adversarial "Town on the X River" cases added after the first pass), so
  0/0/0 is a regression floor, not a field accuracy. The rule is conservative by design: "Huay Xai to Pak Beng on the
  Mekong River" stays as it is (a missed link costs a second destination for that phrase, a false link would draw a
  river for a town). Before the paid run, extend the set with the Dev atom places of the §9 probe.
- Skipping lines in the outlier rule instead of measuring distance to the line: no shape dependency in
  `place_geocode`; a wrong line point is caught by the shape acceptance rule (distance to the anchors) instead.

- Branch 2 line pairs (D2), 24 author-labelled pairs (13 same / 11 different; geometry synthetic along the real
  course): **0 false merges**, 9 / 13 same pairs merged, 11 / 11 different pairs kept. The 4 missed: the Laos-side Ho
  Chi Minh Trail approximate routes (> 1 km apart), "Kwai Noi" / "Khwae Noi" (no OSM names given), "Ha Giang Loop" /
  "Ha Giang Loop Road" (trail vs road class), and a pending pair with no geometry — all left to the Problem 2 model
  check, by design (a false merge would draw one river for two).
- Fixtures are recorded-FORMAT (the Geoapify Place Details FeatureCollection and Overpass `out body geom` JSON shapes),
  hand-built with real coordinates, not captured responses: no call was made. The 5-name trial on Dev should save its
  real answers as fixtures and re-run the unit tests on them.

## Should know
- **Hard order:** apply 255 (Infra copy `feat/aa-847-cis-255-copy`) BEFORE deploying this branch: `place_geocode`
  writes `line` (the old CHECK raises), `store.geocoder_requests_today` / `log_geocode_calls` read and write
  `credits` / `endpoint`, the validity report reads `geo.place_shape`.
- Do NOT apply the old linear-hide fix on Dev (it was never deployed). If it was applied, the ONLY recovery is its
  undo by audit id: the `linear_feature` fix cannot reach those rows (suspect rows are not in the report), so its
  un-hide branch never runs.
- `test_aa712_migration_226.py` drops schema geo and re-applies 226 alone; the modules that need 255 after it
  (`test_aa847_admin_places_validity.py`, `test_aa847_migration_255.py`, the 226 credit test) re-apply 255 (idempotent).
- Local evidence (throwaway PostgreSQL 16, lesson S246): integration 226 + 228 + 255 + admin_places_validity 43
  passed; unit places tests 745 passed + the new files.
- Branch 2: `PLACE_SHAPES_ENABLED` and `GEOCODER_PROVIDER` are underscore env names: Nghiệp edits them by hand in the
  task definition. The shapes backfill refuses (409) while the switch is off or the estimated credits pass today's
  remaining Geoapify credits; it does not know the paid place run's day (AA-905) — never run both on one UTC day.
- Branch 2 needs no migration beyond 255. The views that expose shapes and segments to TripPlanner are branch 3
  (Migration B).
- **One line-duplicate rule (reconciled):** `shape.same_line_place` (70 % of the shorter geom_mid by LENGTH within
  1 km, or the same OSM id) is the only auto-merge rule for two lines; `dedupe.decide` step 2 calls it and keeps a
  30 % geom_lo band for the model check. Two stale texts remain in other branches' files: `dedupe.py`'s module
  docstring step 2 still says "≥ 60 % of the shorter line's geom_lo vertices … → auto_merge" (dedupe lane's file),
  and `validity.is_same_place`'s docstring here points at a non-existent `services/places/line_dedupe` (the dedupe
  lane rewrites that function, so it is not edited here to avoid a rebase conflict).
- **D2 for lines is a hard prerequisite of the flip.** `validity.is_same_place` returns False for any line, so until
  the dedupe lane merges, line duplicates are neither merged nor counted in `GATE_REASONS`: the flip gate could pass
  with two overlapping copies of one river drawn.
- **D1 built scope (review S246, root ADR 0007 amendment corrected):** lines are drawn only where TripPlanner reads the
  CIS views after 256 — the tour-page static maps (always on the catalog views) and, after the flip
  (`PLACES_SOURCE=cis`), the browse and trip maps. Open gaps: (a) before the flip the browse / trip maps read the
  frozen TripPlanner tables (no precision), so 6d's `isExactPin(precision == null)` still pins a linear place; (b) a
  line with no geometry (`pending` / `not_found` / `failed`) is not drawn on the browse map (`MapView` filters on
  `isDrawable`) or a static map; (c) the trip map draws the provisional dashed line only when
  `segment_from` / `segment_to` are two consecutive trip stops (`lib/lines.ts` `planTrip`; a provisional stretch is
  never an extra). These are open questions for Nghiệp, not built.
- Branch 3 order: 253 applied → 255 applied → 256 (Infra copy `feat/aa-847-cis-256-copy`) → the bot schema-reference
  PR merged → then TripPlanner 6d may read the new columns. 256's closing check refuses when a row count of
  `v_tour_place` / `v_tour_component` / `v_destination` changes or a bbox is NULL.

