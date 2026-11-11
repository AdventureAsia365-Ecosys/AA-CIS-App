# AA-1084 (lane l21): balanced CIS unit shards, fresh durations, 4 shards

Part 1 of AA-1084 in AA-CIS-App: the CI unit shards. The ship-tool part (merge main into code branches after a
migration squash) is another lane in the root repo.

## Measured before (merge-queue run of 11/11/2026, 3 unit shards)

| Shard | Job | pytest session | Recorded test seconds (4 workers) | Longest file |
|---|---|---|---|---|
| 1/3 | 309 s | 285 s | 503 s | `test_aa1002_s3_routes_snapshot.py` 145 s |
| 2/3 | 359 s | 334 s | 522 s | `test_aa926_lint.py` 124 s + `test_aa790_fact_invariant.py` 86 s |
| 3/3 | 263 s | 236 s | 402 s | 38 s |

- The committed `unit.json` (06/11) knew 663 of 835 files; the 663 it knew had doubled since (547 s -> ~1040 s).
- About 130-140 s of every shard is outside the recorded test seconds: each xdist worker collects all of its shard's
  files (~0.45-0.5 s per file on the runner; the split assumed 0.07 s).
- Two slow files were most of the growth: the AA-1002 route snapshot (16 cases, each re-hashed every stage's prompt
  source because the unit conftest clears that cache per test, and started a ledger write per attempt) and the
  AA-926 lint test (two full repo scans; the AA-1047 policy lint cut the whole file into lines for every comparison).

## Decisions

- **Fix the slow paths, then rebalance.** Raising the shard count alone gives ~3.9 min (model below); the root causes
  give most of the gain and also speed every other gateway test:
  - `shared/llm_client/prompt_registry.py`: `_module_names` memoised on (path, root, file TEXT) with an LRU of 512.
    Keyed on the text, not the mtime: a test that rewrites a file in place gets a fresh parse. Production behaviour
    is unchanged (the per-stage cache already made it one hash per process).
  - `services/quality/policy_lint.py`: `ast.get_source_segment` only for a comparison that has a band literal (it
    splits the whole source per call: 32k calls, 15 s of a 52 s lint run). Same findings.
  - `tests/unit/test_aa926_lint.py`: one repository scan per module (fixture) instead of two.
  - `tests/unit/test_aa1002_s3_routes_snapshot.py`: autouse fixture stubs `prompt_registry.version_for` and
    `call_log._in_background` (neither feeds the recorded provider calls; the golden still matches).
  - Local, no database reachable (as in CI): route snapshot 87 s -> 7.5 s, lint test 59 -> 18 s, prompt registry gate
    20 -> 6 s, policy registry 15 -> 5 s, OpenAI last resort 16 -> 9 s.
- **Cost model in `ci_shards.plan`**: a shard costs files x overhead + max(seconds / workers, longest file). With
  one worker that is the old sum (a test pins it). The plan and the bot's predictions now price a 145 s file at 145 s,
  not 36 s.
- **`files --longest-first` + `--no-loadscope-reorder`** for the unit shards: xdist's default orders files by test
  COUNT, so a two-minute file with few tests could start last on a busy worker (shard 2/3 above: one worker ~194 s
  while the others ran ~100-130 s). Longest first, each worker pulling the next file when it is nearly idle, is LPT.
  Unit files were already independent of their order (loadfile spread them over workers arbitrarily).
- **4 unit shards**, file overhead 0.45 s (fitted on the three shards above, minus a ~10 s fixed start).
- **Durations refresh: nightly bot PR, auto-merged through the merge queue.** `test-durations.yml` (03:20 UTC):
  downloads `test-durations-unit` / `-integration` of the last 3 successful `merge_group` CI runs (CI no longer runs
  on push to main; the queue run is the tree that lands), takes the median per discovered file, and writes a file
  only when the longest shard drops >= 5 s or >= 10 files lack a record. It commits on the latest main through the
  Git Data API (only the two JSON files), on `bot/test-durations`, with the `aa-ecosys-ci-bot` App token (Contents +
  Pull requests, this repo; minted only when there is something to publish), then `gh pr merge --squash --auto`.
  Same safety as the root DB Schema Dump bot: lands only when every required check is green, here in the merge
  queue's full run with the new split. No direct push to main.
- The bot PR body names no Linear id (a nightly PR would link itself to the issue forever); `pr-title.yml` exempts
  `bot/*` branches from the stray-id rule and needs no Refs line.

## Changed

- Durations files refreshed once in this PR: median of the 3 latest successful merge-queue runs (11/11), keys limited
  to files that exist. In `unit.json` the five files sped up here are scaled by their measured local before/after
  ratio (the next nightly refresh replaces every value with a measured one). `integration.json` is exactly what the
  bot computes from those runs.
- The integration split itself is unchanged (4 contiguous shards); only its weights are fresh (117 of 320 files had
  no record).

## Predicted unit shard jobs (model: 20 s job setup + 10 s + 0.45 s per file + simulated 4-worker load)

| Setup | Longest job |
|---|---|
| main today, 3 shards (model check: real 309 / 359 / 263 s) | 301 / 332 / 272 s |
| 3 shards, fresh file, new planner, longest first | ~248 s (4.1 min) |
| 4 shards, fresh file, old planner | ~231 s (3.9 min) |
| **4 shards, fresh file, new planner, longest first (this PR)** | **~198 s (3.3 min)** |

`ci_shards.py plan` (cost model, test seconds only): 3 shards ~218 s each, 4 shards ~168 s each.

## Tradeoffs

- 4 shards = one more runner per CI run (~20 s of setup each). Free on a public repo.
- The bot auto-merges: no person looks at a nightly durations PR. Kept because the queue runs the full CI on the new
  split first and the files feed no deploy path. Kill switch: repo variable `TEST_DURATIONS_AUTO_MERGE=false` (the
  orchestrator then merges the PR). If GitHub refuses the App's auto-merge (ruleset bypass list), the run says so and
  the PR waits for the orchestrator.
- Fresh integration weights move the contiguous shard boundaries: a module that leans on its old neighbour can now
  fail at the start of a shard (S277, CIS #1440 was one). That shows in this PR's own CI (a `.github/` change runs
  every job) and in each bot PR's queue run, never on main.

## Should know

- `test_aa1084_test_durations_bot.py::test_the_bot_suites_match_ci` fails when ci.yml's shard command and the bot's
  `SUITES` table drift apart (roots, exclude, workers, overhead, contiguous, shard env).
- The xdist option `--no-loadscope-reorder` exists in pytest-xdist 3.8.0 (the pin); a bump must keep it.
- Live: the first nightly run after merge is a dry check of the auto-merge permission; read its summary.
