# AA-CIS-App — Content Intelligence System

The content system of record of the AdventureAsia365 ecosystem (ADR 0001 / 0002). It ingests raw
supplier tours, rewrites them into AA Master Content (A-series, admin), breaks them into platform
atoms, Segments, Routes and Hubs (A3), and lets B2B tenants turn that catalog into their own tour
pages and social content (T-series, tenant portal).

- **Domain and architecture:** [`CONTEXT.md`](CONTEXT.md) — read it before designing a change.
- **Decisions:** [`docs/adr/`](docs/adr/) (this app) and `AA-Ecosys/docs/adr/` (ecosystem).
- **Schema ownership (shared RDS):** `AA-Ecosys/docs/database/schema-ownership.md`.

## Architecture

```
Admin console (Next.js, Vercel)          ──/api/* BFF (holds the admin secret / tenant JWT)──┐
                                                                                               ▼
                               API Gateway (4ylo382khg) → ALB → ECS Fargate
                                   ├─ aa-cis-dev-api     FastAPI (api.main:app)
                                   └─ aa-cis-dev-worker  durable job runner (python -m worker)
                                                 │
            ┌────────────────────────────────────┼───────────────────────────────┐
            ▼                                    ▼                               ▼
  RDS Postgres 15 (acc2, shared)       Bedrock (acc3 satellite → acc1)    Jev (TypeSafe) decisions
  silver/gold_aa_internal, acp_*,      Claude / GPT Luna / Cohere via     DataForSEO (keywords/SERP)
  shared.* (jobs, LLM cost, gateway)   the Model Gateway                  Redis (cache, live progress)
```

| Path | What |
|---|---|
| `api/` | FastAPI app (`api/main.py`), routers (`api/routers/`), numbered SQL migrations (`api/migrations/`) |
| `services/` | Pipeline and domain logic (content generation, A3 atomize, Segments/Routes, T-series, publish, waves) |
| `shared/` | LLM client + Model Gateway, Jev decision layer, job queue, cost guard, secrets |
| `worker/` | Entry point of the job worker service (`python -m worker`) |
| `frontend/` | Next.js admin (`app/admin/*`); UI kit in `app/_kit/` |
| `portal/` | Frozen, undeployed copy of the tenant portal: since AA-979 its source is `AdventureAsia365-Ecosys/AA-Partner/portal` (deleted here at P2b + 7 days) |
| `tests/` | `unit/` (no DB), `integration/` (real Postgres), `acp/`, `e2e/` (Playwright) |

## Local setup

Requirements: Python 3.12, Node 20+, PostgreSQL 15/16 (for integration tests), Redis (optional).

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill in the values (names below)
uvicorn api.main:app --reload --port 8000
python -m worker                 # job worker (separate process)

cd frontend && npm ci && npm run dev    # http://localhost:3000
```

## Tests

```bash
# unit — what CI's "Unit Tests" job runs
ENVIRONMENT=test DATABASE_URL=postgresql://test:test@localhost:5432/test JWT_SECRET=x \
  pytest tests/unit/ -q

# integration — real Postgres; conftest applies every api/migrations file to an empty DB
DATABASE_URL=postgresql://cistest:cistest@localhost:5432/cis_integration_test \
  pytest tests/integration/ -q

flake8 .                         # max line length 120 (.flake8)
cd frontend && npm run lint
```

CI (`.github/workflows/ci.yml`) requires Lint, Security Audit, Unit Tests, Integration Tests and
Docker Build Check; `vercel-deploy.yml` deploys the frontend preview and then runs a Playwright UI
smoke on it.

Secret scan (`.github/workflows/secret-scan.yml`, AA-981 follow-up): check **gitleaks** on every PR
(the lines the PR adds; rules and baseline read from the base branch) and **gitleaks full history**
(`secret-scan-history.yml`) nightly, on push to `main` and on demand (every branch, tag and pull request head). Rules in
`.gitleaks.toml` (same as the root repo), reviewed findings pinned by commit in `.gitleaksignore`. A
PR that changes either file, either secret scan workflow, `.github/CODEOWNERS` or any `.gitattributes`, or adds
an inline `gitleaks:allow` comment, fails until a
reviewer adds the label `secret-scan-baseline-reviewed` after the last push (any later push, reopen or label
change removes it). For a false
positive prefer a narrow allowlist in `.gitleaks.toml`: a commit fingerprint does not survive the squash
merge (header of `.gitleaksignore`). A red result means the value is public already: rotate it, even if it
was force-pushed away.
The PR check runs on `pull_request_target`: GitHub always runs the base branch's copy of the workflow and the PR head
is only read as data, so a PR cannot change the check that judges it. It also scans a squash preview of the PR (what
the squash merge writes), and it fails when another workflow has a job named `gitleaks`. `.gitleaks.toml` carries a
copy of the gitleaks v8.30.1 default rules and no path allowlist, so every file is scanned (`.svg`, `go.sum`,
`node_modules/`, images, lock files); UTF-16 files are transcoded to UTF-8 and scanned too.
Local commands are in the header of `secret-scan.yml`.

## Deploy

- **Backend:** merge to `main` → `Deploy Dev` (`.github/workflows/deploy-dev.yml`): pre-deploy job
  guard (blocks while `shared.job` has queued/running jobs), build + push to ECR, new task
  definitions for api and worker on one image, wait stable, same-SHA check, smoke tests.
- **Frontend deploys:** CI-created via `.github/workflows/vercel-deploy.yml` (only when `frontend/**`
  changes: PR → preview + UI smoke, `main` → production); the Vercel Git integration is off via
  `frontend/vercel.json` (`git.deploymentEnabled: false`). Needs the repo secret `VERCEL_TOKEN`.
- **Migrations are not applied by the deploy.** A new `api/migrations/NNN_*.sql` is applied to Dev
  from its identical copy in `AA-CIS-Infra/db/migrations/cis/` by the **DB Migrate** workflow
  (check → apply), **before** the code that needs it merges.
- Infrastructure (ECS, RDS, API Gateway, IAM, alarms) is Terraform in **AA-CIS-Infra**.

## Environment variables

Names only — values come from Secrets Manager / the ECS task definition (AA-CIS-Infra) or Vercel.

| Group | Variables |
|---|---|
| Core | `ENVIRONMENT`, `DATABASE_URL` (or `SECRET_DB_ARN`), `REDIS_HOST`, `REDIS_PORT`, `REDIS_TLS`, `REDIS_AUTH_TOKEN` (secret), `AWS_REGION`, `BEDROCK_REGION` |
| Auth | `ADMIN_SECRET`, `JWT_SECRET`, `INTERNAL_API_KEY` |
| LLM / decisions | `OPENAI_API_KEY`, `TYPESAFE_API_KEY` / `SECRET_TYPESAFE_ID`, `TYPESAFE_BASE_URL`, `DEFAULT_MODEL_TIER`, `S1_WRITER_TEMPERATURE` |
| SEO data | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` (or `SECRET_DATAFORSEO_ARN`), `S1_SEO_REUSE_DAYS` |
| Jobs | `JOB_WORKER_IN_API`, `JOB_WORKER_MAX_PARALLEL`, `JOB_WORKER_TASK_REVISION` |
| Storage / photos | `BRONZE_BUCKET`, `PHOTO_BUCKET`, `GDRIVE_API_KEY` / `SECRET_GDRIVE_ID`, `PUBLIC_API_BASE`, `TRIP_PAGE_BASE_URL` (AA-979, origin of published trip page URLs) |
| Ops / alerts | `ALERT_TOPIC_ARN`, `SWEEPER_ALERT_SNS_ARN`, `SWEEPER_SLA_HOURS`, `DFS_BALANCE_ALERT_THRESHOLD_USD`, `JEV_CREDIT_COOLDOWN_MIN`, `ECS_CLUSTER`, `ECS_SERVICE`, `RDS_INSTANCE_ID`, `NAT_INSTANCE_ID`, `REDIS_CLUSTER_ID` |
| Frontend (Vercel) | `API_URL`, `NEXT_PUBLIC_API_URL`, `ADMIN_SECRET`, `INTERNAL_API_KEY` |

Some legacy variables (Langfuse) are still read by old code paths and are not set on Dev.
The Step Functions pipeline and its SQS queues were retired (AA-750), so `STEP_FUNCTIONS_ARN` /
`SF_BATCH_THRESHOLD` on the api task definition are now unused and can be dropped from the task def
in a future Infra change.

## Conventions

- Commit / PR: English; reference issues as `Refs AA-xxx` (never `Fixes`, which closes the issue).
- Every issue with code keeps `docs/implementation-notes/AA-xxx.md` (decisions, changes, tradeoffs).
- Agent rules for this repo: [`.claude/CLAUDE.md`](.claude/CLAUDE.md).
