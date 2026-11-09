# AA-1042 — Daily probe cap raised to 50 (S269 lane l7, companion of CIS 376)

Review finding on CIS 376 (every Claude model of the Anthropic API key in the gateway catalog): the nine new enabled
`anthropic-direct` deployments push the daily `llm_connection_probe` over `MAX_PROBES` (30).

## Decisions

- `MAX_PROBES` 30 -> 50 (`services/jobs/llm_probe_job.py`). Counted on the post-376 registry of the 376 unit test:
  33 enabled text + embedding deployments on enabled connections (acc2 6 incl. 2 embedding, acc3 12,
  anthropic-direct 13, openai-direct 2). `build_plan` walks connections in name order, so at 30 the last
  anthropic-direct row and both openai-direct rows (the GPT-6 Luna last-resort rung of every writer chain) were
  listed `over_limit`, never probed, and their `access_state` (read by the stage coverage check) went stale silently.
- Raise the cap rather than seed the five API-only Opus / Fable rows disabled: the tech lead asked for them in the
  catalog as usable fallbacks, selectable in the admin route control, and a disabled deployment is not.
- A separate code PR merged before the CIS 376 migration PR (MIG-4: the migration PR stays migration + tests). The
  cap is harmless before 376 is applied (the plan is smaller than the cap either way).

## Changed

- `services/jobs/llm_probe_job.py`: `MAX_PROBES = 50` with the reason.
- `tests/unit/test_aa1018_probe_dry_run.py`: a registry the size of CIS 376 (invented model keys) is planned with no
  skip, the openai-direct rows last, the cap leaves room for ten more rows, the estimate stays under the S262 bound.

## Tradeoffs

- A higher cap lets a larger registry probe more each day. Cost stays small: the post-376 plan estimates about
  0.019 USD a day (Fable 5.1 / 5 about 0.002 USD each, Opus rows about 0.001), about 0.6 USD a month, under the S262
  bound of 0.03 USD a day that `test_the_estimate_is_catalog_price_times_the_test_size` keeps.
- The candidate sweep (`mode: candidates`, manual) shares the cap: up to 50 disabled deployments at 16 output tokens.

## Should know

- The count on Dev may differ from the test registry; the post-apply live step is a DRY RUN of the probe
  (`POST /admin/job-runner/llm-probe/run` with `{"dry_run": true}`, then the job result) showing no `over_limit`
  line in `skipped`.
