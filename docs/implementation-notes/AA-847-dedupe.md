# AA-847 — Problem 2: automatic duplicate-place merge (S246)

Design: root `docs/audits/2026-10-21-S246-aa847/dedupe/` (`README.md`, `dedupe_proto.py`, `pairs.json`) and the
revised plan of the skeptic pass (S246, 12 changes: id evidence no longer bypasses the caps, migration 257, line
overlap, undo by relink, group-level clash rule, keeper order, no new embedding table, two-model agreement, full
recompute, allow-list table, breaker outside the approved diff, TripPlanner redirect). Kept in its own notes file so
parallel AA-847 lanes do not edit the same file (lesson S242). Stacked on `feat/aa-847-place-shape` (migration 255,
`geo_precision 'line'`, `geo.place_shape`).

## Decisions
- **Migration number 257, not the plan's 256:** the lines lane claimed 256 (`256_catalog_line_places.sql`, CIS
  `feat/aa-847-lines-views` + Infra `feat/aa-847-cis-256-copy`) while this lane ran. 257 does not depend on it.
- **Owner decisions S246:** (1) lines are drawn, so two lines are merged on their geometry; (2) duplicates are merged
  automatically; the admin duplicate fix stays as an override, not the normal path.
- **One decision function** `services/places/dedupe.decide(a, b, ctx)` → `auto_merge` / `llm` / `keep` (rule
  `r1.4`), used by the `place_dedupe` job, by the in-tour dedupe of `place_extract` (`in_tour_remap`, rules band only)
  and by the validity report (`validity.is_same_place` / `clusters` delegate to it; V2 `near_duplicate` = an
  auto_merge pair not merged yet, new informational `possible_duplicate` = an llm pair).
- **Id evidence is an input, not a verdict (§0.1).** A shared `geocoder_place_id` auto-merges only when both
  results are POI / natural (`amenity`, `building`, `unknown` — Geoapify returns peaks and lakes as `unknown`), the
  names are exact / fuzzy and no cap applies; otherwise a shared POI object sends the pair to the model (the model is
  told). A shared admin-area id (city / suburb / district …) is no evidence at all. The same place card follows the
  same rule.
- **"Precision mismatch" cap = geometry class mismatch** (point / area / line from the stored line or
  `geomatch.feature_kind`), not the `geo_precision` column: `area` there also means "the model's own point", and
  capping every confirmed point vs model point would cap the core in-tour case (Kuang Si Falls / Tat Kuang Si).
- **Lines (§0.3):** two lines with drawn shapes (`ok` / `manual`): the auto verdict is the lines lane's own rule
  `shape.same_line_place` (same country, kind and class, a two-way name match, and the same OSM id or ≥ 70 % overlap
  by length on `geom_mid`; measured on its labelled `aa847_line_pairs.json`) — ONE line rule in the codebase, so I
  dropped my 60 %-of-vertices auto rule. What it does not merge goes to the model when the lines share an OSM
  object or ≥ 30 % of the shorter `geom_lo` lies within 1 km of the other; else a SOFT keep (two stretches of one
  river that only touch can both merge into the river). A class mismatch is a hard keep only when both classes come
  from the OSM tags (`meta.line_class`), not when one is guessed from the name. A line without a drawn shape (pending, approximate) and a point vs a line go through the point rules with a
  50 km radius and are capped at llm (`shape_not_drawn`, `geometry_mismatch`, `linear_point_radius`). Two lines of
  different subtypes (river / road / trail / rail / canal) are a hard keep. `range` is an area since the lines lane.
- **Keeper order (§0.6):** manual > an existing keeper (never swapped out) > a line with an ok shape, the longest
  first (the whole river, not a stretch), then any line > a Geoapify point that is not an admin area > a Geoapify
  settlement > a card > most live components > oldest > id. Length is my addition: among several drawn lines of one
  river the whole feature must be the keeper.
- **Group-level rule (§0.5, lesson S239):** a member merges only on its own edge with the keeper; no pair in the
  merged set may be a hard-rule keep or a `different` verdict / pin — including the rows already merged into the
  keeper or into a member (else a clash could be bypassed over two runs). The cluster is cut to its largest
  consistent subset (exact up to 12 members, greedy beyond) and the cut members are reported as conflicts. A soft
  keep (low score) does not block: the plan's "(hard rule, LLM verdict or pin)".
- **Embedding (§0.7):** no new table and no paid call — the job reads the max cosine between the two destinations'
  `geo.tour_place.embedding` rows; it only promotes a soft keep (≤ 1 km, compatible kinds, cosine ≥ 0.8) to llm.
- **LLM band (§0.8):** primary `a3_place_dedupe` (GPT-6 Luna, Bedrock acc3) asked A/B and B/A, then
  `a3_place_dedupe_check` (Claude Haiku 4.5, acc3); merge only when all three answer `same` with confidence ≥ 0.8.
  The final verdict is `different` when any answer said different (it blocks clusters), else `unsure`. Calls stop at
  the first non-agreeing answer.

- **Runtime (PR B, branch `feat/aa-847-dedupe-job`):** job kind `place_dedupe` (platform, debounced, background,
  cap 1). Full recompute per country each run (no watermark: `shared.destinations` has no `updated_at`); only the
  model calls are incremental (cache by input hash — names, rounded points, kinds, line status, rule version, prompt
  sha, the two configured models; NOT the day-text excerpts, so a re-extract does not ask again).
- **The allow-list gates the spend too:** a country gets the paid model band only when it is on
  `geo.place_dedupe_country` (dry_run or apply). Not listed = rules-only report, $0. This makes the §6.3 dry run on
  Laos + Vietnam "add the two countries in dry_run", and nothing is spent before someone lists a country.
- **Approval = the whole diff, not only its hash:** `approved_diff` (jsonb) is stored next to the hash (the plan's
  table had only the hash) because the breaker counts the changes OUTSIDE the approved diff. The approve endpoint
  takes the diff from the latest succeeded `place_dedupe` job result (the diff is never truncated there) and refuses
  (409) a hash that is not the latest or a run of another rule version.
- **Two-vendor check at run time:** a check answered by the primary's vendor (a fallback) counts as `unsure`.
- **Undo:** by merge id, in one transaction under the lock: 409 on a later dependent merge (the keeper was merged
  since), on a name-key collision (`destinations_name_country_uidx`) or when the merged row points elsewhere;
  restores the listed pointers that still hold the keeper (geo.tour_place too: component ids are stable across
  re-extracts), reports the rest as `left`, clears `duplicate_of`, restores flattened members, pins `different`, runs
  the relink and queues the graph build + a dedupe run. Admin merges (decided_by `admin`) are undone with the
  validity-fix undo (it also restores the shapes it moved); their undo closes the log row and pins the pair.
- **Re-check of job merges:** undo only on other country / kind class (`different_kinds`, `town_vs_landmark`,
  `linear_subtype_differs`) or a move past 2 × the radius; pin source `recheck`; never an admin merge.
- **Alert:** `held_volume` writes an admin bell row (`shared.notifications`, event `place_dedupe_held`) and queues
  an `ops_alert_email` with the new template `place_dedupe_held` (recipients = `shared.alert_recipient`, AA-913).
- **VACUUM (ANALYZE) geo.tour_place** after ≥ 50 repointed components is attempted only when the runtime role owns
  the table; as `cis_app` (not the owner, X1) it records `skipped_not_owner` and autovacuum does it.

## Changed
- `services/places/dedupe.py` r1.4 (new), `tests/unit/test_aa847_dedupe.py` (89 tests),
  `tests/unit/fixtures/aa847_dedupe_pairs.json` (134 pairs: + 4 revised-plan adversarial keeps, + 10 homonym keeps).
- `services/places/geomatch.py`: one kind-class map (`KIND_OF_WORD`, `PHRASE_KINDS`, `LINEAR_SUBTYPES`) shared by
  dedupe; `rapids` class; `range` no longer a linear subtype.
- `services/places/validity.py`: the old same-place lexicon (`same_name`, `place_kinds`, `NEAR_KM` …) is gone; V2
  delegates to dedupe; `Place` carries `place_id`, `geocoder_name`, `geo_status`; reason `possible_duplicate` (not a
  gate reason). `pipeline._in_tour_keepers` → `dedupe.in_tour_remap`; `store.destination_briefs` returns the extra
  columns. `tests/unit/test_aa847_lines.py::test_point_distance_rule_never_merges_a_line` checks `[0]` only (the km is
  now returned).
- Versus the plan: the rules-recall gate of §6.1 (≥ 0.75) is not met — see Tradeoffs; the CI floor pins the measured
  0.68.
- PR B: migration `257_geo_place_dedupe.sql` (`geo.place_merge`, `geo.place_pair_verdict` (pins = input_hash `*`),
  `geo.place_dedupe_country` (+ `approved_diff`), stage rows, budget row, `catalog.v_destination_redirect` by the X1
  S22 catalog recipe; conftest skips it, `tests/integration/test_aa847_migration_257.py` applies it the DB Migrate
  way — 10 tests green on a throwaway PG16 cluster). `services/places/merges.py` (SQL), `services/places/dedupe_run.py`
  (one run), `services/jobs/place_dedupe_job.py` (+ `SAFE_DEFAULTS`), `store.lock_destinations` taken by the
  place_geocode upsert and relink, the place_extract persist and the admin fix; `validity_fix` duplicate merges write
  `geo.place_merge` (when 257 is applied); `place_geocode` queues `place_dedupe` after an insert / move;
  `enqueue.enqueue_dedupe` for the lines lane's `place_shape` (not wired here: that file is the lines lane's, still
  uncommitted); `/admin/places` Merges tab (`MergesTab.tsx`) + 3 GET and 3 POST routes (POLICY ops.write, route
  snapshot regenerated); `ops_alerts` template `place_dedupe_held`.

## Tradeoffs
- **Offline evaluation, rules r1.4, 134 pairs (61 merge / 73 keep), $0:**

  | Run | Auto precision | Rules recall | False merges | LLM band merge / keep | Recall if the check is right |
  |---|---|---|---|---|---|
  | Full metadata | 1.000 | 0.689 (42/61) | 0 | 17 / 6 | 0.967 |
  | No ids, no stops, no cards | 1.000 | 0.689 | 0 | 14 / 5 | 0.918 |
  | All points the model's | 1.000 | 0.623 | 0 | 18 / 15 | 0.918 |
  | Every Geoapify row on one city id | 1.000 | 0.066 | 0 | 52 / 15 | 0.918 |
  | No shapes | 1.000 | 0.623 | 0 | 19 / 5 | 0.934 |

  Rules recall is 0.689, below the plan's 0.75: the plan's own §0.1 moves the three cross-language pairs that r1.2
  merged on a shared Geoapify id (Van Mieu / Temple of Literature, Wat Arun / Temple of Dawn, Tiger's Nest / Paro
  Taktsang) to the model, and the caps it lists (soft settlement, heritage museum, bare far, geometry) hold the rest.
  I did not loosen a rule to reach the number (lesson 125). Recall if the two-model check answers the band right:
  0.967. Missed by everything offline: Temple of Literature / Van Mieu - Quoc Tu Giam (needs the embedding promotion,
  not evaluated offline) and Nam Ou / Nam Ou River without shapes (116 km, never merged on points — by design).
- The six keep pairs in the band (Long Bien / Long Bien Bridge ×2, Hoan Kiem / Hoan Kiem Lake on the suburb id, Lao Cai
  / Lao Chai 2.7 km, Dhammayangyi / Dhammayazika model point, Cu Chi / Cu Chi Tunnels model point) are what the
  two-model check must answer `different` / `unsure`: the paid measurement run (§6.2) measures exactly this.
- One pair (Hoan Kiem / Hoan Kiem Lake on one suburb id) is gated in the full-metadata run only: without the suburb
  result it is the s246 merge pair "bare name of a lake" — the label needs the metadata.

## Should know
- **Order on Dev:** 255 → 256 (lines lane) → 257 (Infra copy `feat/aa-847-cis-257-copy`) → the DB Schema Dump PR → this app PR. The
  job is inert until a country is listed: deploy, list Laos + Vietnam in dry_run, label a blind sample of the run's
  pairs (§6.3), then approve per country.
- **Paid runs not done:** every model call here is a stub. The measurement run (§6.2) and the first listed dry run
  spend money — quote in the final report; nothing runs before a country is listed.
- **Lines lane coordination:** rebased on `feat/aa-847-place-shape` ebbf17a4 (its `place_shape` job); `place_shape`
  now queues `place_dedupe` when a shape reaches `ok`. Its `services/places/geolock.py` is a different lock (one
  Geoapify client at a time) and stays next to `store.lock_destinations`. The lines lane's `validity_fix.shape_merge` still moves shapes for ADMIN merges;
  job merges leave `geo.place_shape` on the merged id (PK collision) and the keeper order prefers the drawn line.
- **Branch stack:** `feat/aa-847-place-shape` (ebbf17a4) → `feat/aa-847-dedupe` (PR A) → `feat/aa-847-dedupe-job`
  (PR B). The lines lane's views (CIS 256, `feat/aa-847-lines-views`) are a sibling; 257 does not need them. The
  conftest skip lines (253 / 256 / 257) conflict trivially when the branches meet.
- **The pair set is still author-labelled** (79 constructed pairs from r1.2, + 24 review / skeptic / homonym pairs I
  or the review built). Before `apply`: the paid measurement run and a blind ≥ 60-pair sample of a Laos + Vietnam dry
  run (plan §6.2 / §6.3).
- `geocoder_place_id` is the strongest signal — never clear it on re-locate.
