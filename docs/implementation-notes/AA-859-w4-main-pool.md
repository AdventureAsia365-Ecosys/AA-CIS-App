# AA-859 wave 4 — main-pool wait signal, acquire timeout, shrink (S247)

Plan D5 / §2 (root `docs/audits/2026-10-16-S240-aa859/aa859_plan.md`): once waves 1-4 moved the tenant routes to
the tenant pool, shrink the api main pool (`cis-api`, login `cis_app`) from 10 toward the data-standards C2.2
target of 5, "possibly together with a main acquire timeout". Dev has no real users, so the decision is made
from a scripted burst, not from days of gauge data (S247).

## PR 1: acquire-wait signal + default acquire timeout (pool stays 10)

### Decisions
- **Hook = `asyncpg.Pool._acquire` in a slot-less subclass**, switched onto the already created pool
  (`instrument_main_pool`, `pool.__class__ = _InstrumentedMainPool`). asyncpg 0.29 routes `acquire()` (both
  `await` and `async with`) and the `fetch*/execute*` shortcuts through `_acquire`, so ~190 `pool.acquire()`
  call sites and the `pool.fetchrow(...)` shortcuts are all covered without touching a router. Alternatives
  rejected: wrapping every call site (190+ edits); a proxy object (the shortcuts call `self.acquire()` on the
  real pool, so a proxy misses them); building `Pool(...)` directly (copies `create_pool`'s private
  constructor signature). Swapping the class after `asyncpg.create_pool` also keeps the five unit tests that
  patch `main.asyncpg.create_pool` working: a test double is not a `Pool` and is returned unchanged.
- **Private API pinned by a test**: `test_the_hook_is_asyncpgs_single_acquire_path` asserts `_acquire(self,
  timeout)` exists, `PoolAcquireContext` calls it and the four shortcuts call `self.acquire()`. An asyncpg
  upgrade that changes this fails CI rather than silently dropping the signal.
- **Low noise, tenant-pool shape**: per acquire only when `wait_ms >= 50` (`aa859_main_pool_wait wait_ms
  in_use size max`), at most 200 lines per 60 s interval; everything else is aggregated onto the existing
  `aa859_main_pool_gauge` line (`acquires`, `peak_in_use`, `waited`, `max_wait_ms`, `timeouts`, reset each
  tick). `peak_in_use` is read right after each successful acquire (the only moment in-use grows), so a burst
  between two 60 s samples is not missed.
- **Default acquire timeout 5 s** (`MAIN_POOL_ACQUIRE_TIMEOUT_S`) for acquires that pass none; an explicit
  `acquire(timeout=...)` keeps its value. On expiry: ERROR `aa859_main_pool_acquire_timeout` and asyncpg's
  `TimeoutError` goes up (route: 500 with a request id). 5 s = data standards C2.3g ("bounded acquire timeout,
  <= 5 s"), the tenant pool's value too. Before this, a starved or deadlocked main pool (two nested acquires per request) only showed
  as hung requests.

### Changed
- `api/core/aa859_main_pool_gauge.py`: `_InstrumentedMainPool`, `instrument_main_pool`, `_MainPoolStats`,
  constants; gauge line gains the interval counters; docstring says `cis_app` (was `aa_cis_admin`, stale since
  X1 S18).
- `api/main.py`: `instrument_main_pool(pool)` right after `asyncpg.create_pool`; the literal
  `os.environ["DATABASE_URL"], min_size=2, max_size=10` is unchanged (AA-CIS-Infra
  `db/check_connection_budget.py --verify-sources` parses it).
- `CONTEXT.md` sizing paragraph.
- Tests: `tests/unit/test_aa859_main_pool_gauge.py` (+11), new `tests/integration/test_aa859_main_pool_wait.py`
  (real pool, local Postgres: queued acquire logs ~150 ms wait + gauge peak 2; 0.2 s default timeout fires and
  the pool recovers). Mutation: dropping the `_record_acquire` call fails 6 unit tests.

### Tradeoffs
- `__class__` assignment on a library object is unusual; it is limited to one call in the lifespan, the subclass
  adds no slots (same layout, so Python allows it), and `isinstance(pool, asyncpg.Pool)` stays true.
- The measured wait includes connecting a new connection when the pool grows (TLS to RDS, tens of ms), so the
  first burst after an idle spell can log a few lines from growth alone. The burst runbook warms up first.
- Counters are module-level (one main pool per api process); the worker does not import this.

### Should know
- The in-API job worker (`JOB_WORKER_IN_API`, local only) shares this pool, so its acquires get the 5 s default
  too.
- Logs Insights (group `/ecs/aa-cis-dev`, api streams): `fields @timestamp, @message | filter @message like
  /aa859_main_pool_(gauge|wait|acquire_timeout)/`.
