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
  id in the title, no closing keyword in the body, issue ids only on the `Refs` line. It answers `merge_group` with
  success, like the other queue repos.
- **G6 (3) pinned openapi**: `scripts/export_openapi.py` refuses (exit 2) before importing the app when FastAPI or
  Pydantic differ from the `requirements.txt` pins; `--allow-unpinned` only shows a diff.
- **G6 (4)**: `pythonpath = ["."]` in `[tool.pytest.ini_options]`.

## Changed (vs the design)

- G17: every flag is `pending` while any job of the wave's tours is active (the design named "the kinds it depends
  on"): a job of any kind can still move a tour's outcome, so a per-kind map would only hide a pending state.
- G17: the next-wave refusal is not built: the wave start (`POST /admin/waves`) has no record of the last report, so
  it needs a column or table (a migration) first. Until then the wave runbook (root `skill/aa-wave-rerun`) says the
  next wave starts only after exit 0 or the tech lead's written approval quoted in the wave report.
- G14 (gitleaks in the CIS pre-push) is not in this PR: `scripts/pre-push.sh` is being changed by the S272 GIT_DIR fix
  in its own PR; it follows after that merges.

## Tradeoffs

- Exit 5 for "pending" instead of 0: a script or person waiting on the report sees that it is not final.

## Should know

- The wave report's new SQL reads only `kind`, `status` and the payload tour keys of `shared.job` (cis_app has SELECT).
- `PR title` is not a required check in CIS yet; making it required is a ruleset change (tech lead).
