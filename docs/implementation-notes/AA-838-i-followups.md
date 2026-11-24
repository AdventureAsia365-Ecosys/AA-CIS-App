# AA-838 (S291 lane i-followups) — stable savepoint names on the CIS pools

The weekly C4.3 `pg_stat_statements` review (W48) found AA-Partner churning the 10,000 tracked statements with one
new `SAVEPOINT` / `RELEASE SAVEPOINT` text per nested transaction (asyncpg names savepoints from a process-wide
counter); AA-Partner #389 fixed it with a connection class that names savepoints by depth. This note covers the same
check for AA-CIS-App.

## Decisions

- **CIS does nest transactions**: two lexically nested blocks (`services/writing_memory/post_activation.py`,
  `services/tour_lifecycle/service.py`) and about 130 functions that open `conn.transaction()` on a connection their
  caller passes in, many of them explicitly as a savepoint inside the caller's transaction (fail-soft reads of an
  optional table in `api/services/*`, `services/quality/*`, `services/tour_lifecycle/fanout.py`, per-row retries in
  `services/acp_shared/retarget.py` and `services/places/store.py`, the erasure DRY RUN statements). So the fix applies.
- **Port, not a shared package**: `shared/pg_connection.py` is AA-Partner's `app/pg_connection.py` with prefix
  `cis_sp_` (the repos share no Python package). Depth names, not one fixed name, keep the `ROLLBACK TO` semantics.
  asyncpg 0.29.0 (CIS pin) has the same two internals as 0.31.0 (checked: `Transaction.start` calls
  `con._get_unique_id('savepoint')` and stores it in `_id`).
- **Pools changed**: the api main pool (`api/main.py`, `cis-api`), the tenant pool (`api/core/aa544_tenant_pool.py`,
  `cis-tenant`: only `connection_class` is added; its role, limits, timeouts and the fail-closed logic are unchanged)
  and the job worker pool (`shared/jobs/worker.py`, `cis-worker`). These carry all the steady load.
- **Not changed**: `shared/repository/database.py` (no importer), and the one-off pools of manual or batch scripts
  under `services/*` (`grounding.py`, `eval/regression.py`, `export/handler.py`, the writer proof and writing memory
  runners, the tenant backup / E2E fixtures CLIs): a manual run adds a few texts once. Also untouched: the ops status
  files another S291 lane edits, compliance and the two-person publish code.

## Changed

- `shared/pg_connection.py` (new); `connection_class=StableSavepointConnection` on the three pools above.

## Tradeoffs

- Relies on two asyncpg internals, pinned by `requirements.txt`; the unit test fails if an upgrade changes the
  statement texts.

## Should know

- Tests: `tests/unit/test_s291_pg_connection.py` (statement texts, rollback depth, the three pools) and
  `tests/integration/test_s291_pg_connection_pg.py` (rollback semantics on PostgreSQL).
- Live check after deploy: `pg_stat_statements` rows for role `cis_app` (and the tenant role) whose query starts with
  `SAVEPOINT` / `RELEASE` / `ROLLBACK TO` stop growing; new ones read `cis_sp_<n>`.
