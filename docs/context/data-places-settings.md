# Schemas, photos, places, infra limits and settings

> Detail page of [CONTEXT.md](../../CONTEXT.md): owned schemas, retired tables, photos, the place pipeline, infra limits and the settings registry. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

- **Schemas** (owned here): `silver_aa_internal` / `gold_aa_internal` (raw→published tours),
  `acp_contract.*` and `acp_shared.*` (the pipeline tables — atoms, segments, ranking, routes,
  hubs, subjects, pieces, facts), `geo` (place pipeline, AA-712), migrations under `api/migrations/`.
- **Removed as dead code (15-21/09)**: the N7/N8 produce/deliver write path and its 5 tables
  (AA-603; `acp_deliver.tenant_tour_pages` is kept), the `/v1/s1-from-atom` route (AA-611), and
  the atom "star" (AA-609; the DB column `tour_atoms.starred` still exists). Do not rebuild on
  any of these.
- **Quarantine schema `retired` (data standards DM-3.9, CIS 294, AA-850)**: dead CIS tables and views wait there
  for their drop migration (>= 14 days later unless Nghiệp waives the wait) with no grant to any role (`cis_app` has
  no USAGE: a forgotten reader fails with 42501). Owner migrations only; never read or write it from code. Wave 1
  (quarantined by 294, dropped by 296 with the 14-day wait waived in S255; empty `retired` stays for the next waves):
  `acp_contract.atom_decompose_jobs`, `s1_from_atom_runs`, `route_pick`; `acp_shared.marketplace_portfolios`,
  `tenant_atom_state`; `shared.acp_runs`, `tenant_brand_rule_versions`, `tenant_brand_rules_deleted_aa404`,
  `v_batch_stats`, `v_pipeline_summary` — none of these exists any more, do not rebuild on them. The tenant erasure
  skips `retired` (`services/erasure/inventory.py` `QUARANTINE_SCHEMAS`; an FK child there is a `quarantined_child`
  information line). Registry: AA-Ecosys `docs/audits/2026-10-28-S255-aa850-dead-tables/registry.md`.
- **The ACP v1 stage schemas are gone (CIS 299, AA-850)**: `acp_gold_output`, `acp_silver_s2`, `acp_silver_s3`,
  `acp_silver_s4`, empty since 121 / 208, were dropped (no quarantine: no object to wait). The `/v1/social` router
  over `acp_silver_s4.social_content` (every call had failed since 121) went with them. Do not recreate them.
- **ACP v1 run model is gone (AA-751, 16/10)**: `acp_shared.acp_runs` was dropped by migration 121;
  its last readers (`GET /admin/acp/stuck-runs`, `POST /admin/acp/runs/{run_id}/force-fail`,
  `services/acp_shared/cost_utils.py`, `services/acp_canary/`) are deleted. Stuck or long work is a
  `shared.job` row (reaper + admin Jobs page). `tests/unit/test_aa751_no_acp_runs_reader.py` fails
  on any new SQL against `acp_runs`. `infrastructure/canary/` (old-account EventBridge skeleton) is deleted
  too: its schedule and Lambda no longer exist (checked 16/10/2026).

- **Photos (AA-708, migration 198)**: the `photo_sync` job copies the Jira CON board's PHOTOS
  Drive folders (public, read with a Google API key from `aa-cis/dev/gdrive-photo-reader`) to S3
  (`photos/<country>/<file id>-{1600,960,600}.webp` + the original) and records `shared.place_photo`, matched to a tour
  (folder name) and to a `shared.destinations` place (file name; see below). Images are public via
  `GET /content/photos/{id}?size=`, the stable path every consumer stores (catalog photo views,
  TripPlanner covers and `og:image`, Booking `CisPhoto`). AA-844: it answers a 302 to the photos CDN
  (CloudFront `img.lumiguides.it.com`, AA-CIS-Infra `photos_cdn.tf`, in front of the bucket's
  `photos/` prefix) as `<host>/<slug>/<file>?v=<sha256[:12]>`, cached 1 day; `PHOTO_CDN_BASE=off`
  falls back to a presigned S3 URL (1 h, redirect cached 50 min). The API stays in the image path
  (one hop + one row read per view); CloudFront serves the bytes. A rejected photo is 404 on the
  route, but a CDN URL handed out earlier keeps working (the object stays in S3, edge cache 1 year)
  — reject is reversible, so objects are not deleted. A daily schedule (Infra ops-probe Lambda,
  21:00 UTC = 04:00 ICT) calls `POST /admin/photos/sync/schedule`: one incremental `photo_sync` job
  per CON country, idempotent per UTC day, skipped while another `photo_sync` job is queued or
  running (not skipped during a rerun wave: no LLM work); `/admin/photos/summary` reports it as
  `last_scheduled`. (Before AA-993 G5 those serial jobs held the deploy gate around 04:00 ICT; they no longer do.)
  AA-671 S257 — **photo → destination**: a photo is compared only with the places of ITS OWN tour (the `geo.tour_place`
  rows of the tour's source key + the current graph build's stops, non-duplicate destinations;
  `services/photos/destinations.py`), and only an unambiguous answer of a strong rule is written
  (`match.resolve_destination`: exact name, core name, a place name inside the label; a tie = ambiguous; a label found
  only inside one place name — a town label vs its landmark — is `weak`: counted, never written).
  `place_photo.destination_match_source` (migration 303) says who set the destination: `manual` (the assign route) is
  never changed by the sync, the job or a folder-wide tour correction; `auto` follows its tour (emptied or re-answered
  when the tour changes). Every sync fills an empty `destination_id`; `match_destinations` additionally re-matches the
  automatic destinations of the tours whose places it read (never after a failed read), and fills empty
  `shared.destinations.cover_image_url` (read by the TripPlanner). The photos synced before get theirs from the
  `photo_destination_match` job (`POST /admin/photos/destination-match`, DRY RUN by default, background, $0: counts
  matched / ambiguous / weak / no_match / no_candidate / no_label per country with samples and previews the covers;
  an apply writes in one guarded UPDATE; covers only with `fill_covers`). `destination_id` feeds
  `shared.v_destination_photos` / `catalog.v_destination_photo` and so the pool cover chain and the TripPlanner
  destination pages. S258: the job's matcher version 2 (`{"version": 2}`; the sync stays on 1) breaks a tie between
  places on one spot (every pair ≤ 2 km: most tour days wins) and matches a label that IS the name of a place ≤ 20 km
  from a place of the tour (`nearby_exact`: the town Paro, not the monastery above it). The pool cover chain
  (`services/photos/covers.py`, both pools, also run by the job's `covers_coverage` report): tour photo → a photo of
  the primary place (most days) → of the photo place nearest to it (≤ 25 km) → of a photo place of its country.
  AA-995 (migration 301): `size=medium` is the 960 w file (`s3_key_medium`, phone heroes of the TripPlanner); a row
  without it answers the 1600 w file (never the original). The sync writes it for every file it downloads; files stored
  before 301 get it from the `photo_medium_backfill` job (`POST /admin/photos/medium-backfill`, background, $0: S3 +
  Pillow on the worker, rendered from the stored original, no Drive download; bounded by `limit`, resumes with what is
  still missing, stops while a `photo_sync` runs). `/admin/photos/summary` counts `with_medium`.
- **Place pipeline (AA-712, ADR 0007, migration 226)**: CIS — not TripPlanner — computes each live
  tour's day skeleton (start / end / overnight), places (= itinerary components; id = uuid5(source, day, itinerary spelling, country, activity) at a place's first build, and AA-989: a rebuild keeps the previous row's id for the same place respelt, retyped or moved a day — `components.carry_over_ids`, unambiguous one-to-one pairs only — because TripPlanner trips pin components by id; AA-990: a generic name ("Night Market") keys the carry-over only together with its town; every other dropped id gets a redirect to the component of the same place (destination → card → spelling + country → link name, nearest day; generic names in the same town only), written to `geo.component_redirect` (CIS 285) in the transaction of the `geo.tour_place` rewrite (ids served again are cleared) and served to TripPlanner as `catalog.v_component_redirect`, whose readers follow the chain together with `tripplanner.component_id_map`; `components_hash` includes the atom ids),
  geocoded destinations and the tour graph (stops, nodes, edges, legs, junctions) into the CIS-internal
  schema `geo`, keyed by `source_id` (catalog identity). Jobs (`services/jobs/place_*`,
  `tour_graph_job.py`; logic in `services/places/`): `place_extract` per tour (after A3 atomize — automatic only
  with `PLACE_PIPELINE_AUTO` on or, AA-989, when the tour's source already has a `geo.tour_build` row ("maintain
  what exists"; coverage expansion stays gated, S236) — set-active / restore, admin backfill `POST /admin/places/backfill`, ≤ 50 tours of a country or an explicit
  `tour_ids` list ≤ 100 (AA-847), dry-run cost estimate priced at each stage's live route and catalog price,
  refused during a wave); `place_geocode` (debounced, the ONLY writer of
  `shared.destinations` coordinates) — the model proposes a point (`a3_place_locate`), **Geoapify**
  (OpenStreetMap data; S236 decision, all countries; `GEOCODER_PROVIDER` = `geoapify` default |
  `model_only` for tests / emergency; `mapbox_permanent` / `amazon_location` fail as not supported —
  Mapbox is the basemap only, Amazon Location is not used) confirms it with the bake-off acceptance
  rule (`services/places/geomatch.py`: top 3, name match, no admin-area types, ≤ 25 km / 50 km for long
  features; `rank.confidence` recorded, never a gate) → `located_by='geoapify'`, `geo_precision='point'`.
  **AA-997 (S254 geocode audit): coordinates come from the geocoder only.** With a tour route (the located overnight
  stops of days d-1..d+1 of the tours using the name, `store.route_anchors` / `guards.route_km`, ≤ 50 km for a site,
  100 km for a long feature; a day trip ≤ 150 km off the route passes only as a second choice when the answer also
  agrees with the model's point) the route is the distance reference instead of the model's point, which is only a
  hint (Geoapify `bias`); unconfirmed → the town the model names, geocoded as a settlement (`area`,
  `review_reason='centroid_fallback'`); else no point (`unconfirmed_by_geocoder`, `unknown` at once). A model null gets
  one geocoder-only search when a route exists, else it is final. China searches cover HK / MO and a point there is
  stored as Hong Kong / Macao. Only `GEOCODER_PROVIDER=model_only` still stores the model's point (`llm`, `area`,
  `review_reason='llm_point'`). Names that are not places (route legs, generic lodging, activities, regions,
  descriptions: `services/places/name_filter.py`) never reach a paid call (`unknown`, `last_error='non_place:<cause>'`).
  Existing model-placed rows are re-located by the job kind `place_relocate` (`POST /admin/places/relocate`, dry run
  first; flags `off_route` / `llm_point`, optional hide; no model call; 1,000 daily credits kept for place_geocode).
  Its no-call modes hide flagged pins from every reader (`suspect`): `hide` by review reason, `hide_flagged` (S260)
  only rows a run already stamped `llm_point` / `off_route` and left on the model's point (report, then confirm,
  audited; a confirm hides only the rows its report listed); `unhide` reverts by stamp or by id (a restore by id
  stays shown on later hides), and a hidden row a later run confirms is shown again.
  Generic phrases ("ger camp", "local market") are not geocoded. Free plan 3,000 credits/day: ≤ 5 req/s, retry on
  429/5xx, daily credit guard 2,500 (UTC day, `geo.geocode_call`); AA-990: a run stopped by it with names still
  due queues the next run for 00:10 UTC + jitter (`enqueue.enqueue_after_credit_reset`, one row per reset day, queued
  geocode rows moved there too), and the data coverage page flags names due for > 24 h (`geo.names_stalled`). The
  locate answer is read leniently (`locate.json_object`, re-spelt keys by `name_norm`), an unreadable batch is asked
  again as two halves, and a name without a point carries its reason (`unknown_to_model` = null,
  `missing_in_answer`, `malformed_answer`, `unknown_country:<c>` = names no ISO country). Key: Secrets Manager
  `aa-cis/dev/geoapify` (plain string), worker task role reads it; never logged. **Attribution
  required wherever these coordinates are shown (CIS admin and every consumer, TripPlanner first):
  "Powered by Geoapify" (link) + "© OpenStreetMap contributors"** — also kept per row in
  `geocoder_meta.attribution` (Geoapify storage terms). Hard country/bbox guard on the PLACE's own country — any ISO
  3166-1 country (AA-990), never the tour's: a tour sold as India may climb Kilimanjaro, whose places are Tanzanian
  (cross-border is normal, not an error) — transit-aware outlier
  rule → `suspect`; admin-fixed (`manual`) rows are never overwritten; `tour_graph_build` (debounced, versioned swap, current + 2
  kept). Stages `a3_place_days` (Haiku 4.5, Sonnet fallback, migration 260), `a3_place_locate` (Sonnet, Haiku
  fallback, migration 261: on Haiku its coarser points left most names `llm`), `a3_place_embed`
  (Cohere 1536-d). `shared.destinations` is unique on `(name_norm, country, locality_norm)` (same
  name in two countries = two places). Other apps will read it only through `catalog.*` views
  (migration 228, next slice); TripPlanner's own extraction is frozen.
- **Place kinds and the content-validity gate (AA-847).** `geomatch.feature_kind` names a place that is not a
  pin: a **linear** feature (trail, river, road, loop, stream, path, circuit, expressway …) is stored
  `geo_precision='line'`, `geo_status='ok'` and DRAWN as a line (owner decision S246: never a pin, a circle or
  hidden): `place_geocode` inserts its pending `geo.place_shape` row and never moves its point for shape reasons (it
  stays the only coordinate writer); an **area** feature (national park, delta, plateau, coast, gorge, mountain
  range) is `area` even when Geoapify confirms a point. A name that names a point on the feature ("River Kwai
  Bridge", "Kuang Si Falls", "Peradeniya Railway Station") stays a point. A descriptive atom place around a feature
  ("sandbank on the Nam Ou River", "Annapurna Circuit Trek") links its component to the feature's destination
  (`geomatch.linked_feature`, labelled set in `tests/unit/test_aa847_linked_feature.py`). A line destination is never
  a graph stop or node, never in the outlier rule and never in the point-distance same-place rule.
- **Line geometry (AA-847, CIS 255).** `geo.place_shape` (one row per line destination: status pending / ok /
  approximate / not_found / failed / manual, GeoJSON at ≤ 2,000 / 300 / 60 vertices clipped to the country, non-null
  bbox, `osm_type`/`osm_id`) is written only by the job kind `place_shape` (and the admin's manual GeoJSON);
  `geo.tour_segment` (the stretch a tour-day follows, versioned with `geo.graph_build`) only by `tour_graph_build`.
  The Geoapify daily credit guard sums `geo.geocode_call.credits` of every endpoint (search + Place Details).
  `place_shape` (debounced, cap 1, background; off unless `PLACE_SHAPES_ENABLED`; `GEOCODER_PROVIDER=model_only` = no
  call) takes the geometry from Geoapify Place Details by the stored place id, then Overpass (free, ≤ 1 request / 2 s)
  when that is missing or only one OSM way; `services/places/shape.py` accepts a candidate on name (name / name:en /
  int_name / name:*), kind (waterway / road / route), the country POLYGON (Natural Earth admin-0,
  `services/places/geodata/`; not `guards.BBOX`), distance to the tour-day anchors and a kind-aware length, and
  prefers a relation or merged same-name ways over one way. No OSM feature → the anchored approximate route of a
  tour-day (status `approximate`, drawn dashed "approximate route"); no anchors either → `not_found`, retried and
  listed on `/admin/places` (Lines tab; `POST /admin/places/shapes/backfill` dry run first, ≤ 50 names;
  `PUT|DELETE /admin/places/{id}/shape` manual GeoJSON). `place_geocode` and `place_shape` share one advisory lock
  (`services/places/geolock.py`): one Geoapify client at a time. `tour_graph_build` cuts each day's stretch
  (`shape.day_segment`: between the previous and this day's stop, or ±15 km around one, or none = the whole
  feature). Two lines are the same place only on the same country + kind + name and the same OSM id or ≥ 70 % overlap
  (`shape.same_line_place`; labelled pairs `tests/unit/fixtures/aa847_line_pairs.json`).
- **Automatic duplicate merge (AA-847 Problem 2, CIS 257; owner decision S246: no manual merges as the normal
  path).** One decision `services/places/dedupe.decide(a, b)` → auto_merge / llm / keep (rules `r1.4`, pinned by
  `tests/unit/test_aa847_dedupe.py` on a labelled pair set: 0 false merges in every metadata mode). A shared geocoder
  id or place card is evidence, never a verdict (it auto-merges only POI / natural objects with exact / fuzzy names
  and no cap); two drawn lines merge on their shape overlap, a line without a drawn shape or a point vs a line is at
  most the model's call. The job kind `place_dedupe` (debounced, background, cap 1; queued by `place_geocode` when it
  wrote a destination, by `place_shape` (`enqueue.enqueue_dedupe`), by an undo, by the admin) recomputes every pair
  per country, asks the middle band of allow-listed countries to two models that must agree (`a3_place_dedupe` GPT-6
  Luna A/B and B/A, `a3_place_dedupe_check` Haiku; verdicts cached in `geo.place_pair_verdict`, budget
  bedrock/job:place_dedupe $0.50/run, ≤ 150 pairs/run) and writes only for countries in mode `apply` of
  `geo.place_dedupe_country` (the approved dry-run diff; more than max(5, 1 %) changes outside it → `held_volume` +
  alert). `place_dedupe` is the only writer of `duplicate_of` (the admin fix is an override); every merge is a
  `geo.place_merge` row (repointed rows, flattened members) and is undone by relink (`POST
  /admin/places/merges/{id}/undo`, `different` pin). place_geocode upsert / relink, the place_extract persist, merges
  and the admin fix take one advisory lock (`store.lock_destinations`). `catalog.v_destination_redirect` maps a
  merged id to its keeper (TripPlanner answers 301). `place_extract` maps a tour's in-tour duplicates with the same
  decision, rules band only (`dedupe.in_tour_remap`). `services/places/validity.py` is the deterministic report (reason codes V1 duration / V2 same place / V3
  not a pin / V4 day text; V2 is `dedupe.decide`: `near_duplicate` = an auto_merge pair not merged yet,
  `possible_duplicate` = a model-band pair, informational), run after every
  `tour_graph_build` (counts in `geo.graph_build.stats.validity`) and on `/admin/places` (Coverage + Content
  validity tabs; `GET /admin/places/validity|coverage`, `POST /admin/places/validity/fix` dry run by default, apply
  refused with 409 when the plan changed since the dry run's `fingerprint`, audit row with the prior values,
  `undo=<audit id>`). The **flip gate** (`validity.GATE_REASONS`, `summary.gate`) counts only map defects the fixes
  clear: `near_duplicate`, `linear_feature` (a linear name not stored as a line), `area_feature`,
  `linear_without_shape` (a line whose shape is not ok / manual / approximate); `same_destination_names` (one pin,
  several spellings),
  V1 and V4 are informational. A duplicate merge sets `duplicate_of` and repoints the CIS rows
  (`geo.tour_place`, `geo.tour_day`, `geo.place_name`, `acp_shared.place_card`, `shared.place_photo`); Booking and
  TripPlanner rows are reported, never written; `store.known_destinations` / `upsert_destination` answer a merged
  key with its keeper, so a rebuild does not recreate the duplicate.

### Infra limits: the Terraform layer, read-only (AA-1096)

Platform › Infra limits (`/admin/platform/infra`, API `GET /admin/platform/infra`, ops.read, admin only in the BFF)
shows the limits the S279 thresholds audit keeps in Terraform on purpose (section 4): WAF rate rules and the country
allow-list, API Gateway throttles, Lambda reserved concurrency, the acc2 AWS Budgets and the alarm thresholds. No app
role may write them. Source: the AA-CIS-Infra root output `platform_limits`, rendered by its docs generator into the
section "Edge, budget and alarm limits" of the root page `docs/generated/alarms.md` (OPS-07) and published by Docs sync;
`api/services/platform_infra.py` reads that page through the docs reader (no AWS or Terraform state access, $0) and
adds, per value, a plain label in English and Vietnamese, its unit and the Terraform variable or resource that sets it
(`CATALOG`; a value it does not know is still shown, a catalog key the page lost is listed as stale). Names on the page
are display names: the account label of an output name (`acc2_total_monthly_usd`) is dropped (`display_key`), since
the admin UI never shows acc1 / acc2 / acc3; matching keeps the raw name. A change is an
AA-CIS-Infra PR + apply; the page shows it after the next docs run is merged into the root repo.

### Settings registry: every key ships its guide (AA-1096, CIS 407 / 416)

Platform › Settings (`/admin/platform/settings`, API `/admin/settings/registry`) lists every admin-editable threshold,
limit, cap and policy value (`shared.setting_definition` + `shared.setting_value`, reader `shared/settings`). The page
serves the definitions from `shared/settings/compiled_defaults.json`, generated from the migration blocks by
`scripts/settings/gen_compiled_defaults.py` (`settings-seed` = the definition rows, `settings-guide` = plain label +
guide; `tests/unit/test_s279_settings_seed_parity.py` keeps them equal).

**Rule (tech lead S279, guard `tests/unit/test_aa1096_settings_guides.py`):** a migration that adds a key also gives it
an operator's guide in English and Vietnamese, `guide = {"en": {what, raise, lower, when, example}, "vi": {...}}`
(a `guide` field on the seed row once CIS 416 is applied, or a `settings-guide` block), and a plain label without stage
codes or issue ids (the key, shown small on the page, keeps the code). `what` = what it controls and where the effect
shows in the product; `raise` / `lower` = the effect and risk of each direction (an on/off key: turn on / off; a
choice: the stricter / the looser choice, and the text agrees with that heading); `when` = the signal to change it
("many blogs held for being too short"); `example` = the default in plain terms, worded "With the default of ..." and
never "Today ..." (the value in force changes; the page computes it where it matters, `nowExample` in
`frontend/lib/settingsRegistry.ts`). A setting that a running path ignores says so in its guide (a quality-check mode
only marks the main checks of an error type, read by the quality lint alone; the tour rewrite attempts count only on the older scoring path). The technical
`description` stays as the page's collapsed "Engineering note". The same shape applies to the CRM registry (`crm.setting_definition`,
relayed to the CRM tab), the TripPlanner keys and Booking's policy parameters (`app/pricing/policy_guides.py`). The
shrink-only baseline `tests/unit/snapshots/aa1096_settings_guide_baseline.json` is empty; adding a key to it needs the
tech lead's OK in review.

**Env overrides live one release (AA-1096 l5, CIS 424).** A definition's `env_override` is an emergency switch for one
release only (tech lead S279). CIS 424 retired the 25 the registry had (erasure retention, 21 quality modes, the blog
seed ratio, the blog extend switch; list in `tests/unit/snapshots/aa1096_retired_env_overrides.json`): no definition
names an env var, and the Dev erasure drill sets `cis.erasure.tenant_retention_days` to 0 on the page. The mechanism
stays; `tests/unit/test_aa1096_settings_env_lint.py` refuses a retired override coming back, any runtime literal of a
retired env name, and any new override without its window in `tests/unit/snapshots/aa1096_env_override_window.json`
(at most 14 days; red once `remove_by` passes, until a `settings-edit` migration retires it). A migration changes
fields of existing keys through a `settings-edit` block (`scripts/settings/gen_compiled_defaults.py`).

**Change rules (AA-1096 S280 UX fix round).** A change may not be dated in the past (`settings_registry.effective_start`:
422 `effective_in_past`, five minutes of grace count as now; the CRM relay applies it before relaying), and the newest
decision wins: an approval in force supersedes every other approved row of its scope, scheduled ones included (the
audit row lists `superseded_versions` / `cancelled_scheduled_versions`); a future-dated approval supersedes the
schedules dated at or after it. A tenant value is refused while the key's env override is in force (409
`env_override_active`, the CRM rule). The list carries `approved_version` (the base version of a platform change) and
`scheduled`; "Last change" is an approved change only. The CRM tab's list adds `can_write` (the CRM's `/console`
answer: only `crm-platform-admin` writes) and names the CRM staff actor (`platform:<sub>`) from `shared.admin_users`
by Cognito sub. The relay maps the CRM's agency rows (crm.ops.v1 1.14 `agency_overrides` / `agency_pending`) to
`tenant_overrides` / `proposals` with `agency_name`, so the CRM tab lists each agency's value and open change and decides
it with the agency id, and writes a CRM approval's
`superseded_versions` / `cancelled_scheduled_versions` (1.15, the same rule as CIS) into the CIS audit row. Since S282
(AA-963 / AA-970) the change form of a CRM `platform+tenant` key also authors one agency's value: "Applies to" Platform
or One agency, the agency picked from the CRM agency list (the `/admin/platform/crm/agencies` relay the Agencies console
reads, never the CIS tenant picker), the value and "In force now" of that agency, `tenant_id` in the body (the CRM's
`POST /settings/{key}/changes`). An agency feature switch off by default (an on/off agency key whose default is off:
`crm.search.enabled`, `crm.release.preview`) opens on One agency and warns when Platform is picked.
