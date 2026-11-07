# AA-1024: the rest of the lessons-to-guards design, CIS part (lane l8-lessons-guards-rest, S272)

Design: root repo `docs/audits/2026-11-07-S271-lessons-to-guards/` (guards G1-G22). The root notes
`docs/implementation-notes/AA-1024-guards-rest.md` (root repo) hold the table of every guard; this file covers CIS.

## Decisions

- **G17 wave stop flags enforced** (`scripts/aa645/wave_report.py`): the report computed `stop_conditions` and always
  exited 0. It now also reads `shared.job` (queued / running rows whose payload names a tour of the wave: `tour_id`, a
  `tour_ids` list, or a t2_rewrite `published_tour_id` mapped through `published_tours.id`) and gives each flag as `true` / `false`, or `pending` while such jobs exist. Exit 3 = a flag is
  true, 5 = pending (run again after the jobs drain), 0 = none. `stop_conditions` keeps its boolean shape (readers of
  the JSON keep working); `stop_state`, `active_jobs` and `exit_code` are new keys.
- **G11 PR text guard**: `.github/workflows/pr-title.yml` (new in CIS; the same file as Booking, Partner, root): no issue
  id in the title, no closing keyword in the body, no issue id the `Refs` line does not name (an id the Refs line
  names may appear anywhere; an id inside a file path is ignored; bots skip this rule by login, never by `user.type`,
  since the orchestrator's App is a Bot too).
- **G15 workflow lint** (review fix pass): `.github/workflows/workflow-lint.yml` + `.github/scripts/workflow_lint.py`
  (the AA-CIS-Infra script, vendored; one copy in every repo) + its unittest file: pinned actionlint 1.7.12, then WL1
  no sensitive `vars.*`, WL2 Playwright on `ubuntu-24.04`, WL3 `npm ci` with a git-tracked lockfile. actionlint found
  two real errors here, fixed: `visual.yml` and `ui-smoke.yml` read `secrets.E2E_MAIL_ROLE_ARN` but their
  `workflow_call` did not declare it (now declared, optional). `kit-consumers.yml` runs `npm ci` in a matrix of other
  repos' apps: listed as unchecked, not a finding. It answers `merge_group` with
  success, like the other queue repos.
- **G6 (3) pinned openapi**: `scripts/export_openapi.py` refuses (exit 2) before importing the app when FastAPI or
  Pydantic differ from the `requirements.txt` pins; `--allow-unpinned` only shows a diff.
- **G6 (4)**: `pythonpath = ["."]` in `[tool.pytest.ini_options]`.

## Changed (vs the design)

- G17: every flag is `pending` while any job of the wave's tours is active (the design named "the kinds it depends
  on"): a job of any kind can still move a tour's outcome, so a per-kind map would only hide a pending state.
- G17 (review pass 3): the job lookup covers the wave's EXPECTED set, not only the tours with an A1 version in the
  window: the `--tour-ids-file` ids, the window's tours and, with `--country`, the country's active raw tours
  (`COUNTRY_TOURS_SQL`). A tour whose prefetch / ingest / rewrite is still queued has no version yet; reading only the
  window gave `active=[]` and exit 3 (STOP) on an empty table, or final flags on 10 of 50 tours. A named set with no
  version in the window and no active job reports one `no_a1_version_in_window` row (pending, exit 5), never a STOP.
  A window-only run (no ids, no country) still reads only the window's tours: the runbook always passes the expected
  set. Later, `shared.rerun_wave.expected_tour_ids` (`--wave`) can feed the same set.
- G17 next-wave refusal: STRIPPED in review fix pass 2 (S272). The first version made `POST /admin/waves` recompute
  the newest wave's stop flags inside the request and answer 409. The reviewer measured `_compute_tours` (the S1 code
  checks on the before and after page of every tour; most of the time in `check_numbers` ->
  `find_novel_word_numbers` -> `hygiene.fold`): 16.3 s for 50 dense tours and 74.6 s for 250 on a laptop, before the
  DB reads, on a 0.5 vCPU api task behind the API Gateway's 29 s integration timeout. A 504 still inserted the wave and
  a retry then read that empty wave as "previous" (no versions, no jobs: not applicable) and inserted a second open
  wave with the same expected set. `POST /admin/waves` is back to its main-branch code (no `stop_override`, no openapi
  change); the CLI keeps exit 3 / 5. Follow-up (not built): `wave_report.py --wave <id>` stores its `stop_state`,
  `exit_code` and `computed_at` under `shared.rerun_wave.last_check.stop_gate` (no migration: `last_check` is jsonb;
  `wave_report.check()` must then keep that key when it rewrites `last_check`), and `POST /admin/waves` reads the
  stored value: never computed, or computed while jobs were active = pending = 409 unless overridden; plus
  idempotency on (name, expected set) for a retried create. Tested by a unit test that `create_wave` never calls the
  quality read.
- To share one definition, the quality read moved from `scripts/aa645/wave_report.py` to `services/waves/quality.py`
  (the image carries `services/`, not `scripts/`: AA-725 keeps scripts out of it). The gate code sets moved to
  `services/quality/s1_gates.py` and figure coverage to `services/quality/s1_coverage.py`; `scripts/aa645/gates.py`,
  `s1_screen.py` and `wave_report.py` re-export the old names, so the other aa645 scripts and tests are unchanged.
- G14: `scripts/pre-push.sh` now runs gitleaks v8.30.1 on `<base>..<head>` with the BASE's `.gitleaks.toml` /
  `.gitleaksignore` in every mode but `--ci` (CI has its own Secret Scan). Binary: `AA_GITLEAKS`, PATH, or the
  workspace copy `.tmp-session/gitleaks-8301/gitleaks` found by walking up from the checkout. A missing or
  other-version binary blocks; `AA_SKIP_GITLEAKS=1` skips once with a WARN (same switch as the PR opener).

- G3 (c) deploy gate reads the ecsx heartbeat (review fix pass): `shared.jobs.worker.deploy_gate_status` (the
  `/admin/job-runner/deploy-gate` answer the CI guard reads, and the Platform Apps card) lists `scripts/out/` on the
  bronze bucket and adds a `blocked_by` line per `<run>.txt.running` younger than 120 s with no `.done` next to it
  (`exec_scripts` in the answer; `shared/jobs/exec_heartbeat.py`). The API reads it, not CI: the task role already has
  `s3:ListBucket` (AA-CIS-Infra `modules/ecs` ecs_task_policy), so no Infra grant and no AWS step in the deploy
  workflow are needed. A failed list is one `blocked_by` line "ECS exec heartbeat unreadable" (fail closed:
  `safe_to_deploy` false, the CI guard blocks) plus `exec_scripts: null` and `exec_scripts_error`; the rest of the
  answer stays, so the Platform Apps card keeps its wave and job counts and lists the error as a problem (review fix
  pass 2: it used to raise and blank the whole card). `DEPLOY_GATE_ECSX=off` turns it off (both test conftests set it).
- G3 (c) re-check before the roll (review fix pass 2): the preflight guard runs in parallel with the image build, so a
  script started during the build was not seen. `deploy-dev.yml` runs `deploy_job_guard.sh` again as the step right
  before `Deploy api` (same secret, same `override_running_jobs`).

## Tradeoffs

- (Removed: the in-request wave gate. Its cost, measured by the reviewer, was 16-75 s per wave start, not "a few
  seconds".)
- Heartbeat age 120 s = four missed beats: a script killed by OOM stops blocking two minutes later; a live script whose
  S3 put fails four times in a row would not block (accepted: the put is the same call that writes its result).
- A gate that cannot compute refuses (fail closed) instead of passing: the override is the way through.
- A wave with no A1 version and no active job is "not applicable" (else `not_clearly_better` is true on an empty
  table and every wave after an atomize-only wave would be refused).

- Exit 5 for "pending" instead of 0: a script or person waiting on the report sees that it is not final.

## Should know

- The wave report's new SQL reads only `kind`, `status` and the payload tour keys of `shared.job` (cis_app has SELECT).
- `PR title` is not a required check in CIS yet; making it required is a ruleset change (tech lead).
