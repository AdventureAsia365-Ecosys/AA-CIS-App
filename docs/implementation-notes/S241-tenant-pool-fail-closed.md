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
