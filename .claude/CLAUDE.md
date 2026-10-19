# AA-CIS-App — Claude Code context

Short, current rules for agents. **What the system is and how it fits together lives in
[`CONTEXT.md`](../CONTEXT.md) — read it before any design or multi-module change.** History lives in
git, Linear and the session logs (`AA-Ecosys/docs/sessions/`), not here. Rewritten 11/10/2026
(AA-654); the old append-only LIVE STATE was dropped.

## Where things are
- Workspace: `~/projects/AA-Ecosys/apps/AA-CIS-App` (multi-repo; own `.git`). Org
  `AdventureAsia365-Ecosys`. Sibling repos: AA-TripPlanner-Web, AA-CIS-Infra, root AA-Ecosys.
- Live: API `https://api-cis.lumiguides.it.com` (API GW `4ylo382khg` → ALB → ECS
  `aa-cis-dev-cluster`: services `aa-cis-dev-api` + `aa-cis-dev-worker`), admin/portal
  `https://aa-cis.lumiguides.it.com` (Vercel). AWS acc2 `005097885195`, us-west-1.
- Current revisions / migration number: do not trust a number written here — check
  `aws ecs describe-services`, `shared.schema_versions`, or the latest session block in memory.
- ECS logs: `/ecs/aa-cis-dev` (api stream prefix `api/`). `/ecs/aa-cis-dev-api` is always empty.

## Data rules (shared RDS, Postgres 15)
- Read `AA-Ecosys/docs/architecture/db-schema-reference.md` (live dump) before writing a query;
  never guess a table or column. Owners per schema: `AA-Ecosys/docs/database/schema-ownership.md`.
- Schema-qualify every table (`silver_aa_internal.raw_tours`, not `raw_tours`).
- `raw_tours` PK is `tour_id` (not `id`). `published_tours` and `seo_context` have no `country`
  — JOIN `raw_tours`. Atom reads go through `acp_contract.v_active_tour_atoms`.
- Background work runs on `shared.job` (durable runner, ADR 0003); `shared.pipeline_jobs` is history.
- Serialize UUID / Decimal explicitly (`json.dumps(..., default=str)` or the router's `safe()`).
- Destructive SQL (DROP, DELETE of data): DRY RUN procedure in
  `AA-Ecosys/skill/aa-cis-schema/references/destructive-ops.md`, Nghiệp approves.

## Migrations
- Plain numbered files `api/migrations/NNN_*.sql`; each self-registers in `shared.schema_versions`
  (`ON CONFLICT DO NOTHING`) inside its own `BEGIN; … COMMIT;`.
- The deploy does **not** apply them. Put an identical copy in
  `AA-CIS-Infra/db/migrations/cis/`; merge the Infra PR (dev phase: the orchestrating agent merges after
  green CI, S241 D-13), run the **DB Migrate** workflow (`check` → `apply`) and only then merge the app PR
  that needs it.
- A PR carrying a migration is never auto-merged.

## LLM, cost, decisions
- Every model call goes through the Model Gateway (`shared/llm_client`, stages in
  `shared.llm_role_config` / `shared.llm_model_catalog`, editable in admin Settings). Never hardcode
  a model id; never call `invoke_claude` / `get_satellite_client` outside `shared/llm_client`
  (guarded by a test).
- Every call is logged to `shared.llm_call_log`; ad-hoc scripts must log too
  (`stage="adhoc_<issue>"`, role `writer|judge|validate`).
- Jev (TypeSafe) decisions go through `decide()` and `shared.decision_log`; Jev fails open, so
  call counts are not evidence that it ran — read `decision_log` zones.
- Generation calls are synchronous inside the graph (asyncio deadlock fix); `max_tokens` 4096.

## Code rules that bite
- FastAPI route order: a literal path (`/{id}/full`, `/check`) must be declared before the
  catch-all `/{id}` route of the same method.
- Admin pages call same-origin `/api/admin/[...path]` (the BFF adds the admin secret); never call
  the API URL from the browser. New UI uses the kit in `frontend/app/_kit/`.
- Content rules for the AA brand (forbidden words, CTA, tone) live in the brand identity rows and
  `services/content_generation/` — do not hardcode new ones in prompts.

## Process
- Branch → PR → CI (Lint, Security Audit, Unit Tests, Integration Tests, Docker Build Check) →
  squash merge to `main` → `Deploy Dev`. No `develop` branch (ADR-2026-023).
- Do not merge or deploy backend while `shared.job` has queued/running jobs or a wave is running
  (the Deploy Dev guard blocks it; override is Nghiệp's call).
- "Done" = merged + CI green + deploy rollout completed + live endpoint verified
  (`AA-Ecosys/skill/aa-ship/SKILL.md`). Commits / PRs say `Refs AA-xxx`, never `Fixes`.
- Each issue with code keeps `docs/implementation-notes/AA-xxx.md` (Decisions / Changed /
  Tradeoffs / Should know), written while working. `docs/**/*.md` is tracked (AA-889); this repo is
  public, so no account IDs, ARNs, resource IDs, secrets or real data in docs, and security or
  infrastructure reports go to the private AA-Ecosys root repo.
- Hard-to-reverse architecture decisions get an ADR in `docs/adr/` (ecosystem-wide ones in
  `AA-Ecosys/docs/adr/`). Large specs: run `/grill-with-docs` before coding.
- Language: talk to Nghiệp in Vietnamese; code, comments, commits, PRs, Linear in English.
- Never start/stop infrastructure (ECS scale, RDS, NAT) on your own.

## Tests
```bash
pytest tests/unit/ -q          # no DB; CI env: ENVIRONMENT=test, dummy DATABASE_URL / JWT_SECRET
pytest tests/integration/ -q   # real Postgres; conftest applies every migration to an empty DB
flake8 .                       # max line 120
```
