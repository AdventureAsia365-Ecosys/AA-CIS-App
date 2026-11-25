# S294: flaky verify rate-limit test, pre-push gitleaks resolver

Two S293 lesson follow-ups. Test and tooling only: no app runtime change.

## Decisions

- **Flaky test root cause: the minute boundary, not shared state.** The public certificate verify limit counts in a
  fixed window keyed on `int(time.time() // 60)` (`api/routers/learn.py`, `verify_count`). The test sends 30 requests
  and expects the 31st to be refused. When those requests straddled a minute boundary, the count restarted and the
  31st got 404, not 429. That matches the merge-group failure on 25/11. The xdist workers do not share state: each
  worker is its own process, and the harness clears `_window` on start. A local repro with a clock that rolls over
  mid-loop gives exactly `assert (404 == 429)`.
- **The fix is in the test harness, not the router.** The fixed window per minute is the documented design (module doc:
  "one fixed window per minute in Redis"), not a bug. `tests/unit/aa1105_learn_fake.py` `Harness` now replaces the
  router's `time` with a stub that returns `Harness.now`. The default is `FROZEN_EPOCH`, 20 s into a minute, so
  Retry-After is a known 40. `close()` restores the real module and clears the window. Every verify test (the per-address
  limit, the setting, the visitor-limit header and its parametrised cases) runs on the pinned clock.
- **The pre-push gitleaks binary comes from a resolver that tries each candidate.** `scripts/pre-push.sh` `gitleaks_bin`
  took `$AA_GITLEAKS`, then the first `gitleaks` on PATH. On a machine where an inactive mise shim is first on PATH, the
  shim answers "No version is set for shim" with exit 1, and the hook blocked the push with "gitleaks unreadable at
  .../mise/shims/gitleaks" (S284, S289 and S293 lessons). The new `gitleaks_resolve` follows the same order as the
  root `tools/github/gitleaks_resolve.py`: `$AA_GITLEAKS` (taken as given when it runs), then the workspace copy
  `.tmp-session/gitleaks-8301/gitleaks` in a parent directory (pin only), then the mise install of the pin (pin only),
  then every PATH entry (the first at the pin, else the first that runs). Each candidate passed over is named with its
  reason. The pinned version check stays: no working binary, or only an other-version one, still blocks.

## Changed

- `tests/unit/aa1105_learn_fake.py`: the harness pins the limiter clock (`FROZEN_EPOCH`, `Harness.now`) and restores it.
- `tests/unit/test_aa1105_learn_api.py`: new `test_verify_window_is_one_fixed_minute_on_the_pinned_clock` (Retry-After
  40, same minute still refused, next minute a fresh count) and `test_harness_restores_the_router_clock`.
- `scripts/pre-push.sh`: `gitleaks_bin` replaced by `gitleaks_probe` + `gitleaks_resolve`; `do_gitleaks` reports the
  candidates passed over.
- `tests/unit/test_aa1024_prepush_guards.py`: four resolver tests (shim passed over for the mise install or the next PATH
  entry, only a shim blocks with the reason, workspace copy off the pin, a broken `AA_GITLEAKS` falls through). The
  gitleaks tests now isolate PATH, HOME and the mise data dir, so the machine's own shim or install cannot decide them.

## Tradeoffs

- The resolver is ported to bash rather than calling the root Python resolver. This repo is checked out alone in CI
  and on other machines, so the hook cannot rely on the root repo being next to it. The two copies must keep the same
  order. The root module doc names the order; this hook's comment points to it.
- A frozen clock means the tests no longer exercise real time. That is the point: the window arithmetic is tested
  explicitly by moving `Harness.now` across the boundary.

## Should know

- Other fixed-minute limiters have the same latent flake in their tests: `api/middleware/view_as.py` (5 requests) and
  `api/routers/tenant_session.py` (the exchange limit). Neither has been seen red. They are listed as a follow-up and
  not changed here.
- Proof: the aa1105 unit files, 24 runs with `-n 4 --dist load` and a shuffled order per run (a scratch plugin that
  shuffles with a fixed seed per run; pytest-randomly is not installed): 24 of 24 green.
