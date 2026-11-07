# AA-1024 — lane l22 (S271): repeated test / CI lessons become guards

Design: root `.tmp-session/S271/lessons-guards.json` (guards G6, G7, G8, G10, G16 and the critic's notes). One CIS PR;
copies of G10 (and the rebase refusal of G6) in AA-Booking, AA-Partner and AA-TripPlanner-Web; one root PR for the
`core.hooksPath` check in the PR opener and the lane template. No migration, no runtime code.

## Decisions
- **G6, cheapest enforceable form: a stamp, checked by the hook.** The critic showed that running the integration
  tests inside the pre-push hook hits the push timeout of the PR opener (300 s; the replay alone is ~380 files), so
  the hook never starts Postgres. `scripts/pre-push.sh --integration` runs the integration test files the diff
  touches (changed `tests/integration/test_*.py`, the tests that name a changed migration or import a changed
  `tests/integration/_*.py` helper) on `CIS_TEST_DB_PORT` or on a throwaway `initdb` cluster it starts and removes
  (never 5432), and on a pass with a clean tracked tree writes a stamp under the worktree's git dir
  (`git rev-parse --git-path aa-prepush/integration`). The key is the target list plus the blob ids at HEAD of every
  changed non-doc file: a docs-only commit keeps the stamp, any code change needs a new run. The hook's new
  `integration` check (fast and full mode, not `--ci`) blocks a push without the stamp; `AA_PREPUSH_SKIP_INTEGRATION=1`
  skips once as a visible warning. Unit test files changed by the diff were already run by the hook's fast mode, so
  the remaining hole was the hook not being installed (`core.hooksPath` empty in every checkout): the root PR makes the
  PR opener refuse a repo without it, and the install is one line per clone (`scripts/install-hooks.sh`).
- **Rebase in progress (critic "missing" item, lesson S257):** `.githooks/pre-push` refuses while `rebase-merge` /
  `rebase-apply` exists, in all four app repos. With the hook installed this covers hand pushes too.
- **G16:** `tests/integration/_replay_guard.py` holds `EXPECTED_REPLAY_FAILURES` (41 files, measured on CI's image:
  postgres:15, superuser cistest, no pgvector) and the pure `replay_problems`. conftest records every failing file and
  stops the session (`pytest.exit`, code 3) on a new failure or a stale entry; a superuser check runs first. Where
  pgvector is installed locally the stale check is off (the vector files and some after them apply there).
- **G7:** `tests/unit/test_aa1024_sql_order_stability.py` reads the id type of every table from the migrations (and a
  CREATE TABLE in the test file), AST-scans every string in `tests/integration/`, and fails on a first ORDER BY key that
  is a uuid `id`, and on `ORDER BY created_at` with nothing after it (the critic's sibling form: one transaction, one
  `now()`). Today: 0 uuid hits (the S271 flake was already fixed), 21 created_at-only hits in 18 files, frozen in
  `ALLOWED` (shrink only). Unit tests are out of scope (their SQL goes to mocks).
- **G8 scanner:** the critic showed the REFERENCES / CONSTRAINT / GRANT trigger misses half of the LATER entries
  (columns and rows), so the rule is "any migration after 304 that names a table `_undo` drops, or the catalog's
  `capabilities` column". 18 such files on main were never in LATER and the module passes; they go into a new
  `NOT_LATER` dict in the 304 module with that reason. A new file in neither list fails
  `tests/unit/test_aa1024_replay_later_list.py`, so the author decides. `REPLAY_TESTS` is the registry for more replay
  modules.
- **G8 nightly:** `.github/workflows/nightly-integration.yml` runs the whole integration suite (minus test_007, as CI)
  in one job with the modules in a seeded random order (`ci_shards.py files --shuffle SEED`, no new dependency such as
  pytest-randomly: module order is what the lessons describe, and test order inside a module stays). Schedule and
  dispatch with a seed only, never a PR check.
- **G10:** `.github/scripts/migration_refs.py`, identical in CIS, Booking, Partner and TripPlanner, reads the lines a
  PR adds and fails on `NNN_name` naming a migration of the owner whose file now has another number, or a file the PR
  removes. Called from `migration_preflight.py` (refusal 6), and from its own step in TripPlanner's Migrations job.

## Changed (vs the design)
- G6 part 3 (export_openapi version refusal) and part 4 (`pythonpath`) are not in this lane: neither was in the lane
  brief. G14 (gitleaks before push) stays with the root lane; the critic's claim that the other repos' pre-push runs
  gitleaks is wrong (none does).
- G7's second rule (text ORDER BY compared with a literal list, collation) is not built: it needs data-flow from the
  query to the assert; the created_at rule is the sibling form the critic named instead.
- G10 only flags names of this owner's migrations (exact name match), not every `NNN_name` token, so another owner's
  file and fixture names never fail.

## Tradeoffs
- The stamp trusts the lane's machine (a hand-written stamp passes). It is a guard against forgetting, not against
  intent; CI's shards stay the authority.
- The NOT_LATER reasons for the 18 existing files are generic ("on main before the guard; the module passes without
  it"); a reviewer can sharpen them when one of them is touched.
- The nightly run does not block merges; it is only useful if session start reads its last conclusion (follow-up).

## Should know
- Resumed run (S271): the first version of `--integration` started its throwaway cluster inside `$(...)`, so the
  cleanup never saw the cluster path and every run left a postgres process and its data dir behind. Fixed
  (`start_scratch_pg` sets `PG_PORT` in the calling shell); `test_throwaway_cluster_is_stopped_and_removed` (fake
  initdb / pg_ctl / psql through `AA_PREPUSH_INITDB`) is red on the old form. Checked for real on PostgreSQL 16 (initdb,
  cluster gone after the run) and the final conftest on a postgres:15 container (CI's image): the replay matches
  `EXPECTED_REPLAY_FAILURES` exactly.
- Other lanes of the wave: a CIS PR that changes `tests/integration/` or `api/migrations/` and was pushed without
  `scripts/pre-push.sh --integration` is refused by the hook once it is installed; the push can go with
  `AA_PREPUSH_SKIP_INTEGRATION=1` (a visible warning, named in the PR body). A new migration after 304 that names a
  table the 304 replay drops must be added to `LATER` or `NOT_LATER` (unit test), and a new replay failure must be
  added to `EXPECTED_REPLAY_FAILURES` with its error.
- Install once per clone: `scripts/install-hooks.sh` (core.hooksPath=.githooks; shared by every worktree of the clone).
- Lanes: `scripts/pre-push.sh --integration` after touching `tests/integration/` or `api/migrations/`; it needs
  `psycopg2`, `fakeredis`, `pytest-asyncio` in the picked Python (`pip install -r requirements.txt -r
  tests/integration/requirements-integration.txt`) and either `CIS_TEST_DB_PORT` or a local `initdb`.
- A migration that fails in the replay now stops the integration session with the file and the error. If it is
  expected (an ACPv1-era file, a pgvector file), add it to `EXPECTED_REPLAY_FAILURES` with its error.

## Review fixes (S272, lane l5-guard-review-fixes)

Minor findings left on the merged guard PRs (CIS #1360, Booking #205, Partner #241, TripPlanner #233). One PR per repo.

### Decisions
- **`migration_refs.py` decode (all four repos, still byte-identical):** `git()` reads bytes and decodes UTF-8 with
  `errors="replace"`. Only `NNN_name` tokens matter and they are ASCII, so a Latin-1 line (a fixture, a legacy CSV)
  is read with U+FFFD instead of crashing the preflight with UnicodeDecodeError. Test: a Latin-1 added line next to a
  stale reference still yields exactly that one finding (red on the old code). `migration_preflight.py` keeps its own
  `git()` (it only reads the migrations folder's names and the readiness file).
- **Replay guard, error match:** an expected failure counts only when its `EXPECTED_REPLAY_FAILURES` text is a
  substring of the first line of the error raised now; the same file failing for another reason is a problem. With
  pgvector installed locally the mismatch check is off, like the stale check (another chain of files applies there).
- **Pre-push, conftest smoke stamp:** `SMOKE_INTEGRATION=(tests/integration/test_aa1024_replay_smoke.py)`, a new
  one-test file (SELECT through `db_conn`, `shared.schema_versions` not empty). A changed `conftest.py`, or a
  `tests/integration/_*.py` helper that conftest imports (`_replay_guard.py`, `_constants.py`, read from conftest's
  import lines), now puts the smoke file in the stamp's target list instead of printing a WARN. The smoke file is a
  "direct" target, so the cap never drops it. A dedicated file rather than an existing cheap test: no service import,
  so it only fails when conftest / the replay breaks. CI's shards still run everything.
- **Nightly red visibility: one GitHub issue labelled `nightly-red`.** Job `report` (needs the suite, `!cancelled()`,
  `permissions: issues: write` only, no checkout, `gh` with the run token) opens the issue on the first red night
  (creating the label), comments on it on each further red night (seed + run link), and closes it on the next green
  run. Chosen over writing where the ops probes read (the `nightly_probes` KPI reads `SCRIPT verdict` lines of ECS
  run-script probes in CloudWatch Logs): that needs AWS OIDC credentials in this workflow and a new log stream for a
  GitHub signal. An issue is visible in the repo, needs no new secret, and `gh issue list --label nightly-red` is a
  one-line probe for session start (lesson S255: a non-required red run nobody reads).
- **Migration preflight scope (item 5): no gap found, now pinned.** The scope step does not exit the job: a PR that
  touches no migration and no readiness key only skips the Infra tools and the classify replay; "Preflight verdict"
  has no `if:` and `migration_preflight.main()` always calls `migration_refs.check()`. The workflow has no path filter.
  A renumber PR always touches the migrations folder (a rename = delete + add). The misleading ci.yml comment
  ("exits at once") is rewritten in the three repos, and `test_migration_refs.py` (identical in the three) gains a
  doc-only PR naming the old number (refused) and a shape test: the verdict step has no `if:`, the job's only `if:` is
  the event filter, no `paths:` in `on:`.
- **TripPlanner number check (item 6): built, not deferred.** `.github/scripts/migration_numbers.py` + a step in the
  required Migrations job (pull_request only): an added tp-NNN must be main max + 1 (then + 2, ...) on the main fetched
  at run time (a re-run after another PR merged re-checks it), no number twice in the head, and no other open PR may
  add the same number under another name: against an older PR (lower number) it is an error, against a newer one a
  warning. Same path in both PRs = the same file (a stacked PR), not a collision. The open-PR list comes from
  `gh pr list --json number,files` (the job now asks for `pull-requests: read`; deploy-lambdas.yml's `ci` job grants
  it, otherwise the workflow_call fails at startup). When the list is unreadable the step says so and keeps the two
  other rules.

### Changed (vs the review findings)
- Item 6 also refuses a lower (reserved) number and a gap; TripPlanner has no `migration:out-of-order` label, so a
  reserved number is renumbered (as the lane rules say).

### Tradeoffs
- The open-PR rule only fires when the newer PR's CI runs while the older PR is open, or on a re-run after it merged
  (then "taken on main"). Without a merge queue or a "branch up to date" rule, two PRs green at the same time can still
  both merge; the push to main then goes red (migration lint MIG-3), which blocks the Lambda deploy. Closing that fully
  is a branch protection change (strict status checks, or a merge queue) on TripPlanner, a follow-up for the tech lead.
- The nightly issue can be closed by a green manual replay with a seed; that is a real green run of the same suite.

### Should know
- The smoke file runs in CI's integration shards too (one cheap test). It has no recorded duration yet; ci_shards
  uses the median.
- `scripts/pre-push.sh --integration` after a conftest / `_replay_guard.py` change runs the full replay once (the
  smoke file's session), about the cost of any single integration test file.
