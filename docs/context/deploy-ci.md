# Deploy and CI

> Detail page of [CONTEXT.md](../../CONTEXT.md): workflows, required checks, deploy, migrations on Dev and the guards. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Deploy & CI

- **Backend (ECS)**: shipped via CI; a new task definition rolls out on the `aa-cis-dev-api`
  service. This repo has **branch protection with 5 required CI jobs** — merge PRs with
  `gh pr merge --auto` (auto-merge once checks pass). Agents must not push to `main` directly.
- **Migrations in the app PR (root ADR 0017, AA-993):** CI job **Migration preflight** (`ci.yml`,
  `.github/scripts/migration_preflight.py`, pull_request and merge_group) says whether a new migration is **expand**
  (header `-- migrate: expand`, auto-applied on Dev) or **contract** (manual DB Migrate apply, MIG-4), and refuses a
  number other than `main` max + 1, an edit of a file already copied to AA-CIS-Infra, or a readiness raise to a
  non-expand file. After a push to `main` that touches `api/migrations/`, `migration-sync-wake.yml` rings the
  AA-CIS-Infra **Migration Sync** doorbell (one ci-bot comment on its wake-up issue), so the byte copy opens at once.
  Deploy Dev's **Schema gate** job (`.github/scripts/schema_gate.py`) then waits, before the roll, until `/health/ready`
  `registered_keys` holds every key the commit needs and the last Infra Migration Apply run is green
  (`SCHEMA_GATE_TIMEOUT_S`, default 2400 s).
- **Post-deploy verify (AA-993 V1, ADR 0016 D3)**: `deploy-dev.yml` calls `post-deploy-verify.yml` after `deploy`
  (also dispatchable with `expected_sha`; its own concurrency group, never `deploy-dev-cis`). Script
  `.github/scripts/post_deploy_verify.sh`, HTTP rows in `.github/verify/cis.rows` (a PR that adds an endpoint worth
  guarding adds a row). Red, never a warning: api/worker rollout not COMPLETED, desired 0, an image other than
  `dev-<sha>`, `/health` git_sha/version or `/health/ready` (required schema of the commit) off, an admin row failing
  after 3 tries, no live worker on the SHA serving every registered kind, or `ADMIN_SECRET` missing. The job
  **Deploy Dev result** is red when any job (verify included) did not succeed, skipped included.
- **CI gate parity (AA-879)**: `ci.yml` also runs **Frontend Build** (`tsc --noEmit` + `next build` of `frontend/`,
  no secret needed; a required check). The **Portal Build** / **Portal Lint** jobs went with the frozen `portal/`
  copy at the AA-979 P2b clean-up (Portal Build was also removed from the required checks).
- **Secret scan (AA-981 follow-up)**: `secret-scan.yml`, check **gitleaks** on every PR (lines the PR adds, rules
  and baseline from the base branch; a change to `.gitleaks.toml` / `.gitleaksignore` / either secret scan workflow /
  `.github/CODEOWNERS` / any `.gitattributes`, or a new inline gitleaks allow comment, needs the label `secret-scan-baseline-reviewed`, added after the last push: any later
  push, reopen or label change removes it) and **gitleaks full history** (`secret-scan-history.yml`) nightly / on push to `main`
  (branches, tags and pull request heads; merge commits, renames and type changes included, every file read as
  text; historical findings pinned by commit in `.gitleaksignore`). A red result means the value is public: rotate
  it, even if it was force-pushed away.
  The PR check runs on `pull_request_target`: GitHub always runs the base branch's copy of the workflow and the PR
  head is only read as data, so a PR cannot change the check that judges it. It also scans a squash preview of the PR
  (what the squash merge writes), and it fails when another workflow has a job named `gitleaks`. `.gitleaks.toml`
  carries a copy of the gitleaks v8.30.1 default rules and no path allowlist, so every file is scanned (`.svg`,
  `go.sum`, `node_modules/`, images, lock files); UTF-16 files are transcoded to UTF-8 and scanned too.
  The image scan in **Docker Build Check** is blocking: Trivy fails on any fixable CRITICAL/HIGH finding
  (`ignore-unfixed`); an accepted one goes into `.trivyignore` with a `# reason` line and `exp:YYYY-MM-DD`
  ≤ 90 days ahead (`.github/scripts/check_trivyignore.py`). **Unit Tests** enforces a coverage ratchet
  (`.github/coverage-ratchet.json`, `.github/scripts/coverage_ratchet.py`): below `floor` fails, lowering
  `floor` fails, below the monthly goal (+5 points a month up to `cap`) only warns. **Code-health ratchets**
  (AA-1097; `.github/scripts/code_health.py`, limits and exemptions with reasons in `.github/code-health.json`): a new
  function over cyclomatic complexity 10 (Python: ruff C901, in **Lint**; TypeScript: ESLint `complexity`, in
  **Frontend Lint**) or a new `.py/.ts/.tsx/.js/.mjs` file over 800 lines (tests included; generated files,
  migrations, fixtures exempt) fails; today's offenders sit in `.github/code-health-baseline.json` and may not get
  worse, and the baseline only shrinks: after an improvement run `python3 .github/scripts/code_health.py update` and
  commit it (a moved function keeps its entry under its new path). `scripts/pre-push.sh` runs it too. `deploy-dev.yml` does
  not trigger on `frontend/**` (the image has no frontend file). The URL-pinned wheel of `requirements.txt` (the
  spaCy model) is never downloaded per job: `.github/actions/url-wheels` restores it from the actions cache (key = the
  URL + sha256 pins, warmed on main by Deploy Dev and the nightly run), downloads it with retries only on a miss, checks
  the pinned sha256 every time and hands the job a rewritten requirements file pointing at the local wheel; Docker
  builds read the verified copy from the `.url-wheels/` seed (bind mount) or download it the same way
  (`.github/scripts/url_wheels.py`, S273). Security headers on the deployed host are checked
  by the UI smoke specs (`aa876-security-headers.spec.ts`, `aa885-portal-host.spec.ts`).
- **Public repo, no resource IDs (AA-902)**: the required **Lint** job runs
  `.github/scripts/check_resource_ids.py` over every tracked file and fails on an AWS account ID, an ARN
  with an account, an `<name>-<account>` resource name, a VPC resource ID or an API Gateway host (AWS
  documentation example accounts allowed for test fakes). Exceptions only in
  `.github/resource-id-allowlist.txt`, each with a max count and an expiry date. Code gets the bronze
  bucket from `shared/aws_client/bronze.py` (`BRONZE_BUCKET`, else `aa-cis-bronze-<caller account>`) and
  acc2's own ID from STS; the acc3 satellite ID comes from the task env (`BEDROCK_ACCT_PRIMARY`), read once in
  `shared/llm_client/bedrock_satellite.py`.
- **Test and CI guards from repeated lessons (AA-1024 l22, S271)**: the pre-push hook (`scripts/install-hooks.sh`
  once per clone) refuses a push mid-rebase, and a push whose changed integration tests have no local pass on that
  code (`scripts/pre-push.sh --integration` runs them on a throwaway Postgres and writes the stamp;
  `AA_PREPUSH_SKIP_INTEGRATION=1` skips once); a change to `tests/integration/conftest.py` or a helper it imports
  needs the stamp of the smoke file `tests/integration/test_aa1024_replay_smoke.py`. The integration conftest stops
  the session on a migration that fails outside `tests/integration/_replay_guard.py` `EXPECTED_REPLAY_FAILURES`, on an
  expected file failing with another error than its entry, on an entry that now applies, and on a non-superuser test
  user. Unit lints: no `ORDER BY` a uuid `id` / `created_at` alone in `tests/integration`
  (`test_aa1024_sql_order_stability.py`, frozen allowlist), every migration after 304 naming a table the 304 replay
  drops is in its `LATER` or `NOT_LATER` (`test_aa1024_replay_later_list.py`). Migration preflight also refuses an
  added line naming a migration by an old number after a renumber (`.github/scripts/migration_refs.py`, identical in
  Booking, Partner, TripPlanner), on every PR including one that touches no migration. `nightly-integration.yml` runs
  the integration suite nightly in one job with the modules in a seeded random order (not a PR check); a red night
  opens (or comments on) one GitHub issue labelled `nightly-red`, the next green night closes it.
- **No acc1 in runtime code (AA-998)**: `tests/unit/test_aa998_no_acc1_runtime.py` fails when `acc1`, its account
  ID, its IAM roles, its account env var or its CLI profile appears in api/ (not migrations), services/,
  shared/, worker/, scripts/ or frontend/ — comments included. AA-1095 keeps it: the acc1 Bedrock connection is a
  registry row (migration 406) the code reaches by its row; the switch-over run-script lives in the root repo.
- **Writing-path change control (AA-1052, root ADR 0020 W6 and amendment d12)**: the required **Unit Tests** job runs
  `tests/unit/test_aa1052_change_control.py` (also `python -m services.quality.change_control`). (1) The lock
  `services/quality/change_control.lock.json` holds the sha of each writing-path component (`writing_policy` source,
  `gates` = taxonomy + thresholds, `prompt_registry` = every stage's registered version, `route_table`,
  `final_contract` version + mode, `route_defaults` = `SAFE_DEFAULTS` of the gated stages) with the evidence that let
  it change: `eval:<prompt_eval_runs id>`, `parity:<tests/...py>` (a replay / parity test: output unchanged) or
  `break-glass:<reason>`. A PR that moves a component fails until `--accept <component> --evidence ...` records it;
  the lock's git history is the writing path's change log. (2) A CIS migration above 390 that writes the route config
  of a gated stage (`llm_role_config` / `stage_model_params` rows of an `s1_ t2_ t3_ t8_ t9_ t10_` or
  `writing_memory_eval` stage; any `llm_model_deployment`, `llm_model_catalog`, `llm_route_settings`,
  `llm_route_policy` or `writing_memory_release` write) needs `-- eval: <run uuid>` or `-- break-glass: <reason>` in
  its leading comment block. The lint fails closed: a stage-scoped write is exempt only when every stage it selects
  is a literal outside the gated prefixes (`stage = '<lit>'`, `stage IN (...)`, INSERT ... VALUES, a LIKE pattern
  that cannot match a gated prefix); a write by role (as CIS 331-333 did), by subquery, regex, `<>` / `NOT`, `OR` or
  with no stage selector counts as gated. Prompt registrations need none (they are the bump). (3) The runtime edit gate
  (`services/quality/edit_gate.py`, same prefixes, plus writer / judge stages): an admin edit of a gated stage's model
  or chain (PATCH `/admin/llm-config/{stage}` and `/route`), a route-control save / preset / revert that moves a gated
  stage, a catalog on / off of a model on a gated stage's route (PATCH `/admin/llm-catalog/{key}`) and a change of the
  hold switch (`hold_fallback_writes`) of a gated stage's or the `*` route policy (PUT / DELETE
  `/admin/ai-gateway/route-policy/{stage}`) is refused with 409 while a rerun wave is open (`shared.rerun_wave.status
  = 'open'`, the deploy gate's check) and otherwise needs `eval_run_id` or `break_glass_reason` (422 without), kept in
  the audit row (`details.evidence`; a break glass rings the admin bell). Not gated: the stage kill switch, a provider
  connection on / off, alert-threshold-only policy edits. `shared.stage_model_params` has no runtime write path.
- **Quality registry lint (AA-926)**: the required **Lint** job also runs `python -m services.quality.lint --report`
  (PyYAML only): no gate-name or 7.0 pass-line literal outside `services/quality/` (api/, services/, shared/,
  worker/, scripts/aa729/ and the frontend/portal TS; docstrings, comments, `__all__`, tests and migrations
  exempt; escape `# qlint: allow <reason>`, counted), every gate / chip / Jev key / A1 code mapped to a class
  (incl. the day-oracle, S1-first code-check and degraded codes in `services/quality/`), every Q0 corpus case's
  `class_id` and `detector` resolving through the registry (a gating Q0 case on a `warn`-only class is a warning,
  AA-924 input), the three YAML schemas, every `paths.yaml` glob matching a file, the generated TS fresh. The
  non-required job
  **Quality Paths (report)** prints the quality tiers a change touches and a hash of its diff on
  `services/quality/paths.yaml` paths (AA-919 will enforce `quality/q2` with it).
- **Frontend (Vercel) — retired (AA-911, S272).** `vercel-deploy.yml`, `frontend/vercel.json`, the per-PR previews,
  the fixed Preview host and its `preview-signin` label are gone. Per-PR UI evidence is the required check
  `Visual (next start, public pages + kit gallery)` (root ADR 0019 d23 R7); the deployed host is checked after each
  deploy (below).
- **Frontend (ECS, AA-911, root ADR 0019 d23)**: `.github/workflows/deploy-admin-dev.yml` builds
  `frontend/Dockerfile` (Next standalone, `NEXT_OUTPUT=standalone`) on an arm64 runner,
  pushes `aa-cis-dev-admin:dev-<sha>` (IMMUTABLE), copies the latest revision of family `aa-cis-dev-admin` with only the
  image swapped (env and secrets from that revision), rolls service `aa-cis-dev-admin` and checks
  `https://aa-cis.lumiguides.it.com/api/healthz` answers this commit from ECS (resolved to the ALB on the runner
  through `vars.STAFF_WEB_ALB_DNS`), then runs the UI smoke on that host (job `smoke-production`, public + signed in)
  and the report-only visual run (job `visual-production`). The Booking admin is a separate service (AA-Booking). A PR touching the Dockerfile runs the image
  build only ("Admin Docker Build Check"). Behind the ALB the standalone server's `request.url` / `nextUrl` name its
  bind address (`https://0.0.0.0:3000`), so redirects use a relative Location (`lib/redirect.ts`) and the same-origin
  check takes the public origin from `Host` when `X-Forwarded-Proto` is https (`lib/sameOrigin.ts requestOrigin`).
- **Portal**: no workflow here deploys, builds or tests the portal. It deploys from AA-Partner (`deploy-portal-dev.yml`,
  ECS `aa-crm-dev-portal` behind CloudFront since the P2b cutover); its Vercel project was deleted at the P2b
  clean-up (AA-979, runbook §4.5).
- **Visual run (AA-1008 S1d/S2c, root ADR 0016 decision 9, `.github/workflows/visual.yml`, report-only)**: the
  pages a change touches (`tests/e2e/visual/changed-pages.mjs`; a kit, shell, `lib/` or layout change = every page)
  at 1440 x 900 and 390 x 844, light and dark (Playwright projects `visual-1440` / `visual-390`, `visual-staff-*`),
  asserting no horizontal overflow of the document AND of the kit frame (PageFrame / ShellFrame main scroll on their
  own), 0 console errors, no failed same-origin request, `[data-kit-frame]`, the theme applied; PNGs + `report.json`
  as an artifact, one PR comment. Per PR (job `pr`): `next build` with `KIT_GALLERY=1` + `next start` in the runner,
  the changed public pages and the gated `/kit-gallery` route (404 in every build without the flag), after a
  self-test that must fail on a planted overflow (`/kit-gallery?plant=overflow|frame`). After the production deploy
  (job `deployed`, called by `deploy-admin-dev.yml` job `visual-production`; or a dispatch from `main` with `base_url`):
  the changed pages on the host, staff pages signed in through the smoke setup (Cognito E2E user, environment `e2e`).
  `tests/e2e/visual/pages.ts` lists every route or excludes it with a reason (guard `frontend/lib/visualPages.test.mjs`).
- **UI smoke (AA-732, `.github/workflows/ui-smoke.yml`)**: called by `deploy-admin-dev.yml` after a
  verified deploy from `main` (`workflow_call`, inputs `base_url` + `sha` + `staff_session`), or run by hand
  (`workflow_dispatch`). A Playwright suite (`tests/e2e/smoke/`)
  opens the key admin pages and fails the check on any HTTP/console error, failing same-origin `/api/` call, or a
  blank page (no table rows and no kit empty state). **Split (AA-816 S2 path):** specs that need a session carry
  the tag `@staff` (project `smoke`, after `smoke-setup`); every other spec is project `smoke-public` (no session).
  Job `smoke` runs only `smoke-public`. The signed-in
  run (job `smoke-staff`, `staff_session=true`, from `main` only: GitHub environment `e2e`, the read-only mailbox
  role, concurrency group `e2e-cognito` shared with the Cognito E2E) signs in as the Cognito E2E user with an email
  code (`tests/e2e/helpers/cognitoSignIn.ts`, shared with `tests/e2e/cognito/`) on the callback host only:
  Production after every deploy (`deploy-admin-dev.yml` job `smoke-production`; the user is `content` (reviewer until
  CIS 375), so content mode: pages from `frontend/lib/rbac.ts` `PAGE_RULES`, no writes). Screenshots (desktop 1440 +
  mobile 390, light + dark) upload as an artifact (the public run also uploads the HTML report; the signed-in run
  never does: a `fill` step would name the code) and link from one PR comment each. The specs still read an optional
  `VERCEL_AUTOMATION_BYPASS_SECRET` (`installBypassRoute`), which no workflow passes since the Vercel retirement (a
  no-op). The `E2E_ADMIN_*` password secrets are no longer passed, and since S2 the smoke setup
  has no password mode (locally, run `--project=smoke-public`; agents have no mailbox access). The admin suite also checks that the old portal URLs answer 308
  to the portal host (`aa885-portal-redirect.spec.ts`). Not a required check.
- **Portal UI smoke**: moved to AA-Partner with the portal (AA-979: `e2e/smoke-portal/`, `ui-smoke-portal.yml`).
  e2e specs carry no hardcoded credentials (shared helper
  `tests/e2e/helpers/auth.ts` reads the env). ESLint enforces UI-kit conventions in
  `frontend/eslint.config.mjs` (`frontend-lint` CI job, the kit preset at `error` with a shrinking suppressions file
  since AA-994 S1c, see "Kit lint").
- **Cognito E2E (AA-816 E1, `.github/workflows/e2e-cognito.yml`)**: `workflow_dispatch` on `main` only,
  GitHub environment `e2e`, OIDC role from secret `E2E_MAIL_ROLE_ARN` (Infra `aa-cis-dev-e2e-mail-reader`,
  read-only: List + Get on `s3://aa-cis-dev-e2e-mail-<acc2>/inbound/`, no delete; 1-day expiry). Playwright
  project `cognito` (`tests/e2e/cognito/`, always the production host — the only `cis-admin` callback): staff
  user "E2E Cognito" (`e2e@e2e-mail.lumiguides.it.com` — the SES test mailbox itself, content, Booking none; a
  TEMPORARY exception to the `@adventure.asia` rule, see Staff roles) signs in by email code (`tests/e2e/helpers/mailbox.ts` reads the code from S3), lands under `/admin`,
  then (AA-680 staff-half evidence) opens the Booking staff admin `https://booking.lumiguides.it.com` and signs in
  with company email on the shared managed-login session — Booking must refuse it (`/login?reason=no_booking_role`,
  no `bk_*` session cookie, `/` still bounces to `/login`; screenshots `04b`–`04d`) — goes back to CIS, signs out,
  no silent re-login (the next sign-in shows the address form and then sends a new code, which is never typed). The sign-in test skips unless `E2ECOGNITO=1` (set only by the workflow); only screenshots
  are uploaded (reports would carry the typed code). Not on any PR.
- **Migrations are not applied by Deploy Dev.** Deploy only builds the image and updates the ECS
  task definitions. Migrations are applied only through the AA-CIS-Infra **DB Migrate** workflow
  (`check` → `apply`) from a byte-identical copy in `db/migrations/cis/` (S225); never by hand. A
  migration that the new code needs is applied before that code merges.
- **`catalog.*` view changes use the X1 S22 catalog recipe** (AA-852 / AA-860, X1 plan S22 / XD9,
  `AA-Ecosys/docs/audits/2026-10-17-S241-x1/x1-plan.md`). The views belong to the NOLOGIN role
  `catalog_owner`; since 245 schema `catalog` belongs to `cis_owner` and `catalog_owner` only has USAGE on it, so
  `CREATE OR REPLACE VIEW catalog.*` fails both as `cis_owner` ("must be owner") and as plain `catalog_owner`
  ("permission denied for schema catalog"). A migration here changes a catalog view only like this, inside its one
  `BEGIN … COMMIT` (DB Migrate already runs the file as `cis_owner`):
  `GRANT CREATE ON SCHEMA catalog TO catalog_owner; SET LOCAL ROLE catalog_owner; CREATE OR REPLACE
  VIEW catalog.v_… AS …; SET LOCAL ROLE cis_owner; REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;`
  **Never `RESET ROLE`**: it returns to the DB Migrate login `db_migrator`, which inherits CREATE on every CIS
  schema through `cis_owner`, so what is created after it is `db_migrator`'s (outside the `FOR ROLE cis_owner`
  default privileges: `cis_app` gets 42501); the AA-CIS-Infra role lint refuses it and DB Migrate's ownership
  post-check rolls such a file back (exit 9). The recipe needs a DB Migrate login that is a member of
  `catalog_owner`: `db_migrator` (from X1 S22; Infra rl-002), never the RDS master (not a member since 225 and must
  never become one: the `catalog_owner_aa_master_read` policies would reach its members). Example and CI proof:
  `tests/integration/test_aa860_migration_246.py`. The old 225/235 `GRANT catalog_owner TO CURRENT_USER` dance stays
  refused by the role lint for files ≥ 243. **The XD9 freeze** (no `catalog.*` view change from the X1 S1 merge until
  S22, because the master was the login) **ends when 246 is applied on Dev** (DB Migrate logs `ran as: cis_owner`
  with login `db_migrator`); if the migrator task definition is ever rolled back to the master, DB Migrate refuses a
  recipe file again at `check` (exit 8).
- **Catalog L4 read model (CIS 253, AA-847; the first view change under the recipe above).** `catalog._live_tour`
  (internal, not barrier, not granted) is the one definition of a live AA tour (225's `live` CTE, whole projection);
  `v_tour` is built on it and every place / graph view joins it instead of `v_tour`, so a graph read no longer
  computes `v_tour`'s photo count, cover, duration regex and md5 per tour. `catalog._live_stop` (current build ⋈ live
  ⋈ usable destination) feeds `v_tour_stop`, `v_graph_node` and `v_tour_junction`. `catalog.v_component_embedding`
  (pgvector only, `tripplanner` only) serves the HNSW top-k over the components `v_tour_component` shows (live tour,
  usable place: never a suspect or merged one), so the reader checks nothing after the scan. Columns and rows of
  every contract view are unchanged (fingerprint test).
- **Line places in the catalog (CIS 256, AA-847; contracts `catalog.tour.v1.2`, `catalog.graph.v1.2`).** Columns only
  appended: `v_destination` gains `shape_kind`, `shape_status`, `shape_source`, `length_km`, never-null `min_lat` /
  `min_lng` / `max_lat` / `max_lng` (a line's shape bbox, else the point: map tiles filter by bbox intersection with
  plain float comparisons, pushed below the barrier; a read without photo columns plans no photo LATERAL) and
  `shape_lo`; `v_tour_place` and `v_tour_component` gain the current build's day stretch (`segment`,
  `segment_basis`, `segment_from_destination_id`, `segment_to_destination_id`, `segment_km`; the component view also
  `precision`); `catalog.v_destination_shape` (tripplanner only) serves a line's `shape_mid` / `shape_lo` by id;
  `_live_stop` never shows a line. Tests apply 253 + 256 inside one rolled-back transaction
  (`tests/integration/test_aa847_migration_256.py`, with the EXPLAIN checks in the consumer's filter shape).
- **Countries (CIS 279, AA-851; data standards X5 / DM-2.4 / NAM-11; contract root `catalog.country.v1`).**
  `shared.countries` is the one ISO 3166-1 table of the ecosystem (249 rows, key `iso2`; `name` = the common English
  name AA writes, `iso_name`, `aliases`). `shared.country_iso2(text)` is the one free-text resolver (same answers as
  `shared/countries.resolve` for every lookup key; test_aa990_migration_288 pins it). `raw_tours` and
  `shared.destinations` carry `country_iso2` (FK, filled by the trigger `shared.set_country_iso2()` from `country`
  for every writer; an explicit value is kept; unknown → NULL, listed by `shared.v_country_unmapped`). A role that
  writes either table needs SELECT on `shared.countries` and EXECUTE on the resolver (the trigger runs as the writer,
  MIG-6; 279 grants both to every writer it sees, on Dev `cis_app`, and refuses a writer without them); other apps
  read `catalog.v_country` and `catalog.v_destination.country_iso2` (booking, tripplanner). New country columns are
  `country_iso2 char(2)` keyed to `shared.countries`, never a new free-text key.
  **AA-990 (CIS 288, S252): no hand-made "AA country" list.** `raw_tours.country` is a foreign key to
  `shared.countries.name` (the 20-name CHECK `chk_raw_tours_country` is gone) and `(country IS NULL) = (country_iso2 IS
  NULL)`. A tour's country is exactly what its source says: S0 (`shared/country_resolver.py`) takes the Excel column,
  the file name only when the column is empty, never the file name over a different column value, never a Jev guess
  and never a re-label from the itinerary. Each place gets its own real country from geocoding
  (`shared.destinations.country`). The code reads every country from `shared/countries` — the in-code mirror of
  `shared.countries` (`countries.json`, generated by `scripts/aa990_country_data.py` from 279 + 288 with Natural
  Earth bounding boxes and tzdata zones; drift tests `tests/unit/test_aa990_country_mirror.py` and
  `tests/integration/test_aa990_migration_288.py`): the place guards, the polygons (`services/places/geodata`, every
  ISO country, parsed per country on first use), Geoapify's `countrycode`, catalog.http.v1 codes and zones, AA content
  `applies_to.country_codes`, the SEO meta country words. A compound place-card country ("India and Pakistan") is never
  stored on a new card (`place_cards.normalise_card_country`); old compounds stay inside identity keys. What stays
  AA-specific on purpose: SEO buyer markets, `booking.countries`, `geo.place_dedupe_country` approvals, brand rules, the
  hand-tuned zone splits and bounding boxes, `dedupe.COUNTRY_WORDS`, the SEO keyword filter's `COUNTRY_MASTER` words.
- **The CIS runtime role is `cis_app` (X1, AA-860; api + worker switch in S18).** Migration 244 grants it
  DML / sequences / non-trigger function EXECUTE in the CIS runtime schemas (11 then, 7 since CIS 299; not
  `catalog`, `booking`, `tripplanner`, `public`), default privileges for later objects (`FOR ROLE aa_cis_admin` in 244, restated
  `FOR ROLE cis_owner` by 245: the approved exception XD3, data standards Part A §9), read-only
  `shared.schema_versions`, and a permissive `<table>_cis_app_all FOR ALL TO cis_app USING (true) WITH CHECK
  (true)` policy on every RLS table, ENABLE-only **and** FORCE (the master reads FORCE tables unfiltered
  today; most code sets no GUC). **Every new RLS table in those schemas needs that policy in the same
  migration** (CI standing check `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set on an RLS table must allow it. This holds for an ENABLE-without-FORCE table too: `cis_app` is
  not the owner, so without a policy `TO cis_app` it reads 0 rows and its writes are refused, with no error. The
  isolation probe checks it nightly (cells `rls.enable_only_has_cis_app_policy`, `rls.force_parity.cis_app`). The
  conftest replay skips 244; the X1 tests apply and undo it. Live since X1 S18 (19/10/2026): api + worker
  `DATABASE_URL` = `aa-cis/dev/rds-cis-app`, and ECS-exec scripts run as `cis_app` too (the task role is denied the
  master secret since S19; master work goes through Infra DB Migrate `run-script`).
- **CIS schemas and objects belong to the NOLOGIN role `cis_owner` from migration 245 (X1 S21, AA-860).** 245 moves
  the 12 CIS schemas and every object the RDS master owned in them (the `catalog.*` views stay `catalog_owner`'s;
  `public.*` and the extensions stay the master's, they belong to the Infra `roles/` dir). From then on DB Migrate runs
  every `cis/` file as `cis_owner` (it switches to the live owner of schema `shared`; from X1 S22 the login is
  `db_migrator`), and default privileges `FOR ROLE cis_owner` give `cis_app` what a file creates. Rules for every
  later CIS migration: never `RESET ROLE` (go back with `SET LOCAL ROLE cis_owner`), no `SECURITY DEFINER`, no
  `CREATE/ALTER/DROP ROLE|EXTENSION` (→ Infra `roles/`). The legacy enums in `public` (`pipeline_status_enum`,
  `content_status_enum`, `review_status_enum`, … used by `raw_tours`, `generated_content` and others) stay the
  master's, so `ALTER TYPE public.<enum> ADD VALUE` (as in 073 / 158) fails as `cis_owner` ("must be owner of type"):
  it needs an Infra `roles/` file (runs as the master). `cis_owner` is **filtered by FORCE RLS** (unlike the master,
  it is not in `rds_superuser`), and every view without `security_invoker` reads its base tables as its owner, so 245
  gives it a permissive `<table>_cis_owner_all FOR ALL TO cis_owner USING (true) WITH CHECK (true)` policy on the 8
  FORCE tables: a migration that FORCEs RLS on another table adds both `<t>_cis_app_all` and `<t>_cis_owner_all` in
  the same file (CI standing checks in `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set must allow both. Only the master and `db_migrator` may be members of `cis_owner` (a member reads
  every tenant of those tables). The conftest replay skips 245; `test_aa860_migration_245.py` applies it as an
  RDS-like master after 244 and undoes it. Rollback is a prepared Infra `roles/` undo (a `cis/` file runs as
  `cis_owner` and cannot give objects back). The CIS migration freeze (Infra copy of 245 until X1 S22's 246 probe) is
  **over**: 245 applied 19/10/2026 12:11 UTC, 246 applied as `cis_owner` with login `db_migrator`, probe 58/58.
- **DB Migrate logs in as `db_migrator` from X1 S22 (AA-860)**, not the RDS master: every `cis/` file runs as
  `cis_owner` through the live-owner switch, and migration **246** (`246_x1_set_role_probe.sql`, assert-only) is the
  permanent proof (session user `db_migrator`, current user `cis_owner`, `db_migrator` a member of `catalog_owner`).
  The conftest replay skips 246; `test_aa860_migration_246.py` applies it as `db_migrator` under the switch, after
  245. The master keeps only Infra `roles/` files (roles, extensions, `public.*` enums), DB Migrate `set-password` and
  `run-script`; anything a `cis/` file cannot do as `cis_owner` goes there, never into a `cis/` file: no
  `CREATE/ALTER/DROP ROLE|EXTENSION` and no role attribute (`CONNECTION LIMIT`, `PASSWORD`) outside Infra `roles/`.
- **Verify the frontend with a full `next build`**: `tsc --noEmit` and eslint miss some type
  errors that break the Vercel build. The CI Lint job only runs flake8 on Python
  (`--max-line-length=120`).
- **Health and version (AA-884)**: `GET /health` returns `git_sha` (the image's `APP_GIT_SHA`, the merge commit) and
  `GET /health/ready` (public, 5 s in-process cache) returns 200 only when the DB answers and the newest numeric CIS
  migration in `shared.schema_versions` is ≥ `api/core/readiness.py` `REQUIRED_CIS_SCHEMA` (bump it in the PR whose
  code needs a newer migration). A needed number applied after a higher one (a reserved number, e.g. 297 after 299)
  goes into `OUT_OF_ORDER_CIS`: while it is not registered the reported version is capped just below it. Since AA-993
  M4a it also requires every key of `REQUIRED_CIS_KEYS` (each number from `LEGACY_CIS_BASELINE` + 1 = 264 up to
  `REQUIRED_CIS_SCHEMA`; a gap is `schema_behind` with `missing_schema_versions`) and lists `registered_keys` (every
  numeric key above the baseline, read by the Deploy Dev `schema-gate` job; omitted if that read fails). Deploy Dev's
  smoke asserts both, and that a live worker reports the same SHA (`shared.job_worker.git_sha`, written at worker
  start).
- **Release version (AA-891)**: release-please (manifest mode, `release-please-config.json`,
  `.release-please-manifest.json`, workflow `release-please.yml` with the CI bot GitHub App token) keeps one release PR
  open on `main`; merging it bumps the manifest, `CHANGELOG.md` and `api/version.py` (the only copy in code: FastAPI app
  version and `/health` `version`) and tags `vX.Y.Z`. Never edit the version by hand. A release-only merge does not
  deploy (`!api/version.py` in Deploy Dev's paths); the next code merge ships it, and the smoke asserts `/health`
  `version` equals the deployed commit's manifest.
- **API reference and breaking-change check (AA-899)**: `docs/api/openapi.json` is the OpenAPI document of `api.main`,
  exported by `scripts/export_openapi.py` (no DB, Redis, secret or AWS call; deterministic, no `servers`,
  `info.version` = `unreleased`). Unit Tests fails when it is stale (`tests/unit/test_aa899_openapi_export.py`, which
  also keeps the list of public routes without a response model, `tests/unit/snapshots/aa899_untyped_public_routes.json`,
  from growing). The "API contract" workflow (`api-contract.yml`) diffs it against the base branch with oasdiff
  (pinned) and fails a PR that breaks a public path (everything but `/admin/*`) unless the PR carries the label
  `api-breaking` or a `type!:` title; `/admin/*` breaks are warnings. FastAPI and Pydantic are pinned in
  `requirements.txt` for that reason. Each release gets the document as an asset (`aa-cis-openapi-vX.Y.Z.json`).
  How-to: `docs/api/README.md`.
- **API security headers (AA-1111, S284)**: `api/middleware/security_headers.py` (just inside the request id
  middleware) adds `X-Content-Type-Options: nosniff` and HSTS (same value as the frontends) to every response that
  does not set them itself; the unhandled 500 gets them from `unhandled_error_handler`. The image starts uvicorn with
  `--no-server-header`. Responses API Gateway answers itself (missing route 403, throttling 429) get the same two
  headers from its gateway responses (AA-CIS-Infra).
- **Logs are JSON with request / job ids (AA-871)**: `shared/log_config.py` (called once by `api.main` and
  `python -m worker`, not under unit tests) renders structlog, stdlib and uvicorn lines as one JSON object each on
  `/ecs/aa-cis-dev` (`service` = `cis-api` | `cis-worker`, `git_sha`, `environment`). The outermost API middleware
  `api/middleware/request_id.py` accepts a well-formed `X-Request-ID` (else generates one), returns it on every
  response, binds `request_id` for the request and writes one `request_completed` line (method, path, route, status,
  duration; never query string, headers or body) in place of uvicorn's access log. `/v1/*` lines also carry the
  verified `tenant_id` (rate limiter); worker lines carry `job_id` / `job_kind`. Find a request with Logs Insights:
  `fields @timestamp, event, level | filter request_id = "<id>" | sort @timestamp`. Keep event names stable: Infra
  metric filters match them as substrings.
  An explicit `request_id=` on a log call (the T8/T9 angle-gate / content-writing request) is logged as
  `acp_request_id`, so the HTTP `request_id` stays on the line; use `acp_request_id` for the domain id.
- **Definition of "done" for a backend issue** (program rule): merge + green CI is not enough.
  Also verify (a) Dev deploy **rollout COMPLETED** (ECS running the new taskDef, old deployment
  drained) and (b) the **live endpoint** (curl → HTTP 200 + correct shape). Only then is it Done.
