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
- Install once per clone: `scripts/install-hooks.sh` (core.hooksPath=.githooks; shared by every worktree of the clone).
- Lanes: `scripts/pre-push.sh --integration` after touching `tests/integration/` or `api/migrations/`; it needs
  `psycopg2`, `fakeredis`, `pytest-asyncio` in the picked Python (`pip install -r requirements.txt -r
  tests/integration/requirements-integration.txt`) and either `CIS_TEST_DB_PORT` or a local `initdb`.
- A migration that fails in the replay now stops the integration session with the file and the error. If it is
  expected (an ACPv1-era file, a pgvector file), add it to `EXPECTED_REPLAY_FAILURES` with its error.
