# AA-1097 (lane l15, S279): complexity and file-size ratchets

Parts 1-3 of AA-1097 in this repo. The same tool ships to AA-Booking, AA-Partner and AA-TripPlanner-Web (one PR per
repo); part 4 (splitting `api/routers/admin_pipeline.py`) is a separate lane after these guards merge.

## Decisions

- **One tool, two ratchets, one baseline.** `.github/scripts/code_health.py` (standard library only) measures and
  compares; `.github/code-health.json` holds the limits, scope and exemptions (each with a reason);
  `.github/code-health-baseline.json` holds today's offenders. The same script and test file are vendored in the four
  app repos, only the config and baseline differ.
- **Complexity limit 10 for Python and TypeScript.** Measured on 13/11/2026 (product code, tests excluded):
  - Python (ruff C901): 10,938 functions, p90=6, p95=9, p97=11, p99=16, max 71. 10 is just above p95 and is the
    mccabe default. 343 functions over the limit go in the baseline.
  - TypeScript / JavaScript under `frontend/` (ESLint `complexity`, classic variant): 10,356 functions, p90=4, p95=7,
    p97=10, p99=19, max 82. 10 is p97 and keeps one number for both languages. 304 functions in the baseline.
- **File size: 800 lines, tests included.** A 1,200-line test file is as hard to edit as a 1,200-line module, so test
  files count; fixtures, snapshots, generated files (`*.generated.ts`), migrations and vendored agent skills are exempt.
  Extensions: `.py .ts .tsx .js .jsx .mjs .cjs`. 2,677 files measured, 78 over 800 in the baseline (largest:
  `api/routers/admin_pipeline.py`, 5,813 lines).
- **Complexity covers product code only** (tests, conftest and migrations excluded): the limit was chosen from product
  code, and a test's branching is usually a table of cases.
- **Measuring tools are the ones the repo already has.** Python: ruff 0.16.9 (`--isolated`, only C901, threshold 0 so
  every function reports its score), pinned in the Lint job and `requirements-dev.txt`. TypeScript: the frontend's own
  ESLint 9 + `@typescript-eslint/parser` from `frontend/node_modules`; `.github/scripts/code_health_eslint.mjs` runs
  only the core `complexity` rule (wrapped so every function reports its score and node), with the frontend's
  `eslint.config.mjs` ignored so the score depends on the code alone. No new paid tool, no new npm dependency.
- **Function identity = `<path>::<qualified name>`.** Python: `Class.method`, `outer.inner` from `ast`.
  TypeScript: enclosing class / function names joined by "."; an anonymous function is named after its variable,
  property, assignment target, JSX attribute (`Page.onClick`) or the call it is an argument of (`Page.useEffect()`).
  A repeated name in one file gets `#2`, `#3` in source order.
- **The baseline only shrinks, and stays exact.** `check` fails on: a new over-limit function / file (NEW), a baseline
  entry that got worse (WORSE), an entry that is gone or back under the limit (STALE), an entry that improved but still
  sits over the limit (TIGHT). `update` fixes the last two by removing / lowering entries and never adds or raises.
  Against the base commit (`--base`, CI passes the PR or merge-queue base) an added entry, a raised value or a raised
  limit fails, so regenerating the baseline cannot hide new code.
- **Moves and splits are allowed.** A function moved to another file keeps its entry under its new path (same
  qualified name, score not higher; one removed entry pays for one move). A baseline file split into parts may list
  parts still over 800 while their total stays within the removed file's lines. This keeps the coming
  `admin_pipeline.py` split possible without refactoring every handler at once.
- **CI placement: existing required jobs.** Python complexity + file size: a step in **Lint** (always runs, no
  node_modules). TypeScript complexity: a step in **Frontend Lint** (has node_modules; skipped on a PR that does not
  touch the frontend, runs on every queue group). No branch-protection change.
- **Pre-push**: new check id `code_health` whenever code changed (`--kind python,files`, plus `typescript` when
  `frontend/` or the ratchet files changed, or `--full`). A missing ruff / `frontend/node_modules` is a visible SKIP
  outside `--ci`.

## Changed

- Nothing beyond the issue: no file was refactored in this lane (the split is part 4).

## Tradeoffs

- Failing on an improvement (TIGHT / STALE) costs a one-line `update` but keeps the baseline equal to the code, like
  the ESLint "suppressions file is pruned" step. Without it the baseline would drift and hide regressions up to the
  old score.
- Duplicate-name ordinals (`#2`) shift when a same-named function is inserted earlier in the file; that shows as one
  STALE + one NEW pair. Rare, and the message says what to do.
- The limits are dev-tooling settings in a committed config, not app runtime settings: CI cannot read the admin
  settings table, and a limit change is a reviewed PR that the base check refuses to raise.
- ESLint counts optional chaining (`a?.b`) and default parameters as branches, so React components score high; the
  limit is the same number but measured on that distribution (p97).

## Should know

- Run locally: `python3 .github/scripts/code_health.py check [--kind python,typescript,files] [--base origin/main]`;
  `report` prints today's distribution; `init` is for first adoption only.
- New files are checked before their first commit (`git ls-files --others --exclude-standard`).
- Tests: `tests/unit/test_aa1097_code_health.py` (unittest style; the end-to-end Python case needs ruff, the TypeScript
  names case needs `frontend/node_modules`, both skip otherwise).
