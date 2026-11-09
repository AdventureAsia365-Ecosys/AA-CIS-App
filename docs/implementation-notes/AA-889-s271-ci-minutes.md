# AA-889 (merge queue) follow-up, S271: cut GitHub Actions minutes without weakening the queue

Lane l20-ci-minutes, session S271 (06/11/2026). Five PRs: AA-CIS-App (this file), AA-Partner, AA-Booking,
AA-CIS-Infra, root AA-Ecosys. Tech lead approved the change on 06/11 (CI/workflow change, a risky PR class: reviewed
before merge).

## Measured before (05-06/11/2026, billed = each job rounded up to the minute)

| Repo | Workflow | Runs | Billed min | Avg / run |
|---|---|---|---|---|
| CIS | CI pull_request | 55 | 1904 | 34.6 |
| CIS | CI merge_group | 24 | 830 | 34.6 |
| CIS | CI push (main) | 23 | 763 | 33.2 |
| CIS | Visual pull_request | 56 | 71 | 1.3 |
| Partner | CI pull_request / merge_group / push | 49 / 19 / 19 | 976 / 380 / 362 | 19.9 / 20.0 / 19.1 |
| Booking | CI pull_request / merge_group / push | 49 / 22 / 20 | 582 / 267 / 219 | 11.9 / 12.1 / 10.9 |
| Booking | Visual pull_request | 34 | 107 | 3.1 |
| Infra | Migration Copy Guard pull_request | 40 | 190 | 4.8 |
| Infra | Secret Scan (full history) push | 33 | 84 | 2.5 |
| Root | Secret Scan (full history) push | 57 | 516 | 9.1 |

The CIS and Booking listings hit the 600-run page cap, so their window is a little shorter than two days; the ratios
hold. A merged CIS PR cost about 2.3 PR runs + 1 queue run + 1 main run of CI = ~147 billed minutes.

## Decisions

- **No CI run on push to main** (CIS, Partner, Booking). Every main commit is a merge-queue group commit
  (`gh-readonly-queue/main/*`) whose tree passed the full run; the push run re-tested the same tree. Checked first:
  Deploy Dev, Deploy Admin Dev, Vercel Deploy, Deploy Web Dev, release-please, migration-sync-wake, kit-sync and
  llm-core-sync all trigger on `push` themselves; no workflow uses `workflow_run` on CI or reads CI runs on main
  (`schema_gate.py` reads AA-CIS-Infra Migration Apply runs, not CI). CIS also dropped `push` on `feature/**`
  (branches get a PR run). `workflow_dispatch` added: a dispatch on a PR branch runs every job on its head.
- **Light pull_request run, full queue run.** On pull_request: Lint, Security Audit, Unit Tests (3 shards),
  Migration preflight, Frontend Lint (+ the kit checks and `tsc --noEmit` of Frontend Build). On merge_group and
  dispatch: every job, no path filter. Heavy jobs on a PR only in their area (`.github/scripts/ci_scope.py`):
  migrations or integration tests -> Integration Tests; Dockerfile / requirements / .trivyignore -> Docker Build
  Check. Frontend Build (next build) and Visual never run on a PR.
- **Required checks still report on every PR.** Gated jobs are skipped by a job-level `if`, which reports
  "skipped" = passed for a required check; no required check is left pending. The aggregators (Unit Tests,
  Integration Tests) keep `!cancelled()` and use the same condition as their shards, so a failed shard still turns
  them red. Visual's `pr` job is skipped on pull_request with no `needs` (no failure can hide behind the skip).
- **Fail closed.** The scope step (`continue-on-error: true`) writes `true` for every output on any non-PR event,
  when the diff cannot be read or is empty, and when the PR touches `.github/`; every gate tests `!= 'false'`, so a
  broken scope (empty outputs) runs the job.
- **Scope lives in an existing job** (CIS "Quality Paths (report)", Partner/Booking "Lint") to avoid a new
  billed minute per run.
- **"Vendored CIS copies" (Partner, Booking) stays its own non-required job.** Folding it into Lint was tried and
  reverted: it compares with CIS main, which the PR does not control (Partner main is red on it today, after a CIS
  change to `frontend/lib/sameOrigin.ts`), so inside the required Lint a CIS change would block every PR and the
  queue. It costs 1 billed minute a run.
- **Docs-only PRs** (every path under `docs/` or `*.md`) skip the test jobs on the PR only; the queue still runs
  them (some unit tests read docs/, e.g. the docs reader and legal register tests).
- **Not changed:** integration shard counts (2 CIS shards would start shard 2 at a file that was never a shard start:
  a contiguous-split risk for ~2 billed minutes on the queue run only), unit shard count (PR critical path), required
  check names (no branch-protection or ruleset change needed), deploy targets, secrets, OIDC, Terraform.
- **Root + Infra history scan:** a push scans only its `before..after` range (same options, octopus check and UTF-16
  pass); full history stays nightly and on dispatch, and runs for a push whose range cannot be used (new branch,
  force-push, unknown before). Push runs get their own concurrency group and are never cancelled (an incremental
  run that is cancelled would leave its range unscanned).
- **Infra Migration Copy Guard:** 5 jobs -> 4. The scope is a step of `guard` ("Copies match owner main"); lint,
  retention-rows and classify need `guard`. Required check names unchanged. The DM-7.1 retention rows stay their own
  non-required job (review S271): folding them into `guard` made a root-repo registry gap block the required check
  and with it the copy bot's merge check (`db/migration_sync.py merge-check` reads only ruleset contexts), a gating
  change outside a CI-minutes PR. Guarded by `test_retention_rows_stay_out_of_the_required_check`.

## Changed (vs the lane brief)

- Unit shards 3 -> 2 not done: same billed minutes (~10-12 either way), one minute slower PR.
- Merging Lint + Security Audit not done: it would rename a required check (branch protection change) for 1 minute.
- The Visual PR comment step is removed with the per-PR run (it only ran on pull_request).

## Tradeoffs

- A heavy failure (integration test, Docker build, next build, visual) is now found in the merge queue: the group
  fails and the PR leaves the queue (~22 min on CIS) instead of failing on the PR. Lanes already run the integration
  tests of touched modules locally before opening a PR (lane brief), migrations and integration-test edits still run
  integration on the PR, and image-input edits still run the Docker build on the PR.
- Frontend PRs lose the per-PR visual screenshots and PR comment; the queue run keeps the screenshots as an artifact
  (`visual-cis-pr-<run id>`), and the deployed visual run after the production deploy is unchanged.
- Docs-only PRs can break a docs-reading unit test unseen until the queue (rare; caught before main).
- Lint, retention-rows and classify in the Infra copy guard start ~30 s later (after `guard`).
- Infra: keeping retention-rows as its own job costs one short job on migration-path PRs (4 jobs, not 3); the
  required-check gating stays exactly as before.

## Estimate after (billed minutes)

| Repo | Per PR run before -> after | Per merged PR before -> after | Two-day CI total before -> after |
|---|---|---|---|
| CIS | 34.6 -> ~19 (backend), ~21 (frontend), ~5 (docs) | ~147 -> ~79 | ~3,500 -> ~1,900 (-46%) |
| Partner | 19.9 -> ~7-9 | ~91 -> ~40 | ~1,720 -> ~770 (-55%) |
| Booking | 11.9 -> ~5-7 | ~50 -> ~25 | ~1,070 -> ~540 (-50%) |
| Infra copy guard + history | 4.8 -> ~4; 2.5 -> 1 | | ~275 -> ~185 (-33%) |
| Root history scan | 9.1 -> 1 per push | | 516 -> ~60 (-88%; root total 750 -> ~290) |

Across the five repos' CI workflows: ~7,300 -> ~3,650 billed minutes in two days (about -50%). CIS stays at -46%
because its three unit shards (~12 billed minutes) are the PR's core check; the remaining big levers are fewer PR
pushes per PR (2.3 PR runs per merged PR) and the CodeQL "dynamic" runs (repository setting, not a workflow file).

## Should know

- After merge, nothing has to be changed in branch protection or rulesets: every required check name is the same.
- The first PR after merge shows the skipped jobs as "skipped" checks; that is the intended signal.
- `ci_scope.py` is the same file in CIS, Partner and Booking (byte copy); change all three together.
- Tests: `tests/unit/test_s271_ci_scope.py` evaluates every job condition of ci.yml for each event (merge_group and
  dispatch run every job; a failed scope runs every gated job; docs-only, backend, migration and frontend PRs run
  exactly the expected set). `test_aa1032_ci_shards.py` and `test_aa1008_h3_required_checks.py` updated.
- The history-scan change was exercised locally with gitleaks 8.30.1 (a docker shim) on scratch repos: clean push
  range green with an old secret outside it, the full run red on that secret, a new secret in the range red,
  force-push / new branch / unknown before -> full history.
