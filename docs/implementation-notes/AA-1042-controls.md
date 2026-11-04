# AA-1042 — Route control center, slice "Admin controls" (S268, lane aa1042-controls)

Tech lead request (S267): every routing setting of the AI gateway is editable in the admin with no deploy, and every
change is audited, previewed and revertible. This slice: the provider order per model, the route order mode as a DB
setting, two presets, a drag-and-drop chain editor per stage, a server-side preview and validation, route config
versions with one-click revert, the route config version on every CIS ledger row, the retired gpt-4.1 deployment off,
and the Guide tab + root docs. Route trace, fallback alerts and the quality guard are the parallel lane
`aa1042-trace`, not here.

Two PRs, MIG-4 order (370 is contract): CIS migration 370 (migration-only), then the code PR. Plus a root PR (the
DM-7.1 retention rows of the two new tables, merged before the Infra copy, and `docs/architecture/ai-gateway.md`).

## Decisions

- **One versioned config = order mode + every stage's chain (primary + fallbacks) + every model's deployment
  priorities.** Shadow, kill switch, data scopes and caps keep their own editors and audit rows; they are not in the
  snapshot, so a revert never flips a stage on or off or widens its data scope.
- **Every route change is a version, whatever path made it.** The new route control writes one; the existing PATCH
  `/admin/llm-config/{stage}` and `/route` (Routing tab) also write one, in the same transaction as their UPDATE
  (`shared/llm_client/route_versions.py`, called from `role_config.set_stage_config` / `set_stage_route`). A change
  the app did not make (a later migration) is recorded as an `external` version when the next admin change starts,
  so a diff is never attributed to the wrong person.
- **Version numbers are serialised by the settings row lock** (`SELECT … FOR UPDATE`), current + 1; no sequence. Saves
  carry `expected_version` (409 when someone saved in between); the check is against the version before the external
  catch-up, because the preview the admin looked at already read the live rows.
- **Route config version on the ledger: a BEFORE INSERT trigger, not the INSERT statement.** The ledger INSERT is
  pinned to the shared core (`shared/aa_llm/ledger.py`, synced to TripPlanner and the CRM by `llm-core-sync.sh`) and
  relabelled by the AA-729 eval harness; adding a 36th column there touches three repos. The trigger stamps CIS rows
  (app cis / NULL) with `llm_route_settings.current_version`; TripPlanner rows stay NULL. The function is
  SECURITY INVOKER (migration 245's X1 guard / MIG-6 refuse a SECURITY DEFINER routine in `shared`, first draft was
  DEFINER and the 245 replay tests caught it); it checks `has_table_privilege` first, so a role that may not read the
  settings writes NULL instead of losing the ledger row (only cis_app writes CIS rows; it has SELECT). The gateway's
  caches reload within 20 s of a new version (`route_settings` sees the new number and invalidates the stage and
  deployment caches of its process), so a row written in those seconds can name the new version for a call routed
  by the old one; documented on the column.
- **Every route resolution watches the version (review fix).** First draft: other processes noticed a new version
  only through `order_mode_sync()`, reached only from `client.route_aws_first_enabled()` for chains of 2+ models, so
  a provider-order change could wait the deployment cache's 5 minutes on a single-model stage. Now
  `route_settings.watch_version()` runs first in `role_config.get_stage_config_sync` and `deployments._ensure_loaded`
  (the two caches every route read goes through, whatever the stage or order mode). Inside its 20 s window it is a
  clock read; after it, one small SELECT per process per 20 s, not per call. The watch runs BEFORE the deployment
  cache reloads, so a process never routes with rows older than the version it recorded. Never raises.
- **Order mode: DB wins, env var is the bootstrap default.** `route_aws_first_enabled()` reads
  `shared.llm_route_settings.order_mode` (20 s cache); with no readable row (before 370, DB down, unit tests) the env
  var `LLM_ROUTE_AWS_FIRST` and then model first, exactly as before.
- **Provider order per model = deployment `priority`.** A save sets 10, 20, 30 … in the order the admin gave; a revert
  restores the snapshot's exact numbers. cis_app gets UPDATE on (priority, updated_at, updated_by) only.
- **Presets touch provider order (and order mode), never chains:**
  - `prefer_anthropic_api` — "Prefer Anthropic API for Claude (sponsored)": every Anthropic-family model puts its
    Anthropic API deployments first, then the rest in their current order; order mode model first (with AWS first the
    API would only be tried after every AWS model of the route, which defeats the preset).
  - `prefer_aws` — every model puts its AWS (aws_only) deployments first, then the vendor APIs; order mode unchanged.
  A preset needs eval evidence only when it moves a writer / judge stage to another model@connection (below).
- **Validation (server, on preview and on every save / revert):** the per-stage rules of the Routing tab PATCH
  (selectable models, data scopes, vendor-only rungs last unless a twin, no duplicates) plus the chain rules of
  `llm_route_chains.violations` that the tech lead named: judge family ≠ writer family, check pairs on different
  makers, at most 6 attempts; and every active stage keeps a model that would be called now (the $0 resolver). A rule
  broken ALREADY by the live config (e.g. `s1_atom_writer` / `tp_extract` have no live model until 336) is a warning,
  not an error: a save is refused only for what it breaks itself.
- **Preview** reuses `llm_route_chains.resolve_stage` under two context-variable overrides (proposed priorities in
  `deployments`, proposed order mode in `route_settings`): per request, never seen by a concurrent gateway call.
- **Evidence (AA-1026 D7) on writer / judge stages whose chain OR first serving model@connection changes**, once per
  save (eval run or break-glass reason), the break-glass bell per such stage (its payload names the chain and the
  first model@connection before / after). Review fix: the first draft counted chain changes only, so a provider-order
  change or a preset that moved a writer from acc3 to the Anthropic API passed without evidence. The serving
  comparison uses the preview resolver (`resolve_stage`, $0) before vs after for every active stage; a stage whose
  first serving deployment is unchanged needs nothing (e.g. reordering a fallback-only model). A revert or the order
  mode count the same way. The preview marks each stage `serving_changed`; the UI shows "before: …" whenever the
  first serving model@connection changes, not only on chain edits.
- **Access:** reads `spend.read` (like every AI gateway read), preview / save / preset / revert `ai.admin`. The page
  asks `GET /admin/ai-gateway/access` (`route_control`) and shows a read-only view otherwise.
- **UI:** its own tab "Route control" (`RouteControlTab.tsx` + `routeControl.messages.ts` + `lib/routeControl.ts`);
  the shared page file gets one tab entry. Drag and drop is native HTML5 with up / down buttons beside each item (touch
  and keyboard).

## Changed

- **CIS 370 (contract, migration-only PR):** `shared.llm_route_settings` (one row, seeded `model_first`),
  `shared.llm_route_config_version` (baseline version 1 = the routes and priorities after 370), `llm_call_log.
  route_config_version` + BEFORE INSERT trigger, cis_app grants (settings SELECT / UPDATE, versions SELECT / INSERT,
  deployment UPDATE on priority / updated_at / updated_by), the `gpt-4.1` deployment off. Tests:
  `tests/integration/test_aa1042_migration_370.py`; `test_aa1002_migration_304.py` (370 in LATER, gpt-4.1 excluded
  from the pre-304 comparison) and `test_aa1026_migration_367.py` (priority now granted) adjusted.
- **Gateway:** `route_settings.py` (order mode DB-first, env fallback, version watch, preview override),
  `deployments.priority_override` (preview only), `client.route_aws_first_enabled` reads route_settings,
  `route_versions.py` (snapshot / diff / begin / finish), `role_config.set_stage_config` / `set_stage_route` write a
  version in their transaction (never fail the PATCH: savepoints).
- **API:** `api/routers/admin_llm_route_control.py` + `api/services/llm_route_control.py`; POLICY (reads spend.read,
  writes ai.admin), `PAGE_WRITES["route_control"]`; readiness requires 370 (`REQUIRED_CIS_ABOVE_GAP`).
- **Writes apply only the proposal's diff** against the config the preview read, inside one transaction under the
  settings lock with `expected_version` (required): an untouched stage keeps whatever it has now.
- **Admin:** tab "Route control" (`?tab=controls`): `RouteControlTab.tsx`, `routeControl.messages.ts`,
  `lib/routeControl.ts` (+ `.test.mjs`); `AiGateway.tsx` one tab entry; Guide section "Route control" (EN + VI) and
  three glossary terms; `lib/guides.test.mjs` reads the new files.
- **Guard tests updated:** route policy and role matrix snapshots, openapi.json, `test_aa1002_no_behaviour_change`
  (two new gateway modules), `test_aa804_ai_gateway_api` and `test_aa882_gd3_router_samples` (new routes),
  `test_aa884_health` / `test_aa1035_s267_fix_pass` (370 above the gap), the fake-conn store tests of AA-686 /
  AA-1024 (version hook stubbed).

- **Review fixes (S268):** version watch on every route read (above); evidence on serving changes (above);
  integration test `test_revert_restores_the_exact_chains_and_provider_order` (a chain save + a provider-order save,
  revert to the version right before the first save, every stage's route and every deployment priority equal to the
  rows before); the integration fixture now also restores `shadow_model_id` / `shadow_sample_pct` (the Routing-tab
  PATCH test set them on `a3_place_days`). Unit: the version watch reloads a "fresh" deployment cache 20 s after
  another process saves; serving-change evidence for a provider order and a preset; `test_aa1002_s2_vendor_route`'s
  concurrency double now answers the settings read (one settings read across 8 threads).

## Tradeoffs

- Trigger vs explicit column in the gateway INSERT: explicit is the exact version the process used, but it changes the
  shared ledger core in three repos; the trigger is exact except for the cache window after a change.
- One version for the whole config instead of one per stage: a revert restores the whole config at that point
  (including later unrelated changes being undone). The diff shown before a revert lists everything it changes.

- Evidence on serving changes vs chain changes only: stricter (a sponsored-API preset on Dev now asks for an eval run
  or a break-glass reason when it moves a writer / judge), but a provider switch is a model-quality and data-path
  change for that stage, which is what D7 guards. A reorder that does not move the first serving deployment stays
  evidence-free.
- Version watch in the two caches vs one call in `LLMClient._generate`: the caches also serve embed, pricing and the
  admin reads; hooking them covers every path without listing call sites.

## Should know

- Not in this slice (follow-ups): the Guide tab rendering the root `docs/architecture/ai-gateway.md` from the docs
  catalog with live tables, and the CI drift guard of names in that doc (Linear comment "docs stay in sync"); a
  `ai-gateway.vi.md` twin; a retention window for the version table before prod. Route trace, fallback alerts and the
  quality guard are lane aa1042-trace.
- Pre-existing, order-dependent integration failure on `main` (not from this change): running
  `test_aa1002_migration_304.py` before `test_aa1002_deployment_wire_drift.py` / `test_aa1026_migration_366.py` in one
  process fails them (the 304 replay drops the anthropic-direct deployments of 306, which LATER does not re-seed). CI's
  sorted shard order runs wire drift first.

- 370 is contract: Infra Migration Apply applies it on Dev when every job table is idle; the code PR merges after.
- 370 grants (review fix): on Dev DB Migrate creates the tables and the trigger function as cis_owner, whose default
  privileges (245) hand cis_app SELECT/INSERT/UPDATE/DELETE on new tables and EXECUTE on new functions. The file
  REVOKEs ALL from PUBLIC, cis_app, aa_app_user and catalog_owner first (345/353 pattern), revokes EXECUTE on
  `shared.llm_call_log_stamp_route_version()` from cis_app (X1: trigger functions get no EXECUTE grant), then grants
  narrowly; an after-guard RAISEs if cis_app still holds UPDATE/DELETE/TRUNCATE on the versions or INSERT/DELETE on
  the settings. The CI superuser replay has no cis_owner default ACL, so `test_aa1042_migration_370` simulates it
  (grants everything, re-runs the file, checks it is narrowed) and runs the version write path as cis_app.
- The code tolerates 370 missing: the tab shows "not set up", the gateway uses the env var, the Routing tab PATCHes
  still work (no version row).
- `tests/integration/test_aa1002_migration_304.py` and `test_aa1026_migration_367.py` were adjusted: 370 grants
  priority UPDATE and switches gpt-4.1 off on the deployment table they rebuild.
- Merged with the Trace lane (CIS #1301, migration 371) by a merge commit, not a rebase (this branch was already
  pushed). Both routers, capability entries, page actions (`route_control`, `route_policy`, `route_test`) and tabs
  (Route control, Trace) are kept. The two lanes touch different gateway seams: this lane's
  `route_settings.watch_version()` (route resolution) and the 370 trigger stamping `route_config_version`, the Trace
  lane's `route_trace` / `position` in `quality_signal` and `call_log.ROUTE_KEYS` (ledger rows). Route positions in the
  trace are positions of the route the gateway actually tried, so they follow the order mode set here.
  `docs/api/openapi.json` and the two route-policy snapshots were regenerated (fastapi 0.161.1 / pydantic 2.14.1).
