# AA-1039 — R6 rest: gap list, routing by data class (CIS 373), Writing Lab Releases and Metrics (lane aa1039-r6-rest, S268)

Plan: root ADR 0019 d12 / d13 / d20 / d21, clarifications TL-12 / TL-13 / TL-20 / TL-21, root audit
`docs/audits/2026-11-03-S265-ecosystem-restructure/README.md` §5.5 and §6. Earlier slices: `AA-1039-r6-admin-ia.md`,
`AA-1039-r6-gate-fanout.md`, `AA-1039-r6-writing-lab.md`.

## Gap list (Scope and Done-when vs CIS main on 04/11, TripPlanner for the hide-on-retire part)

| Item | On main before this lane | This lane |
|---|---|---|
| Nav groups (seven) | Done (slice 1) | — |
| Every old route redirects | Done: `ADMIN_ROUTE_MOVES` + `PRE_R6_ADMIN_ROUTES` (node test), `aa1039-admin-ia-redirects.spec.ts` (smoke: every move is a 307 with the query kept) | No new move, so nothing to add |
| AI & spend: providers, routing, budgets, calls together | Done (slice 3: AI gateway + one Spend page) | — |
| AI & spend: routing by `pii_class` (PII → us) | Card only, empty state; no column, no API field, no gateway rule | **Built**: CIS 373 + gateway + admin (below) |
| Anthropic line | The `anthropic-direct` connection row exists (CIS 304/306), Connections shows its access state; `unconfigured` reads "no key set up"; the Providers overview marks it as a warm path (AA-1024/1026) | Nothing to add: no hard-coded key, the row is the slot. Live check by the orchestrator |
| Writing Lab per stage: samples, labels, experiments, prompt versions, eval gate | Done (slice 3) | — |
| Writing Lab: Releases, Metrics (IA §6.4 tabs) | In Writing memory only | **Built**: two Lab tabs over the existing reads |
| Writing Lab: Hypotheses, Regression set (IA §6.4) | Rule counts on the gate tab; golden sets on Samples | Not split out (Samples already is the regression set; candidates hold tenant text and stay in Writing memory) |
| Writing Lab: `ai_ops` logged read of tenant text | — | R2 (AA-1035 lane), not here |
| Writing Lab stages `place_card`, `tp_compose` | Not stages: every learning table CHECKs the writer to a1 / t2 / t9_blog / t9_social | Freeze (TL-11) per stage; not here |
| AA-subject gate | Done (slice 2), verified live in a rolled-back transaction | — |
| Fan-out: TripPlanner hides the tour | Done (AA-987 purge + `catalog.v_tour`, tour page 410) | — |
| Fan-out: cancels pending publishes, flags posts, WordPress draft after notice | Done (slice 2) | Live retire → restore check needs a live WordPress test site (orchestrator / next session) |
| Fan-out: tenant bell + e-mail | E-mail digest done (slice 2); the bell rows `tour.discontinued` / `tour.reinstated` for tenants exist since AA-986 L3 (`GET /v1/notifications`) | — (live check with the WP site) |
| Fan-out: CRM agent task, portal `needs_change` view | Follow-ups of slice 2 (AA-Partner) | Not here |
| IA: Tenant 360 as a route `/admin/tenants/[id]` | A drawer of the Directory (`?tenant=<id>`) | **Built** (fix pass): route page, panel moved to `_components/TenantDetail.tsx`, every link builder points at the route, old `?tenant=` links replaced in the browser |
| IA: Catalogue settings split from the tenant tabs | Settings had Pipeline Gates, Brand Identity, SEO Config, Tenant Info, UI Kit | **Built** (fix pass): "Catalogue settings" = Pipeline gates, AA house style, SEO config; Tenant Info → aa_internal Tenant 360 › Settings; UI kit → `/admin/platform/ui-kit`; both old tabs 307 |
| IA: SEO Spend tab folded into Spend | SEO had a Spend tab (same `dfs_call_log` rollup as Spend › DataForSEO) | **Built** (fix pass): tab and API key removed, link to Spend › DataForSEO for spend roles, `?tab=spend` 307 |
| IA: Places & photos takes the Cards tab of Content quality | Cards is `?tab=cards` of `/admin/tenant-qa/quality` (TENANT_QA roles) | **Built** (option A, tech lead 05/11): `/admin/places/cards`, own page rule = TENANT_QA, `?tab=cards` 307 |
| Screenshots 1440/390 light/dark | Slices 1–3 | Orchestrator after deploy (pages in the PR live steps) |

## Decisions

- **`pii_class` extends the existing stage data policy instead of adding a parallel one.** The gateway already had
  `allowed_scopes` per stage (CIS 311: `aws_only` = our Bedrock accounts, `vendor_direct` = a vendor API) and
  `data_scope` per connection. A `pii` stage is held to `allowed_scopes = {aws_only}` by a CHECK (not NULL: NULL is
  "no policy", which lets a vendor API be a model's first deployment), so "PII stays on our accounts" is data the
  database enforces; the gateway adds the geography of ADR 0019 d12 ("PII routes to us"): only a `us.` profile or an
  in-region id on a `us-*` connection, never `global.` (`data_scope.deployment_geo` / `pii_allows`).
- **Every stage is `content` today.** No CIS stage receives personal data: A-series and place stages write AA
  catalogue text, T-series write tenant brand content, `tp_compose` narrates catalogue tours, the embed stages embed
  catalogue text and search words. The CRM stages that read traveller messages run in the CRM's own gateway after its
  PII scrub (root ADR 0008a §5). So the change is behaviour-neutral on Dev; the CHECK protects a future pii stage from
  a bulk "open every writer to vendor APIs" migration like 366.
- **Fail closed for a pii stage on paths the registry does not describe**: the hard-coded legacy chain and the
  catalog's Converse / OpenAI fallback can call a `global.` profile or a vendor API, so a pii stage gets a
  RuntimeError there (the route moves to its next model) and `embed` returns no target.
- **One role_config read per call**: the class is bound with the scopes and the role (`client._STAGE_SCOPES`, now
  `(stage, scopes, role, pii_class)`) by `_generate`, the shadow, an AWS-first pass and the route preview;
  `stage_pii_class` reads the binding first (`test_aa900_eval_label_keeps_route` counts the reads).
- **An unknown text value reads as `pii`** (fail closed), a non-text value (a test double) or a missing column as
  `content`. `GET /admin/llm-config` sends `null` before CIS 373 so the Routing card keeps its empty state until the
  column exists (the gateway itself reads a missing column as content).
- **Admin**: the class is migration-only (no `cis_app` grant on the column). The model and route PATCHes and Route
  control's `chain_problems` refuse a model with no US deployment for a pii stage (422 / problem `pii_class`); the
  route invariant (`invariant.check_stage`) and the route preview (`llm_route_chains.resolve_stage`) count only US
  deployments for it.
- **Writing Lab Releases and Metrics** reuse `GET /admin/writing-memory/releases?writer=` and `GET .../metrics` (no
  new backend). Releases shows ids, status, class, training, rules count, evidence verdicts, check-again flag and
  dates (no notes, no text); Metrics shows the writer's overall KPIs per stored week, the replay rate, clean pairs,
  soft stops and the weekly job's actions. Each tab has its own request (the stage read is not widened). Actions stay
  in Writing memory; its page now opens `?tab=` (Suspense boundary for `useSearchParams`).
- **Review pass after the rebase on main (S268)**:
  - The route preview (`llm_route_chains.resolve_stage`) keeps both rules: the data class goes into
    `deployment_plan` (the same call the gateway makes, so a pii stage's planned deployments are the runtime's), then
    AA-1042's refusal evidence picks the deployment that would serve. A legacy key skipped for a pii stage is listed in
    `skipped` like any other skip before `first`.
  - A pinned `model@connection` (`vendor_route.pinned_deployment`, probes and connection tests) keeps the request
    stage's `allowed_scopes` and `pii_class` like `deployment_plan`: a stage that may not use the pinned connection
    gets a RuntimeError before any vendor call. A pin with no stage (the probes) is unchanged.
  - A failed role_config read with no cached row (cold cache, DB unreachable) returns SAFE_DEFAULTS' route with
    `pii_class = pii` (`role_config._read_failed_default`, not cached), and `client.stage_pii_class` answers `pii` if
    its own read raises. A read that succeeds with no row (an adhoc_* stage) or a DB before 373 stays `content`.
    Consequence: while the DB is unreachable on a cold process, every stage runs only on US deployments of our AWS
    accounts, and the legacy chain and the no-registry path refuse. (Corrected in review 2: an earlier sentence said
    those paths "would need the DB for the registry anyway"; they do not: the hard-coded legacy chain and the
    catalog's Converse fallback run from constants / the cached catalog, which is exactly why a pii stage must be
    refused there explicitly.) Review 2 narrowed the fail-closed case to a stage never read in this process (below).
    Four unit tests that left the read to an unreachable DB now state "read OK, no row" explicitly.
  - `data_scope.deployment_geo` answers `us` only for an explicit `us.` profile, or for an in-region Bedrock id
    `<provider>.<model>` of a known provider (`_BEDROCK_PROVIDERS`) on a `us-*` connection. A full ARN, an unknown
    prefix (`zz.anthropic.…`) and an id that is not a Bedrock model id are `unknown` (a pii stage refuses them). A new
    Bedrock provider must be added to that list before a pii stage can use its in-region ids.

- **The decision path keeps the data class too (review S268).** `decide()` sends a stage's state to TypeSafe (Jev,
  api.typesafe.ai), a vendor outside our AWS accounts, and its question stages share names with `llm_role_config`.
  A `pii` stage now answers every question `skipped` with `pii stage: no vendor decision` before the tenant guard,
  the verdict cache and the call (the stage keeps its own rule, as for any skip). The pii stages are one query
  (`SELECT stage FROM shared.llm_role_config WHERE pii_class = 'pii'`) read with the questions on the connection
  `_decide` already holds, same 20 s TTL, rather than `role_config.get_stage_config` per stage: that opens its own
  connection on a miss and warns for every decide stage with no `llm_role_config` row (`a1_seo`, `a0_ingest` ...).
  Before 373 (column missing) every stage is content; any other read failure fails closed (all verdicts `error`, no
  TypeSafe call, not cached). A decide stage with no `llm_role_config` row is content, as the gateway reads it.

- **Review 2 (S268), all verified against the code before fixing:**
  1. `decide()` / `decide_sync` (same `_decide`): already skipped a stated pii stage (commit before this pass); the
     failed class read answered `error` verdicts. Now a class read that fails before any good read in this process
     marks every stage pii (`_pii_stages is None`): every question `skipped` with `pii stage: no vendor decision`,
     no TypeSafe call, questions kept but not timestamped (the next call reads again). A failed refresh after a good
     read keeps the last set (the class changes only by migration). The log event `decide_pii_class_unread` says
     "stage class unknown (role_config read failed)".
  2. `role_config._read_failed_default` gives a pii class (stated or unread) `allowed_scopes = (aws_only,)` (373's
     CHECK invariant), and `_call_openai` (`OpenAIPlatformBlocked`) and `_legacy_last_resort` (no GPT-4.1 call) check
     `stage_pii_class` themselves, so a pii stage never depends on its scopes alone there.
  3. `role_config._pii_known` (stage -> class of the last successful read, a row or "no row" = content) survives
     `invalidate()` and the route-version cache drop; a failed read uses it and only a stage never read here fails
     closed, as `data_scope.PII_UNREAD`: a `str` subclass equal to `"pii"` (every `== PII_PERSONAL` rule holds, and
     `normalize_pii_class` returns it as is), whose type marks "not read". `data_scope.pii_reason()` turns it into
     "stage class unknown (role_config read failed): US AWS deployments only" in the plan skip log, the pin error,
     `_pii_refusal`, the OpenAI / last-resort refusals, the s1_batch refusal and the role_config warning. Chosen over a
     new StageConfig flag: no extra plumbing through the 4-tuple binding (`_STAGE_SCOPES`) and every call site.
  4. `pii_allows(pii_class, scope, wire, region, kind)`: a pii stage also needs `kind in AWS_KINDS` (`bedrock`,
     `bedrock_guardrails`, `mock`; a missing kind is refused). Passed from `deployment_plan` / `pinned_deployment`
     (`connection.kind`), `invariant.check_stage` (`CONNECTIONS_SQL` already selects `kind`) and
     `admin_llm_ops._pii_usable_models` (`c.kind`). Required argument, so a new caller cannot forget it.
  5. Route preview: a legacy key on a pii stage is planned like `_legacy_call` (`LLMClient._alias_steps`, now a
     staticmethod, with the class; `LLM_LEGACY_ALIASES` off or an alias the registry cannot describe = skip; no
     usable target = skip; refusal evidence per `model@connection`; `deployments` lists `model@connection` for that
     step since an alias spans models). `chain_problems(..., pii_usable=None)`; `preview_world` computes
     `_pii_usable_models` once in `asyncio.to_thread`, only when a changed stage is pii.
  6. `s1_batch.submit_s1_batch` refuses (`BatchUnavailable`, before reading any prompt) when `s1_generate` is pii or
     unread: Batch runs the legacy Haiku profile, outside the deployment plan; the endpoint answers 400 and the tours
     run on demand on the stage route.
- **Place cards, option A (tech lead, 05/11/2026)**: `/admin/places/cards`, page rule TENANT_QA placed before the
  admin-only `/admin/places` rule; nav "Place cards" in AA Catalogue between Places and Photos (longest-prefix active
  state, so Places is not lit on it); `/admin/tenant-qa/quality?tab=cards` and `/admin/content-quality?tab=cards` 307
  there in one hop (query kept: `tab=cards` rides along, ignored by the page). The Cards UI, its facts drawer
  (`git mv`), its types and its EN/VI messages moved to `app/admin/places/cards/` (Content quality keeps no card key);
  the page has its own URL state (status, search, page). Backend and BFF rules unchanged.

## Changed

- Migration-only PR: `api/migrations/373_llm_stage_pii_class.sql` (contract), `tests/integration/test_aa1039_migration_373.py`.
- Decision path: `shared/llm_client/decide.py` (`_load_pii_stages`, `stage_is_pii`, `PII_STAGE_SKIP_ERROR`, the skip
  in `_decide`); tests `tests/unit/test_aa660_decide.py` (5 tests; the `_conn` double answers the pii query, as do the
  doubles in `test_aa741_g1_tenant_binding.py` and `test_aa982_a3_place_paa_filter.py`) and
  `test_aa1039_migration_373.py::test_the_decision_path_reads_the_pii_stages` (real table).
- Code PR: `shared/llm_client/data_scope.py` (PII_CLASSES, `normalize_pii_class`, `deployment_geo`, `pii_allows`),
  `role_config.py` (`StageConfig.pii_class`, optional column read with the 373 fallback; admin list sends it),
  `vendor_route.deployment_plan(pii_class=)`, `client.py` (`stage_pii_class`, the plan / alias / legacy paths),
  `embed.py`, `invariant.py` (ROUTES_V4_SQL; wire id and region read), `api/routers/admin_llm_ops.py`
  (`_pii_usable_models`, `_check_pii`, `pii_class` in GET), `api/services/llm_route_chains.py`,
  `api/services/llm_route_control.py`; frontend card copy (`models.messages.ts`), `lib/aiGateway.ts`,
  `llmShared.ts`; Writing Lab (`lib/writingLab.ts`, `app/admin/lab/WritingLabStage.tsx`, `messages.ts`),
  Writing memory `?tab=`. Tests: `tests/unit/test_aa1039_pii_class.py` (new), `lib/writingLab.test.mjs`,
  `lib/aiGatewayDataClass.test.mjs`, stub signatures in `test_aa1026_embedding_space.py`, the invariant SQL in
  `test_aa642_a1_stage_switch.py`. `CONTEXT.md`.

- **Fix pass (S268): the three IA moves that need no decision are built.** The earlier reasons did not hold:
  AA-1035 slice 2 is on main, Spend › DataForSEO already had the per-endpoint table (`cost-dfs-by-endpoint`, the
  same `_DFS_TREE_SQL` rows SEO showed), and the Tenant 360 route is in this issue's own scope (AA-1028, named
  before, is Done and never covered the route).
- **Tenant 360 route reads the scoped tenant list** (`GET /admin/tenants?include_inactive=true`, filtered to the
  caller's book by the backend) and picks its row: no new backend read, and a tenant outside the book shows "not
  found" instead of a 403 page. Status / offboard / restore stay on the Directory row. `?tab=` alone picks the tab
  (the tenant is the route's). Old `?tenant=<id>[&tab=]` links (bookmarks) are replaced client-side by
  `legacyTenantLinkTarget` (a Next.js redirect through `ADMIN_ROUTE_MOVES` cannot move a query value into the path).
- **Catalogue settings keeps `/admin/settings`** (slice 1 rule: AA Catalogue pages keep their URLs; Upload links
  `?tab=brand`). Tenant Info goes to the aa_internal Tenant 360 Settings tab (plan, limits); the UI kit becomes an
  admin-only Platform page (all Platform pages are admin only; a content role has no use for the design reference,
  and the nav test "a content worker sees the AA Catalogue group only" stays true).
- **SEO's Spend fold removes `spend` from `GET /admin/seo-intelligence`** (two dfs queries less): content_lead reads
  that route and has no `spend.read` (root audit S265 §4.1), so the page shows the Spend link only to a role that may
  open Spend. `days` stays an accepted parameter so the OpenAPI contract is unchanged.

## Decision: Cards (option A chosen by the tech lead, 05/11/2026; built, see Decisions)

The IA puts Content quality's **Cards** tab (place cards: verify / reject, merge duplicates, facts drawer;
`/api/admin/content-quality/place-cards*`, TENANT_QA = admin, content_lead, tenant_qa) under AA Catalogue › Places
& photos. `/admin/places` is admin only. Choice:

- **A (proposed): a route `/admin/places/cards` with its own page rule = TENANT_QA**, placed before the
  `/admin/places` rule; backend routes unchanged; `/admin/tenant-qa/quality?tab=cards` 307 there. Nobody gains or
  loses access; one PR.
- **B: a tab of `/admin/places`** (admin only): content_lead and tenant_qa lose the card review. Matches §4.1 only
  partly (content_lead has quality.write on the catalogue).

Proposed: A. The tech lead chose A on 05/11/2026; built in this lane.

## Tradeoffs

- **Geography by wire-id prefix**, not a new deployment column: the Bedrock cross-region prefix is the geography
  (`global.` / `us.` / `eu.` …), and an in-region `<provider>.<model>` id runs in the connection's region (a full ARN
  or an unknown prefix is not parsed: unknown, fail closed).
- **Fail closed on a failed stage read** costs availability during a cold-cache DB outage (US-only AWS deployments,
  no legacy chain) in exchange for never sending a pii stage to `global.` or a vendor API on a guess. A vendor API is never
  US-pinned here (the Anthropic `inference_geo` request default exists, but a pii stage is aws_only anyway).
- **Access changes of the fix pass, all toward the IA role table**: the UI kit page is admin only (was every
  content role through Settings); `/admin/settings?tab=tenant` lands on a Tenant 360 a content role may not open (the
  aa_internal row is tenant management); content_lead no longer sees DataForSEO spend on SEO Intelligence (no
  spend.read). Nobody loses a write.
- **Tenant 360 as a page, not a drawer**: the Directory loses the side-by-side view; in exchange the URL is
  shareable and the browser back button works, which the IA asks for.

## Should know

- **Order**: the migration-only PR (373, contract) and the code PR may ship in either order: the code reads a missing
  column as content and the admin sends `null`. The Routing card fills once 373 is applied.
- **Number**: 373 because 372 is held by the AA-1045 lane. The preflight refuses 373 until 372 is on main
  ("not the next one"); if AA-1045 does not land first, renumber to main max + 1: the number is in the file name,
  its header and registration (`'373'`), the integration test (`MIGRATION`, `version = '373'`), the comments
  ("CIS 373", "migration 373") in `data_scope.py`, `role_config.py`, `client.py`, `embed.py`, `invariant.py`,
  `admin_llm_ops.py`, `llm_route_chains.py`, `llm_route_control.py`, `lib/aiGateway.ts`, `llmShared.ts`, the unit
  test, `CONTEXT.md` and these notes (`git grep -n '373'`).
- No new table: no DM-7.1 retention row, no ERD design page change.
- Fix pass files: `frontend/app/admin/tenants/[id]/page.tsx` (new), `tenants/_components/TenantDetail.tsx` and
  `tenantRow.ts` (moved out of the Directory page), `tenants/page.tsx`, `tenants/messages.ts`;
  `app/admin/settings/{page,messages}.ts(x)`; `app/admin/platform/ui-kit/{page.tsx,messages.ts}` (new);
  `app/admin/seo-intelligence/{page,messages}.ts(x)`, `api/routers/admin_seo.py`; `lib/adminIa.ts` (moves,
  `tenant360Href`, `legacyTenantLinkTarget`, `AA_INTERNAL_TENANT_ID`), `app/_kit/{adminNav,nav.messages,CommandPalette}.ts(x)`,
  `app/admin/jobs/JobDrawer.tsx`, `lib/aiGateway.ts` (`tenantCostHref`). Tests: `lib/adminIa.test.mjs` (test 11),
  `lib/aiGatewayC4.test.mjs`, `lib/aiGatewayPolish.test.mjs`, `lib/tenantPlans.test.mjs`,
  `tests/unit/test_aa1039_r6_rest_ia.py`; smoke `ui-smoke.spec.ts` (UI kit, Tenant 360) and `admin-evidence` pages.
- TripPlanner's own gateway (`backend/shared/llm_gateway.py`) does not read `pii_class`; no TripPlanner stage is pii.
  A pii TripPlanner stage would need its catalog view and gateway to apply the same rule first.
- Review 2 / Place cards files: `shared/llm_client/{data_scope,role_config,client,vendor_route,invariant,decide}.py`,
  `api/routers/admin_llm_ops.py`, `api/services/{llm_route_chains,llm_route_control}.py`,
  `services/content_generation/s1_batch.py`; tests `tests/unit/test_aa1039_pii_class.py` (kind, unread class, last
  read kept, OpenAI / last-resort refusals, preview alias plan, one usable-models read, s1_batch),
  `tests/unit/test_aa660_decide.py`. Frontend: `app/admin/places/cards/{page,PlaceCardFactsDrawer,messages,shared}`,
  `app/admin/tenant-qa/quality/{page,messages,drawer.messages,shared}.ts(x)`, `lib/rbac.ts`, `lib/adminIa.ts`,
  `app/_kit/{adminNav.tsx,nav.messages.ts}`, `lib/adminIa.test.mjs` (test 12: the role matrix over every staff role),
  smoke `ui-smoke.spec.ts` (`place-cards`), `admin-evidence/admin-pages.spec.ts`.
