# AA-1032 part 3 — faster CI: unit and integration shards (AA-CIS-App)

Lane `ship-ci-speed` (S263). Same change in AA-Partner (its own notes file).

## Measured before (10 green `main` runs of CI, 01/11/2026)

| Job | Job time | Test step | Notes |
|---|---|---|---|
| Unit Tests | 3.5-5.4 min (~4.8 typical) | ~200-310 s | 4 xdist workers; ~46 s of it is each worker importing all 667 files before the first test |
| Integration Tests | 3.0-4.6 min (~4.1 typical) | ~200-235 s | 2014 tests in one process, ~20 s schema replay first |
| Docker Build Check | ~2.3-2.8 min | | the next longest job |

The run's wall time was the Unit Tests job (~5 min); sharding only the integration suite would not have moved it, so
both suites are sharded.

## Decisions

- **Matrix shards behind the same required check names.** `Unit shard i/3` and `Integration shard i/4` run in
  parallel; `Unit Tests` and `Integration Tests` are aggregate jobs (`needs:` the matrix, `if: ${{ !cancelled() }}`)
  whose first step fails unless `needs.<matrix>.result == success`. Without `!cancelled()` a failed shard would make the
  aggregate "skipped", which branch protection counts as passed. Branch protection and the merge queue need no change.
- **Split by test file, by our own stdlib script** (`.github/scripts/ci_shards.py`), not pytest-split: no new
  dependency, deterministic (a pure function of the files on disk, the durations file and N, computed by each shard
  from the same checkout), and it can check that the split is a partition (every file in exactly one shard).
- **Unit: longest-first by file, 3 shards x 4 workers.** Weight = recorded seconds / 4 workers + 0.07 s per file
  (every xdist worker imports all of its shard's files; measured ~0.07 s/file on a runner). Unit files are already
  independent (xdist `--dist loadfile` puts them on arbitrary workers today).
- **Integration: consecutive ranges (`--contiguous`), 4 shards.** The modules are NOT independent. A longest-first
  split was tried first and broke 9 modules (183 errors, 6 failures) in one shard: `test_aa712_migration_226` drops
  schema `catalog` in its setup and `test_aa712_migration_228` (next in path order) re-creates it; every later module
  that needs `catalog` relied on that pair running back to back. Consecutive ranges (exact linear partition on the
  same weights) keep each module's predecessors except at the start of a shard (fresh replay). Verified locally on
  four fresh clusters: 756+1s / 458 / 622+2s / 178 = 2014 passed, 3 skipped, identical to the unsharded run.
- **Coverage.** Each unit shard writes `coverage-shards/.coverage.unit-N` (`COVERAGE_FILE`, `--cov-report=`); the
  aggregate runs `coverage combine`, `coverage report --show-missing`, `coverage xml`, then the unchanged ratchet. On a
  two-shard split of 6 files the combined XML matched a single run line for line (12210 / 60366 lines).
- **Q0 red on base** moved into the aggregate `Unit Tests` job unchanged (it needs the deps and `fetch-depth: 2`).
- **Durations.** `.github/test-durations/{unit,integration}.json` were built from the timestamps of main run 41304349872.
  Shards write junit (`-o junit_family=xunit1`, which carries the `file` attribute); the aggregate prints each shard's
  real seconds in the step summary and uploads `test-durations-<suite>` (the refreshed JSON). A stale file only
  unbalances shards; a file without a duration weighs the median.

## Changed

- `.github/workflows/ci.yml`: `test-unit` -> `test-unit-shard` (matrix) + `test-unit` (aggregate);
  `test-integration` -> `test-integration-shard` (matrix, own Postgres + Redis services each) + `test-integration`.
- New `.github/scripts/ci_shards.py`, `.github/test-durations/*.json`, `tests/unit/test_aa1032_ci_shards.py`.

## Tradeoffs

- More runner jobs (7 test jobs instead of 2) for less wall time; Actions minutes are free on public repos.
- Each integration shard pays the container start (~22 s) and the schema replay (~20 s) again; with 4 shards that
  overhead is still well under the time saved. More than 4 shards gives little: the largest single module is ~42 s.
- `--contiguous` balances a little worse than longest-first (modules cannot move), accepted for correctness.
- `scripts/pre-push.sh --full` still runs the whole unit suite in one process (same coverage, slower); not touched.

## Should know

- A shard that goes red on `main` right at its first files while the unsharded order passed means a module leans on
  an earlier module's leftovers: fix that module's setup (create what it needs), do not regroup files by hand.
- To rebalance: download `test-durations-unit` / `test-durations-integration` from a green main run and commit them
  over `.github/test-durations/`.
- `tests/integration/pytest.ini` makes `tests/integration` pytest's rootdir for that suite, so its junit `file`
  attribute is `test_x.py`, not `tests/integration/test_x.py`. The integration aggregate therefore runs
  `ci_shards.py durations --prefix tests/integration/`; without it the refreshed file would match no discovered file
  and the split would quietly become a count split with moved boundaries (review finding, fix pass). Guards:
  `test_the_integration_durations_refresh_uses_the_names_discover_returns` and
  `test_committed_durations_name_real_test_files` (>= 80% of committed keys must be real files). The rootdir itself
  is left as it is.
- To change a shard count: the matrix list, `UNIT_SHARDS` / `INTEGRATION_SHARDS` (both jobs for unit) and the `/N` in
  the job name together; `test_aa1032_ci_shards.py` checks they agree.
