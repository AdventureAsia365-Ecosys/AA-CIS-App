# S241 — Vercel deployments only from CI (chore, no Linear issue)

Goal: stop Vercel deployments that are not needed. The Hobby team cap is ~100 deployments / 24 h for the
whole team (aa-cis-frontend, aa-tripplanner, aa-booking-admin), and deployments CANCELED by the Ignored Build
Step still count. Vercel's "skip unaffected projects" needs npm workspaces (and backend changes count as
global), so it cannot help. Same change in AA-CIS-App, AA-TripPlanner-Web and AA-Booking
(branch `chore/s241-vercel-ci-deploys`).

## Decisions
- Git integration off with `frontend/vercel.json` → `git.deploymentEnabled: false` (the file in the
  project's Root Directory is the one Vercel reads). The repo stays connected to Vercel (commit links, env
  per branch); only automatic deployments stop.
- New `.github/workflows/vercel-deploy.yml`: `pull_request` (opened/synchronize/reopened) and `push` to
  `main`, both filtered to `frontend/**` + the workflow file; `workflow_dispatch` with `target`
  preview|production. Production from any ref other than `main` fails the plan step.
- Deploy from the REPO ROOT with `npx --yes vercel@60.0.1 deploy --yes --token "$VERCEL_TOKEN" --scope <team id>`
  (+ `--prod`); the project's Root Directory (`frontend`) makes Vercel build `frontend/` remotely with the
  project's own env vars. No `vercel pull` / `--prebuilt`: the build stays on Vercel exactly as before.
  60.0.1 was published 25/09/2026 (> 2 weeks old); latest on 17/10 was 64.5.0.
- The PR deploy checks out the PR HEAD sha (`github.event.pull_request.head.sha`), not the synthetic merge
  commit, to match what the Git integration used to deploy.
- `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` are plain workflow env (ids are not secret); the token is only in
  the deploy step's env and goes to the CLI as `--token "$VERCEL_TOKEN"` (GitHub masks it in logs).
- Token absent → the plan step sets `enabled=false`: `::notice::` on previews, `::warning::` on a push to
  `main` (production was NOT deployed). Fork PRs are skipped by the job `if:`.
- Concurrency group = ref + target; `cancel-in-progress` only for previews. A production run is never
  cancelled; a newer main push waits (GitHub keeps only the newest pending run).
- Production check: `curl -L` the returned deployment URL; with `VERCEL_AUTOMATION_BYPASS_SECRET` present it
  must be 200 (else the job fails), without it the status is only logged (the deployment URL itself is
  behind Deployment Protection; only custom domains are public).
- One PR comment, marker `<!-- vercel-preview-deploy -->`, edited in place on each push (same pattern as the
  CIS UI-smoke comment); all values reach `run:` through env.
- `ui-smoke.yml` is now `on: workflow_call` (inputs `base_url`, `sha`; secrets declared optional, caller
  passes `secrets: inherit`) + `workflow_dispatch` (now also takes an optional `sha`). Inside a called
  workflow `github.event_name` / `github.event` are the CALLER's (pull_request), so every expression reads
  `inputs.*`; the job gate is `inputs.base_url != ''`.
- The `deployment_status` trigger was REMOVED rather than kept: with the Git integration off it should never
  fire, and if Vercel does post statuses for CLI deployments of a Git-connected project, keeping it would run
  the smoke twice per preview. `deployments: read` dropped with it.
- `vercel-deploy.yml` job `smoke` (`needs: deploy`, only for target preview with a URL) calls the reusable
  workflow with `base_url` = deployment URL and `sha` = PR head sha, so checkout + "find PR by sha" still work.
  The caller job grants `contents: read` + `pull-requests: write` (a called workflow cannot exceed them).
- `deploy-dev.yml`: confirmed there is no Vercel deploy left (S240 #783 removed the hook); only its comment was
  updated. Backend deploy behaviour unchanged.

## Changed
- `frontend/vercel.json` (new), `.github/workflows/vercel-deploy.yml` (new), `.github/workflows/ui-smoke.yml`
  (deployment_status → workflow_call), comment in `.github/workflows/deploy-dev.yml`, CONTEXT.md (Architecture +
  Deploy & CI), README.md (CI + Deploy).

## Tradeoffs
- A PR that touches no `frontend/**` file gets no preview at all (before: the Git integration still created a
  deployment that the Ignored Build Step then canceled — and it still counted against the cap).
- Remote build vs `vercel build` on the runner + `--prebuilt`: remote keeps the project's env vars and build
  settings as the single source and needs no `vercel pull`, at the cost of build minutes on Vercel (same as before).
- The CLI uploads the repo root (not only `frontend/`); hashed files are deduplicated across deploys, and
  `.git` / `node_modules` are ignored by the CLI by default. No root `.vercelignore` was added (where Vercel
  reads it with a Root Directory for CLI uploads was not verified).
- Cancelling a preview run mid-way stops waiting on the runner, but the deployment already created on
  Vercel keeps building and still counts — accepted (it is still far fewer deployments than before).

## Should know
- Nothing changes until the repo secret `VERCEL_TOKEN` exists. Set it BEFORE merging this PR, otherwise the
  merge to `main` gets no production deploy (the Git integration is off from that commit on) — the run
  shows a warning; re-run it or dispatch `target=production` after setting the secret.
- The PR that adds `frontend/vercel.json` already has the Git integration off for its own branch.
- The project-level Ignored Build Step (`git diff HEAD^ HEAD --quiet -- .`) is now irrelevant; it may still be
  evaluated for CLI deployments (unverified) — without `.git` in the upload the command errors, which Vercel
  treats as "build". Clearing it in the dashboard is optional and safe.
- The workflow has not run on GitHub yet (it can only run after it is on a branch with the token set);
  validated with `yaml.safe_load` + actionlint 1.7.12 with shellcheck only.
- Vercel's own PR comment / "Vercel" GitHub check disappear; look at the "Vercel deploy" job instead.
- `deploy-dev.yml` still triggers on `frontend/**` (it rebuilds the ECS image for frontend-only merges). That is
  backend deploy behaviour and was left alone; worth a separate chore.
- A PR that changes only `tests/e2e/**` (smoke specs) no longer gets a preview + smoke automatically; dispatch
  UI Smoke by hand with an existing preview/production URL.
- The smoke and the deploy post two separate PR comments (markers `ui-smoke-report` and `vercel-preview-deploy`).
