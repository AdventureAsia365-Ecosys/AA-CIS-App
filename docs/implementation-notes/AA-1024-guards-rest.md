# AA-1024: the rest of the lessons-to-guards design, CIS part (lane l8-lessons-guards-rest, S272)

Design: root repo `docs/audits/2026-11-07-S271-lessons-to-guards/` (guards G1-G22). The root notes
`docs/implementation-notes/AA-1024-guards-rest.md` (root repo) hold the table of every guard; this file covers CIS.

## Decisions

- **G17 wave stop flags enforced** (`scripts/aa645/wave_report.py`): the report computed `stop_conditions` and always
  exited 0. It now also reads `shared.job` (queued / running rows whose payload `tour_id` or `published_tour_id` is a
  tour of the wave) and gives each flag as `true` / `false`, or `pending` while such jobs exist. Exit 3 = a flag is
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
- G17 next-wave refusal (review fix pass): `POST /admin/waves` recomputes the newest wave's stop flags (no stored
  report, so no migration) and answers 409 while one is true or pending, or when they cannot be computed; a
  `stop_override` of at least 20 characters (the tech lead's written approval, quoted) lets it through and is kept at
  the top of the new wave's notes, with the flags it overrode (log `rerun_wave_stop_override`).
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
  workflow are needed. A failed list raises (the gate fails closed); `DEPLOY_GATE_ECSX=off` turns it off (both test
  conftests set it).

## Tradeoffs

- The wave start runs the code checks of every tour of the previous wave (both versions) in a worker thread: a few
  seconds for a country wave, once per wave start. Cheaper than a stored report that could go stale.
- Heartbeat age 120 s = four missed beats: a script killed by OOM stops blocking two minutes later; a live script whose
  S3 put fails four times in a row would not block (accepted: the put is the same call that writes its result).
- A gate that cannot compute refuses (fail closed) instead of passing: the override is the way through.
- A wave with no A1 version and no active job is "not applicable" (else `not_clearly_better` is true on an empty
  table and every wave after an atomize-only wave would be refused).

- Exit 5 for "pending" instead of 0: a script or person waiting on the report sees that it is not final.

## Should know

- The wave report's new SQL reads only `kind`, `status` and the payload tour keys of `shared.job` (cis_app has SELECT).
- `PR title` is not a required check in CIS yet; making it required is a ruleset change (tech lead).
