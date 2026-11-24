---
id: ARCH-06-cis
title: "AA-CIS-App — Repo Context"
description: Stack, boundaries, ownership, domain terms and working rules of AA-CIS-App (API incl. the Content studio API, job worker, admin app); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-cis-api, aa-cis-worker, aa-cis-frontend]
last_reviewed: 2026-11-01
review_every_days: 90
related: [ARCH-01, ARCH-05, ARCH-08, docs/adr/README.md]
---

# AA-CIS-App — Repo Context

The core application of the Adventure Asia (AA) ecosystem: the **CIS (Content Intelligence
System)** backend + admin/tenant frontends. It is where a tour becomes shared master content and
where a marketplace tenant turns that content into channel-specific published content. This is the
B2B/admin side of the ecosystem — distinct from AA-TripPlanner-Web (the B2C consumer trip
planner) and AA-CIS-Infra (the Terraform that provisions the AWS resources this app runs on).

> This file is the short entry point: what the repo is, how to run it, its boundaries and the map of the
> [detail pages](#detail-pages) under `docs/context/` (split on 24/11/2026, AA-1118). The detail pages hold the
> repo context (stack, gateway, jobs, deploy, roles, tenancy) and the domain glossary of the Master Content →
> Tenant Content pipeline (AA-539/540); keep each page in sync with the code it describes.

## What it is (role in the AA ecosystem)

- **Product surface**: two audiences in one app — **Admin** (AA staff running master content and
  cross-tenant oversight, the A-series) and **Tenant** (marketplace tenants publishing their own
  channel content, the T-series).
- **Position**: the hub product. It **owns** the master content pool and the whole content
  pipeline; TripPlanner reads the shared `shared.destinations` reference data but never drives
  this pipeline. AA-CIS-Infra provisions the cloud resources this app runs on but contains no app
  logic.
- **Ownership split**: this repo owns its application code and its DB schemas/migrations
  (`acp_*`, `*_aa_internal`, `acp_shared.*`, `acp_contract.*`, and since CIS 420 `learn`, the AA Academy
  learner records of root ADR 0022). AWS resources (VPC/RDS/ECS/ALB/
  API GW/Lambda/OIDC) are owned by **AA-CIS-Infra** — "App owns code, Infra owns resources."
  Owner per schema and per `shared.*` table (CIS also owns `shared.destinations` since ADR 0002):
  `AA-Ecosys/docs/database/schema-ownership.md`. A migration here is applied to Dev from its
  identical copy in `AA-CIS-Infra/db/migrations/cis/` by the **DB Migrate** workflow, before the
  code that needs it merges.

## Run it locally

The shortest path that works without AWS access. The [README](README.md) has the full commands and the variable
names; the Dev environment is described under [Deploy & CI](docs/context/deploy-ci.md).

| Part | Command (from the repo root) | Needs |
|---|---|---|
| Python setup | `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt` | Python 3.12 |
| Unit tests | `ENVIRONMENT=test DATABASE_URL=postgresql://x/y JWT_SECRET=x .venv/bin/python -m pytest tests/unit/ -q` | nothing else: no database, no AWS |
| Lint | `.venv/bin/python -m flake8 .` (line length 120) | |
| Integration tests | `scripts/pre-push.sh --integration` runs the integration tests your diff touches on a throwaway PostgreSQL cluster it starts itself | PostgreSQL 15 or 16 binaries; by hand, a throwaway database whose user is a superuser, as in CI (the test setup applies every `api/migrations/` file) |
| API | `.venv/bin/python -m uvicorn api.main:app --reload --port 8000`, then `curl -s localhost:8000/health` | a reachable PostgreSQL in `DATABASE_URL` (the API opens its pool at start and refuses to start without one) and a local `JWT_SECRET` |
| Job worker | `.venv/bin/python -m worker` (a separate process) | the same database |
| Admin app | `cd frontend && npm ci && npm run dev` (port 3000) | Node 20 or later; the API base comes from its server environment (README, "Environment variables", Frontend row) |
| Pre-push checks | `scripts/install-hooks.sh` once per clone; each push then runs `scripts/pre-push.sh` | |

- Copy `.env.example` to `.env` and ask the tech lead for values; never point a local run at the Dev database.
- Run frontend installs in a worktree, not in the main checkout of the workspace.
- Leave the paid-provider keys (LLM, DataForSEO) empty locally: a local run must not spend money.

## Detail pages

This file is the short entry point (S282 audit R19: it had grown past 5,800 lines). The detail moved, unchanged,
to [`docs/context/`](docs/context/); read the page of the area you change before designing or changing code there.

| Page | What it holds |
|---|---|
| [Architecture at a glance](docs/context/architecture.md) | The backend and frontend stack, the repo layout and the runtime services |
| [AI gateway and model routing](docs/context/ai-gateway.md) | The gateway, connections, routes, budgets, reliability, data class per stage and the Jev decision layer |
| [Cost observability, cost guard and the job runner](docs/context/cost-and-jobs.md) | The cost ledger and views, the spend guard and the durable job runner with its kinds |
| [Batch lanes, plans, billing and tenant runtime](docs/context/runtime-features.md) | Bedrock and gateway batch lanes, plans and quota, billing, tenant account pages and live writing progress |
| [Schemas, photos, places, infra limits and settings](docs/context/data-places-settings.md) | Owned schemas, retired tables, photos, the place pipeline, infra limits and the settings registry |
| [Deploy and CI](docs/context/deploy-ci.md) | Workflows, required checks, deploy, migrations on Dev and the guards |
| [Contracts with other apps](docs/context/contracts.md) | The views, HTTP APIs and events CIS publishes or reads |
| [Staff roles and permissions](docs/context/staff-roles.md) | Staff roles and permissions, administrator MFA, the admin navigation, the audit trail, cost per app, the alerts catalogue and the retired Booking staff token |
| [Tenant control plane, lifecycle and DB role](docs/context/tenancy.md) | Tenants, plans and entitlements, lifecycle and the tenant DB role |
| [Content pipeline domain language](docs/context/domain-language.md) | The glossary of master, platform-derived and tenant content, model operations and Jev decisions |
| [Writing memory](docs/context/writing-memory.md) | Governed writing memory: rules, examples, gates (platform and tenant) |
| [Pipeline stages](docs/context/pipeline-stages.md) | The A-series and both T-series, stage by stage |
| [Ownership, frequency and cross-tenant mechanisms](docs/context/ownership.md) | Who owns each content object, how often it is computed and every cross-tenant mechanism |

## Boundaries — what this repo does NOT do

- It does not provision AWS resources or OIDC roles (that is AA-CIS-Infra).
- It does not run the B2C map trip planner (that is AA-TripPlanner-Web).
- **Only CIS writes content** (root ADR 0002, 01/10/2026): tours, atoms, places
  (`shared.destinations`), itinerary places / tour graph, photos. Other apps read views and write
  only their own schema (+ `shared.llm_call_log` telemetry); the `tripplanner` DB role is read-only on
  `shared.destinations` (migration 201). The place pipeline (extraction, geocoding, tour graph) still
  lives in the TripPlanner, frozen until it moves to CIS (AA-712).

---

# Content Pipeline Domain

The Master Content → Tenant Content glossary and the stage-by-stage pipeline now live in
[domain language](docs/context/domain-language.md), [writing memory](docs/context/writing-memory.md),
[pipeline stages](docs/context/pipeline-stages.md) and [ownership](docs/context/ownership.md).

## Open questions

1. ~~Segment/Route/Score scope contradicts this issue's own boundary statement.~~ **Closed by
   AA-545 (migration 146, 06/09/2026)**: Segment/Score/Route/Hub are platform-wide. The
   per-tenant period and the reasoning are kept in `docs/adr/0001-*.md` (Atom/Segment),
   `0002-*.md` (Search Demand shared) and `0003-*.md` (decision to go platform-wide).
2. **T5/T6 labels are now historical/dead** (see [pipeline stages](docs/context/pipeline-stages.md)) — any UI epic spec that
   still refers to "T5" or "T6" as a tenant-facing stage should be corrected to "A3 atomize" /
   "admin atom curation" respectively before build starts.

## Related documents

- `docs/investigation/aa-social-media-audit.md` — the field-by-field comparison against Ms.
  Thư's origin repo (Segment/Route/Slate/Subject/Piece) this glossary's Tenant-side definitions
  are grounded in.
- `docs/adr/0001-atoms-platform-wide-segments-per-tenant.md` — the ADR for Open Question 1's
  Atom/Segment half; also records why the Segment/Score/Route/Hub per-tenant default exists.
- `docs/adr/0002-search-demand-shared-across-tenants.md` — the ADR for Open Question 1's Search
  Demand half (AA-540).
- `docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md` — the decision (AA-542)
  that Segment/Score/Route/Hub per-tenant was tech debt; carried out by AA-545.
- `docs/adr/0004-live-writing-progress-via-redis-poll.md` — live writing progress (AA-637).
- `docs/adr/0010-governed-writing-memory.md` — governed writing memory: release-gated rules and
  pinned examples, no fine-tuning; supersedes the AA-132 lessons loop (AA-915).
- `docs/implementation-notes/AA-526.md`, `AA-527.md`, `AA-529.md`, `AA-540.md` — build records
  for the A3 atomize move, the admin atom-curation page, Facts Entry, and this table
  respectively.
