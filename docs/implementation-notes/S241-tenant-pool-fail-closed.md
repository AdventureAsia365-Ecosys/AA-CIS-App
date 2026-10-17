# S241 — tenant-pool route flags fail closed (decision D-12, Refs AA-859)

Nghiệp decision D-12 (S241): when a tenant-pool route flag cannot be read, the route uses the tenant pool
(`aa_app_user`, RLS), never the admin pool. An explicit OFF value still moves a route back to the admin pool
(the per-route rollback stays). Applies to all 11 flags in `api/core/aa544_tenant_pool.py` (5 `aa544:` /
`aa817:` + 6 `aa859:`), all `"true"` on Dev since S240.

## Decisions

- **Encoding.** ON/OFF values stay as they were (`"true"` / `"false"`), only the default flips:
  | Redis state | Before | After (D-12) |
  |---|---|---|
  | `"true"` | tenant pool | tenant pool |
  | `"false"` (case-insensitive, whitespace stripped, bytes decoded) | admin pool | **admin pool** (the only OFF) |
  | key missing | admin pool | **tenant pool** |
  | any other value (`"0"`, `"off"`, typo) | admin pool | **tenant pool** + WARNING `aa817_tenant_flag_unrecognized` |
  | Redis error | admin pool + ERROR | **tenant pool** + ERROR `aa817_tenant_flag_read_failed reason=redis_error` |
  | Redis read > 0.5 s | request hung (no timeout) | **tenant pool** + ERROR `... reason=timeout` |
  | no Redis client on `app.state` | admin pool (AttributeError caught) | **tenant pool** + ERROR `... reason=client_missing` |
  Constants: `FLAG_OFF_VALUE = "false"`, `FLAG_ON_VALUE = "true"`, `FLAG_READ_TIMEOUT_S = 0.5`.
- **Only `"false"` is OFF.** Accepting `"0"` / `"off"` too would widen the set of values that open the admin
  path; one documented value is easier to audit. A typo therefore keeps the route on the tenant pool (safe
  side) and is logged.
- **Metric event kept.** `aa817_tenant_flag_read_failed` (ERROR) is unchanged as an event name, so any
  metric filter on it keeps counting; its `fallback` field is now `"tenant_pool"` (was `"admin_pool"`) and it
  gains `reason=`. A missing key logs nothing (it is the normal ON state after a Redis flush).
- **Read timeout added (0.5 s).** The api's Redis client (`api/main.py`) has no socket timeout, so a hung
  ElastiCache would hold every tenant request. Bounded with `asyncio.wait_for`; on timeout → tenant pool.
- **One reading rule for both `fail_closed` modes.** No production caller uses `fail_closed=False`; keeping
  one `_flag_enabled` avoids two encodings.

## Changed

- `api/core/aa544_tenant_pool.py`: `_flag_enabled` rewritten as above; module docstring (encoding + rollback =
  `SET <key> false`), flag-constant comment (a newly wired route is ON from its first deploy), stale
  "Default off (admin pool)" comment removed, `acquire_scoped_conn` docstring.
- `CONTEXT.md` (Tenant DB role section): fail-closed encoding, rollback = `SET <key> false`, `DEL` is not a
  rollback, the 241 rollback STEP 0 now means `SET ... false`.
- Tests:
  - `tests/unit/test_aa817_tenant_pool_fail_closed.py`: new D-12 block — value matrix (`"true"`, missing,
    `"false"`, `"FALSE"`, `" false "`, `b"false"`, garbage, `"0"`), Redis error → tenant pool + ERROR event,
    timeout → tenant pool, no client → tenant pool, missing key → tenant pool with no error log, unrecognised
    value → WARNING, Redis error + no tenant pool → 503 (never the admin pool). Replaces
    `test_redis_error_counts_as_flag_off`.
  - Shared route matrix `tests/unit/test_aa817_fail_closed_routes.py`: `test_redis_error_falls_back_to_admin_pool_loudly`
    → `test_redis_error_keeps_tenant_pool_loudly`, new `test_missing_flag_key_uses_tenant_pool`; both run for
    every route through `MATRIX_CHECKS` / wrappers (t2_version_write, integration_connect, unpublish). The
    per-route copies in piece_edit, t2_version_reads and angle_gate_decide flipped the same way.
  - "Flag off" mocks now return `"false"` instead of `None` (unit + 7 integration files), and legacy route
    tests whose `MagicMock` request had no awaitable `redis.get` (aa449, aa455, aa457, aa458, aa462 x2, aa796,
    aa802 x2, aa815, `_main_request`) set the explicit OFF value — they test route logic on the main pool,
    which is now only reachable through an explicit OFF.
- Root repo (not in this PR, plain edit, uncommitted): `docs/audits/2026-10-16-S240-aa859/aa859_flag.py`
  `off` now does `SET <key> "false"` (was `DEL`), new `del` action for cleanup only (= back to ON default),
  `ALLOWED` widened from the 6 `aa859:` keys to all 11 tenant-pool flags; `aa859_flip_verify.py` docstring
  says rollback = `SET "false"`. `flip_verify --off` and the auto-revert path call `flag("off")`, so they now
  write `"false"` with no further change.

## Tradeoffs

- **New routes are ON on first deploy.** With a missing key = ON, wiring a new route to a new flag moves it
  to the tenant pool as soon as it deploys (no "dark" period). That is the D-12 intent (no main-pool fallback
  for tenant data, DM-8.4); to ship dark, `SET <key> false` before the deploy (noted in the module comment
  and CONTEXT.md). Alternative considered: a per-flag allow-list where only the 11 known flags default ON —
  rejected as an extra rule to keep in sync.
- **Redis down + tenant pool down = 503** on these 11 routes (before: admin pool served them). Accepted:
  serving tenant data without RLS is the failure D-12 removes.
- **Unrecognised values are ON.** A mistyped rollback (`SET key off`) does not roll back; the WARNING line
  and the per-request `used_tenant_pool=True` show it.

## Should know

- **Runbook change (operators):** rollback of a tenant-pool route is now `SET <key> false`
  (`aa859_flag.py off <key>`). `DEL <key>` turns the route ON. The `DEL` in migration 241's rollback comment
  (STEP 0) and in older notes (`AA-817.md`: `redis-cli DEL aa817:content_piece_reads_pool`,
  `redis-cli DEL aa544:integration_disconnect_pool`) predate D-12 — read them as `SET ... false`. The applied
  migration file was not edited (it is mirrored in AA-CIS-Infra `db/migrations/cis/`).
- No live behaviour change on Dev today: all 11 keys hold `"true"`. The change matters only on a Redis error,
  timeout, flush, or a key that is deleted/mistyped.
- Data standards: SEC-10 / §9 row "CIS tenant pool falls back to the admin pool when its Redis flag is off or
  the pool is `None`" and C2.3g's exception are narrowed by this PR to "explicit OFF only" (the pool-`None`
  fallback remains only for `fail_closed=False`, which has no production caller). The root data-standards
  files were not edited here (they have unrelated uncommitted edits in the root working tree); the §9 row
  wording can be tightened in the S241 root PR.
- Integration tests: only the flag mocks changed (`None` → `"false"` for the off case). They could not run
  locally (the local `cis_integration_test` DB lacks `aa_app_user`; same 71 failures before and after the
  change); CI runs them on a fresh Postgres.

## Review round 1 (S241) — six findings, all accepted

### Decisions
- **One flag decision per request.** `_flag_enabled` memoises on `request.state._tenant_flag_cache`
  (keyed by flag). Unpublish (3 blocks) and disconnect (2 blocks) can no longer switch pools mid-request:
  under a rollback (`"false"`), a Redis blip at the last block (after the WordPress trash / Graph DELETE or
  the secret tombstone) used to give ON → tenant pool → 42501 if the route was rolled back for broken
  grants. Also one Redis round-trip and at most one ERROR line per request. A request object without
  `.state` (SimpleNamespace in integration tests, direct callers) is simply not cached.
- **OFF values widened (review option a):** `FLAG_OFF_VALUES = {"false", "0", "off", "no"}`. This reverses
  the "only `false`" decision above: during an incident a hand-typed `SET key 0` silently not rolling back is
  worse than a slightly wider OFF set. Still explicit values only; garbage and missing stay ON, and an
  unrecognised value now logs at ERROR (was WARNING).
- **Durable rollback:** env var `TENANT_POOL_FORCE_ADMIN_FLAGS` (comma-separated keys, read once at import),
  checked before Redis. Redis is single-node with no persistence, so a reboot / replacement / FLUSHALL drops
  every key = every route back ON with no log line; the env var survives that. INFO
  `aa817_tenant_flag_forced_admin` once per key per process; unknown keys log ERROR at import (a typo would
  otherwise be a no-op rollback). Not set in the task definition today (empty = no override).
- **Redis client bounds:** `api/core/redis_client.py::build_api_redis` — socket connect / read timeout 0.5 s,
  `Retry(NoBackoff(), 1)`. The rate-limit middleware awaited Redis unbounded before the route ran, so the
  0.5 s `wait_for` on the flag never helped a blackholed Redis. **Deviation from the review:** no
  `health_check_interval`. Measured against a local server that accepts and never answers: redis-py 5.2.1
  with `health_check_interval` + `retry` hangs (no timeout fires), and 8.1 doubles the wait. Explicit
  `retry` keeps the bound the same on redis-py 5.x (local venv) and 8.x (what `redis[asyncio]>=5.0.0`
  installs in CI / the image today).
- **`fail_closed` defaults to True** (kept the parameter and the fail-open branch for an explicit
  `fail_closed=False`; no production caller uses it). `ALL_TENANT_POOL_FLAGS` lists the 11 keys; a unit test
  fails when a new `*_FLAG` constant is not in it.

### Changed
- `api/core/aa544_tenant_pool.py` (memo, OFF set, override, default, inventory), new
  `api/core/redis_client.py`, `api/main.py` (uses it; `aioredis` import kept for tests that patch it).
- Docstrings: `v1_publish.py` list_publish_log + list_pending, `service.py::fetch_piece`, `v1_integrations.py`
  W9 block comment (missing key = ON, rollback = SET false).
- Migrations 237 / 241: rollback comments only (STEP 0 = `SET <key> false` + env override, confirm
  `used_tenant_pool=False`). Comment-only; DB Migrate's sha256 is a transport check, not a stored checksum,
  and both are already applied. The AA-CIS-Infra copies must be byte-identical (Migration Copy Guard):
  the copy PR goes after this one merges.
- Old notes that said `DEL` (AA-817, AA-859-d5/d6/i) carry a "superseded" marker.
- Tests: memo (3 blocks, `["false", RedisError, RedisError]` → admin pool in all three, one GET, no ERROR),
  per-flag / per-request cache, real Starlette `Request.state`, override, OFF matrix, default fail closed,
  inventory, Redis client kwargs, lifespan builds the bounded client, middleware + flag read against a hung
  local server (fail open in < 3 s / < 2 s).

### Should know
- Root `docs/audits/2026-10-16-S240-aa859/aa859_flag.py` `ALLOWED` must hold all 11 keys (it does in the
  root working tree, uncommitted) — keep it in step with `ALL_TENANT_POOL_FLAGS`.
- To make a rollback stick: add the key to `TENANT_POOL_FORCE_ADMIN_FLAGS` in the api task definition
  (AA-CIS-Infra `modules/ecs`), which is a deploy.

## CI round (PR #792, Integration Tests red) — tests state the flag explicitly

### Changed
- `tests/integration/test_aa802_pending_latest_piece.py` and `tests/integration/test_aa815_publish_log_sql.py`
  built a `MagicMock` request whose `app.state.redis.get` is not awaitable and `tenant_pool = None`. Before
  D-12 the failed read meant OFF, so they silently ran on the admin pool; under D-12 it means ON, so 11 tests
  got 503 (`aa817_tenant_flag_read_failed reason=redis_error`). Both harnesses now set
  `app.state.redis = _FlagsOff()` (async `get` returning `"false"` for every key in
  `ALL_TENANT_POOL_FLAGS`): they test the route SQL on the admin pool (owner connection) on purpose. The
  tenant-pool / RLS side of the same routes is `test_aa817_tenant_pool_routes.py` and
  `test_aa859_unpublish_pool.py` (explicit `_Redis(flag_on)`). Production rule unchanged.
- Every other integration test that reaches `acquire_scoped_conn` already passes an explicit `_Redis` fake
  (`"true"` / `"false"`); the remaining `MagicMock` / `redis=None` requests (aa791 label routes, aa819 admin
  override) call admin routes that read no tenant-pool flag.
- New unit test `tests/unit/test_s241_tenant_flag_keys.py`: AST-scans `api/`, `services/`, `shared/`,
  `worker/` for every flag key handed to `acquire_scoped_conn` / `_flag_enabled` / `_read_flag` (must resolve
  to a `*_FLAG` constant or a literal) and asserts the set equals a documented literal list of 11 keys, which
  must also equal `ALL_TENANT_POOL_FLAGS`; a third check refuses any other `aaNNN:..._pool`-shaped string
  literal in production code. Under D-12 a new or renamed key is a missing key = ON, so it cannot appear
  silently. Scanned `services/` too: three of the 11 keys are read there (`fetch_piece`,
  `fetch_latest_piece_for_request`, `update_piece_content_text`).

### Should know
- **`aa544:publish_log_list_pool` was MISSING in live Redis** (not `"true"` as the notes above assumed for all
  11). Under D-12 a missing key is ON, so merging would have moved GET /v1/publish-log onto the tenant pool
  with no A/B. The orchestrator pinned it to `"false"` on Dev before merge. Turning it on needs the AA-859
  A/B check (tenant pool vs admin pool response for the same tenant, `used_tenant_pool=True` in the logs)
  first, then `SET aa544:publish_log_list_pool true`.
- Verified locally: the unit file passes, and a mutation (one call site switched to `"aa999:new_pool"`)
  fails both scan checks; the two integration modules import and collect (12 tests), and their `_request`
  harness sends all 11 flags to the admin pool through the real `acquire_scoped_conn`. The integration tests
  themselves only run in CI (no `aa_app_user` locally).
