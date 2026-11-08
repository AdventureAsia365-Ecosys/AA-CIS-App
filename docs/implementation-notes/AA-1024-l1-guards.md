# AA-1024 — lane l1-guards (S273): the nightly integration run in random order is red

The nightly `Nightly integration (random order)` (lane l22, G8) was red on both of its runs (06/11 and 07/11). Run
42423125464 (seed 92395, 287 files): 55 failed, 224 errors, in about 40 modules. The report job opened issue #1379
(label `nightly-red`); the next green nightly closes it. Reproduced locally with the exact module order of that run
on a throwaway Postgres: the same 55 failed / 224 errors.

## Decisions
- **Root cause: one mutable database shared by every module.** conftest replays the migrations once and every module
  then works on that database; a module whose teardown does not restore everything leaks into every later module.
  Leakers found in the seed-92395 order:
  - `test_aa712_migration_226` drops the `catalog` and `geo` schemas in its setup (`_undo_226`), rebuilds geo from 226
    alone and never brings the catalog views back: about 200 of the 279 red tests (`schema "catalog" does not exist`,
    `catalog.v_destination` missing, `migration 253: catalog views of 225 / 228 / 235 missing`, `geo.geocode_call`
    missing in the erasure inventory).
  - `x1_244_undo` (used by the 244 / 245 / X1 privilege modules) revokes every privilege of `cis_app` in the CIS
    schemas, also the grants later replayed migrations gave (309, 322, 347, the KPI views ...): `permission denied for
    table llm_app_profile / llm_provider_connection / provider_health / provider_credit_topup`, missing column grants.
  - Replays of older migrations and route tests change seed rows (`8 materialised row(s) differ from the resolver` in
    353-361, route chains in 326 / 327 / 366 / 376 / route control).
  The 4-shard CI stayed green only because of its contiguous file order (the lessons log already lists four repeats
  of this class, S252..S270).
- **Fix: every module starts on a fresh copy of the replay.** After the replay, `_setup_schema` freezes `DB_NAME` as
  the template `<DB_NAME>_replay_tpl` (`ALLOW_CONNECTIONS false`); the module-scoped autouse fixture
  `_fresh_db_per_module` drops `DB_NAME` and creates it again from the template before each module. With postgres 15+
  (`WAL_LOG` strategy) a copy of this ~45 MB database takes about 0.1 s, a drop about 0.1 s: about 1 minute for the
  whole suite, about 20 s per CI shard. Chosen over fixing each leaker's teardown: the leak class has repeated four
  times, each new migration that touches a table an old test replays can bring it back, and no teardown can restore
  everything a superuser replay did (owners, grants, policies, rows). The leakers' teardowns are left as they are.
- **What a copy does not reset:** cluster-wide objects (roles, role attributes, memberships, passwords). The database
  owner and ACL live in `pg_database` (cluster-wide too), so the reset re-applies the replayed owner and ACL.
  A module that changes a role must still undo it.
- **`DROP ROLE catalog_owner`** in `test_aa679_catalog_views` / `test_aa712_migration_228` now fails (the template still
  depends on the role); both modules already caught that error ("owned objects in another DB of a shared cluster"),
  so 225 runs with the existing role, as it does on a shared local cluster.
- **Switch:** `CIS_TEST_DB_RESET=off` keeps the old shared database (to look at what one module leaves behind).
  `CIS_TEST_ADMIN_DB` (default `postgres`) is the maintenance database the drop / create runs from.

## Changed
- `tests/integration/conftest.py`: `freeze_replay_template()`, `reset_db_from_template()`, `_database_acl()`, fixture
  `_fresh_db_per_module`; `_setup_schema` freezes the template after the replay.
- New `tests/integration/test_aa1024_fresh_db_per_module.py`: the template exists and refuses connections; a reset
  undoes a dropped schema, a revoked grant, a leftover table and a leftover registry row, and keeps the database ACL.

## Tradeoffs
- Every open connection to `DB_NAME` is closed at each module boundary (`pg_terminate_backend`). Module-scoped
  fixtures are torn down before the next module's setup, so only a connection kept across modules (a global pool in
  application code) would notice; none did in the full run.
- A module can no longer lean on rows or objects an earlier module left (it never should have); such a module now
  fails in every order instead of only in some.

## Should know
- Replay a red nightly order locally: build the file list from the run log (the `-v` lines, first occurrence of each
  file) or `ci_shards.py files --shards 1 --index 1 --shuffle <seed>` on the same commit, then run pytest with that
  list against a throwaway cluster (`CIS_TEST_DB_PORT`).
- Local runs need a superuser `cistest` able to create databases (CI's postgres service user is one).
