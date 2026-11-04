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
  A preset needs no eval evidence (no stage changes model).
- **Validation (server, on preview and on every save / revert):** the per-stage rules of the Routing tab PATCH
  (selectable models, data scopes, vendor-only rungs last unless a twin, no duplicates) plus the chain rules of
  `llm_route_chains.violations` that the tech lead named: judge family ≠ writer family, check pairs on different
  makers, at most 6 attempts; and every active stage keeps a model that would be called now (the $0 resolver). A rule
  broken ALREADY by the live config (e.g. `s1_atom_writer` / `tp_extract` have no live model until 336) is a warning,
  not an error: a save is refused only for what it breaks itself.
- **Preview** reuses `llm_route_chains.resolve_stage` under two context-variable overrides (proposed priorities in
  `deployments`, proposed order mode in `route_settings`): per request, never seen by a concurrent gateway call.
- **Evidence (AA-1026 D7) on chain changes of writer / judge stages**, once per save (eval run or break-glass reason),
  the break-glass bell per changed stage. A revert that changes such a chain asks for it too (the preview says so).
- **Access:** reads `spend.read` (like every AI gateway read), preview / save / preset / revert `ai.admin`. The page
  asks `GET /admin/ai-gateway/access` (`route_control`) and shows a read-only view otherwise.
- **UI:** its own tab "Route control" (`RouteControlTab.tsx` + `routeControl.messages.ts` + `lib/routeControl.ts`);
  the shared page file gets one tab entry. Drag and drop is native HTML5 with up / down buttons beside each item (touch
  and keyboard).

## Changed

- (filled in as the slice lands)

## Tradeoffs

- Trigger vs explicit column in the gateway INSERT: explicit is the exact version the process used, but it changes the
  shared ledger core in three repos; the trigger is exact except for the cache window after a change.
- One version for the whole config instead of one per stage: a revert restores the whole config at that point
  (including later unrelated changes being undone). The diff shown before a revert lists everything it changes.

## Should know

- 370 is contract: Infra Migration Apply applies it on Dev when every job table is idle; the code PR merges after.
- The code tolerates 370 missing: the tab shows "not set up", the gateway uses the env var, the Routing tab PATCHes
  still work (no version row).
- `tests/integration/test_aa1002_migration_304.py` and `test_aa1026_migration_367.py` were adjusted: 370 grants
  priority UPDATE and switches gpt-4.1 off on the deployment table they rebuild.
