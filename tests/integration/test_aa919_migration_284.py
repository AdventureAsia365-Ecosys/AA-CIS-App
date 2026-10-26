"""AA-919 (WM-17) — CIS 284 (golden set tables, widened prompt_eval_runs) on real Postgres.

conftest replays 284 like every file (with stand-in 281-283 registry rows where missing: the replay skips 281).
Each test runs in one transaction that is rolled back.

  * the file applied and a re-run is a no-op; guard 1 refuses while 281, 282 or 283 is unregistered and over other
    columns; RLS on and forced on items and results with exactly three policies each; the grants are exact (also
    after stray grants); the AA-859 invariant v3 finds nothing on the new tables;
  * isolation: the tenant pool (aa_app_user) has no privilege; cis_app / cis_owner see every row and a read that
    filters tenant_id (as the code does) never resolves another tenant's item (ADR 0010 decision 10);
  * the CHECKs of the three tables and of the widened run table; the legacy s1_old regression insert still works;
  * the result trigger: tenant copy, phase = split, no post_edit score_only control, run of the item's writer and
    golden set, controls regenerated only in baseline_import / reverify, a rejudge of the same item; the run freeze
    trigger: a run with results keeps its pipeline / writer / run_kind / golden_set_id;
  * one active version per set name; deleting an item removes its results (erasure order).
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X284_STANDIN_MARK, X284_PREDECESSORS  # noqa: E402
from _rls_fixture_helpers import NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SHA = "a" * 64
ITEM_SQL = """
    INSERT INTO shared.eval_golden_item (set_id, sample_id, writer, channel, scope, tenant_id, split, trap, kind,
                                         input, label, label_verdict, label_source, input_sha256, post_edit, tiers,
                                         injection_spec)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '{}'::jsonb, '{}'::jsonb, 'pass', 'agent_panel', $10, $11,
            $12::text[], $13::jsonb)
    RETURNING item_id
"""
RUN_SQL = """
    INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by,
                                         run_kind, writer, golden_set_id)
    VALUES ('writing_memory', 'zz919', 0, 0, '{}'::jsonb, 'pytest', $1, $2, $3)
    RETURNING id
"""
RESULT_SQL = """
    INSERT INTO shared.eval_item_result (run_id, item_id, writer, scope, tenant_id, arm, mode, phase, verdict,
                                         output, output_sha256, scored_result_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12)
    RETURNING result_id
"""


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X284_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X284_STANDIN_MARK)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _set(conn, name=None, status="active"):
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, status, superseded_at, source_uri, source_sha256, seed, "
        "item_count, imported_by) VALUES ($1, '0.0.1', $2, CASE WHEN $2 = 'superseded' THEN now() END, "
        "'file:zz.json', $3, 1, 0, 'pytest') RETURNING set_id",
        name or f"zz919_{uuid.uuid4().hex[:8]}", status, SHA)


async def _item(conn, set_id, *, writer="t9_blog", tenant=None, split="tune", post_edit=False, sample=None):
    channel = {"t9_blog": "blog", "t9_social": "linkedin"}.get(writer)
    scope = "platform" if writer == "a1" else "tenant"
    if scope == "tenant" and tenant is None:
        tenant = uuid.uuid4()
    trap = split == "trap"
    return await conn.fetchval(ITEM_SQL, set_id, sample or f"zz-{uuid.uuid4().hex[:10]}", writer, channel, scope,
                               tenant, split, trap, "synthetic" if trap else "real", SHA, post_edit,
                               ["post_edit"] if post_edit else [], '{"canary": "x"}' if trap else None)


async def _item_row(conn, item_id):
    return await conn.fetchrow("SELECT writer, scope, tenant_id, split FROM shared.eval_golden_item "
                               "WHERE item_id = $1", item_id)


async def _result(conn, run_id, item_id, *, arm="candidate", mode="score_only", phase=None, verdict="pass",
                  output=None, scored=None, **override):
    it = await _item_row(conn, item_id)
    phase = phase or {"trap": "probe"}.get(it["split"], it["split"])
    return await conn.fetchval(RESULT_SQL, run_id, item_id, override.get("writer", it["writer"]),
                               override.get("scope", it["scope"]), override.get("tenant_id", it["tenant_id"]),
                               arm, mode, phase, verdict, output, SHA if output else None, scored)


async def _as(conn, role, guc=None):
    # other modules re-create the roles or revoke ALL from cis_app (X1 teardown): re-apply 284 for its grants
    await conn.execute(f"GRANT USAGE ON SCHEMA shared TO {role}")       # first: FK checks run as the table owner
    await conn.execute(migration_body(284))
    if role == "cis_app":         # Dev's grant on the run ledger (CIS 244; the replay skips 244), unchanged by 284
        await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")
    await conn.execute(f"SET LOCAL ROLE {role}")
    if guc is not None:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)


async def _refused(conn, exc, sql, *args, match=None):
    with pytest.raises(exc, match=match):
        async with conn.transaction():
            await conn.execute(sql, *args)


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_284_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '284'"), \
        "conftest swallowed an error in 284 (see its log)"
    s = await _set(conn)
    await _item(conn, s)
    before = await conn.fetchval("SELECT count(*) FROM shared.eval_golden_item")
    await conn.execute(migration_body(284))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '284'") == 1
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_golden_item") == before   # the probe rolled back


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", X284_PREDECESSORS)
async def test_guard_requires_every_predecessor(conn, missing):
    # readiness compares max(version): once 284 is in, a missing 281 / 282 / 283 would be hidden (critic 17)
    await conn.execute("DELETE FROM shared.schema_versions WHERE version = $1", missing)
    await _refused(conn, asyncpg.RaiseError, migration_body(284), match=f"migration 284: {missing} not registered")
    await conn.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                       "VALUES ($1, now(), 'zz919')", missing)
    await conn.execute(migration_body(284))


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["shared.eval_golden_item", "shared.prompt_eval_runs"])
async def test_guard_refuses_a_rerun_over_other_columns(conn, table):
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN zz919 text")
    await _refused(conn, asyncpg.RaiseError, migration_body(284), match="other columns|neither the 089")


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["eval_golden_item", "eval_item_result"])
async def test_rls_forced_with_exactly_three_policies(conn, table):
    assert tuple(await conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     f"WHERE oid = 'shared.{table}'::regclass")) == (True, True)
    rows = await conn.fetch("SELECT policyname, permissive, roles::text[] AS roles, cmd FROM pg_policies "
                            "WHERE schemaname = 'shared' AND tablename = $1 ORDER BY policyname", table)
    assert [(r["policyname"], r["permissive"], r["roles"], r["cmd"]) for r in rows] == [
        (f"{table}_cis_app_all", "PERMISSIVE", ["cis_app"], "ALL"),
        (f"{table}_cis_owner_all", "PERMISSIVE", ["cis_owner"], "ALL"),
        ("tenant_isolation", "PERMISSIVE", ["aa_app_user"], "ALL"),
    ]
    assert not (await conn.fetchrow("SELECT relrowsecurity FROM pg_class "
                                    "WHERE oid = 'shared.eval_golden_set'::regclass"))[0]


@pytest.mark.asyncio
async def test_privileges_are_exact(conn):
    for t in ("eval_golden_set", "eval_golden_item", "eval_item_result"):
        await conn.execute(f"GRANT SELECT ON shared.{t} TO catalog_owner, PUBLIC")
        await conn.execute(f"GRANT SELECT, INSERT ON shared.{t} TO aa_app_user")
        await conn.execute(f"GRANT UPDATE (created_at) ON shared.{t} TO aa_app_user")
        await conn.execute(f"GRANT TRUNCATE, REFERENCES, UPDATE ON shared.{t} TO cis_app")
    await conn.execute("GRANT EXECUTE ON FUNCTION shared.eval_item_result_guard() TO PUBLIC")
    await conn.execute(migration_body(284))
    want = {"eval_golden_set": {"SELECT", "INSERT", "UPDATE"}, "eval_golden_item": {"SELECT", "INSERT", "DELETE"},
            "eval_item_result": {"SELECT", "INSERT", "DELETE"}}
    for t, privs in want.items():
        rows = await conn.fetch(f"""
            SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, a.privilege_type AS p
              FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
             WHERE c.oid = 'shared.{t}'::regclass
               AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                                   'catalog_owner'::regrole))""")
        assert sorted((r["r"], r["p"]) for r in rows) == sorted(("cis_app", p) for p in privs), t
        assert not await conn.fetchval(f"SELECT bool_or(attacl IS NOT NULL) FROM pg_attribute "
                                       f"WHERE attrelid = 'shared.{t}'::regclass")
    assert not await conn.fetchval("SELECT has_function_privilege('public', 'shared.eval_item_result_guard()', "
                                   "'EXECUTE')")
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "eval_" in r["rel"]]
    assert bad == []


# ── isolation ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_pool_has_no_privilege(conn):
    a = uuid.uuid4()
    s = await _set(conn)
    await _item(conn, s, tenant=a)
    await _as(conn, "aa_app_user", str(a))
    for t in ("eval_golden_set", "eval_golden_item", "eval_item_result"):
        await _refused(conn, asyncpg.InsufficientPrivilegeError, f"SELECT 1 FROM shared.{t}",
                       match="permission denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cis_app", "cis_owner"])
async def test_runtime_and_owner_see_every_row_and_code_filters_tenant(conn, role):
    a, b = uuid.uuid4(), uuid.uuid4()
    s = await _set(conn)
    ia, ib, ip = await _item(conn, s, tenant=a), await _item(conn, s, tenant=b), await _item(conn, s, writer="a1")
    if role == "cis_owner":                       # the Dev shape (245): cis_owner owns it, and FORCE filters the owner
        await conn.execute("ALTER TABLE shared.eval_golden_item OWNER TO cis_owner")
    await _as(conn, role, str(uuid.uuid4()))                                  # a forged GUC changes nothing for them
    seen = {r["item_id"] for r in await conn.fetch("SELECT item_id FROM shared.eval_golden_item WHERE set_id = $1", s)}
    assert {ia, ib, ip} <= seen
    own = {r["item_id"] for r in await conn.fetch(
        "SELECT item_id FROM shared.eval_golden_item WHERE scope = 'tenant' AND tenant_id = $1", a)}
    assert own == {ia}


@pytest.mark.asyncio
async def test_cis_app_writes_a_run_and_a_result(conn):
    s = await _set(conn)
    i = await _item(conn, s)
    await _as(conn, "cis_app")
    run = await conn.fetchval(RUN_SQL, "baseline_import", "t9_blog", s)
    rid = await _result(conn, run, i, arm="control")
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_item_result WHERE result_id = $1", rid) == 1
    await _refused(conn, asyncpg.InsufficientPrivilegeError,
                   "UPDATE shared.eval_item_result SET verdict = 'fail' WHERE result_id = $1", rid)   # immutable


# ── the CHECKs ──────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("writer,channel,scope,tenant,split,trap,tiers,spec", [
    ("a1", None, "platform", "x", "tune", False, [], None),                      # a1 with a tenant
    ("t9_blog", "blog", "tenant", None, "tune", False, [], None),                # T9 without a tenant
    ("t9_blog", "linkedin", "tenant", "x", "tune", False, [], None),             # blog writer on linkedin
    ("t9_social", "blog", "tenant", "x", "tune", False, [], None),               # social writer on blog
    ("t2", "blog", "tenant", "x", "tune", False, [], None),                      # t2 with a channel
    ("t2", None, "platform", None, "tune", False, [], None),                     # t2 as platform
    ("a1", None, "platform", None, "tune", False, ["long_7d"], None),            # unknown tier
    ("a1", None, "platform", None, "tune", True, [], '{"c": 1}'),                # trap flag on tune
    ("a1", None, "platform", None, "trap", True, [], None),                      # a trap without a spec
    ("a1", None, "platform", None, "dev", False, [], None),                      # unknown split
])
async def test_item_checks(conn, writer, channel, scope, tenant, split, trap, tiers, spec):
    s = await _set(conn)
    await _refused(conn, asyncpg.CheckViolationError, ITEM_SQL, s, "zz-x", writer, channel, scope,
                   uuid.uuid4() if tenant else None, split, trap, "real", SHA, False, tiers, spec)


@pytest.mark.asyncio
async def test_item_post_edit_tier_must_match_the_flag(conn):
    s = await _set(conn)
    await _refused(conn, asyncpg.CheckViolationError, ITEM_SQL, s, "zz-x", "t2", None, "tenant", uuid.uuid4(),
                   "tune", False, "real", SHA, True, [], None)
    await _refused(conn, asyncpg.CheckViolationError, ITEM_SQL, s, "zz-x", "t2", None, "tenant", uuid.uuid4(),
                   "tune", False, "real", SHA, False, ["post_edit"], None)


@pytest.mark.asyncio
@pytest.mark.parametrize("uri,inputs", [
    ("s3:aa-cis-bronze-" + "9" * 12 + "/k.json", "{}"),                          # an account id
    ("s3://some-bucket/k.json", "{}"),                                         # a bucket URI
    ("arn:aws:s3:::x", "{}"),
    ("file:x.json", '{"brand_rules": {"t2:zz-agency": {}}}'),                   # a tenant pack at set level
])
async def test_set_refuses_account_bucket_and_tenant_packs(conn, uri, inputs):
    await _refused(conn, asyncpg.CheckViolationError,
                   "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, "
                   "imported_by, shared_inputs) VALUES ('zz919_x', '0.0.1', $1, $2, 1, 0, 'x', $3::jsonb)",
                   uri, SHA, inputs)


@pytest.mark.asyncio
async def test_one_active_version_per_name(conn):
    name = f"zz919_{uuid.uuid4().hex[:8]}"
    await _set(conn, name)
    await _refused(conn, asyncpg.UniqueViolationError,
                   "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, "
                   "imported_by) VALUES ($1, '0.0.2', 'file:x', $2, 1, 0, 'x')", name, SHA)
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = $1", name)
    await conn.execute("INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, "
                       "item_count, imported_by) VALUES ($1, '0.0.2', 'file:x', $2, 1, 0, 'x')", name, SHA)


@pytest.mark.asyncio
async def test_legacy_regression_insert_still_works(conn):
    rid = await conn.fetchval("""
        INSERT INTO shared.prompt_eval_runs (
            pipeline, prompt_version, tour_count, avg_quality_score, avg_words_per_citation,
            gate_pass_count, gate_fail_count, cost_usd, regression_detected,
            baseline_prompt_version, baseline_avg_quality_score, details, triggered_by
        ) VALUES ('s1_old', 'zz919', 3, 7.0, NULL, NULL, NULL, 0.1, false, NULL, NULL, '{}'::jsonb, 'manual')
        RETURNING id""")
    row = await conn.fetchrow("SELECT run_kind, writer, tiers, fingerprint FROM shared.prompt_eval_runs "
                              "WHERE id = $1", rid)
    assert (row["run_kind"], row["writer"], row["tiers"], row["fingerprint"]) == ("regression", None, [], "{}")


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    # a writing_memory run without a writer / with the legacy kind / without a cost
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind) "
    "VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2')",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, writer) "
    "VALUES ('writing_memory', 'x', 0, 0, '{}', 'a1')",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, details, run_kind, writer) "
    "VALUES ('writing_memory', 'x', 0, '{}', 'r2', 'a1')",
    # a legacy run with a writer-memory kind or a writer
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, run_kind) "
    "VALUES ('s1_old', 'x', 0, 'r2')",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, writer) "
    "VALUES ('s1_old', 'x', 0, 'a1')",
    # finished without stop_reason / verdict; unknown values; judge counted without calibration
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind, writer, "
    "finished_at) VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2', 'a1', now())",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind, writer, "
    "stop_reason) VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2', 'a1', 'tired')",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind, writer, "
    "judge_kappa, judge_counts) VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2', 'a1', 0.55, true)",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind, writer, "
    "tiers) VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2', 'a1', ARRAY['long_7d'])",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, run_kind, writer) "
    "VALUES ('writing_memory', 'x', 0, 0, '[]', 'r2', 'a1')",
    "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count) VALUES ('s2', 'x', 0)",
])
async def test_run_checks(conn, sql):
    await _refused(conn, asyncpg.CheckViolationError, sql)


@pytest.mark.asyncio
async def test_run_judge_counts_with_calibration_and_release_id_without_fk(conn):
    await conn.execute("INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, "
                       "run_kind, writer, judge_kappa, judge_counts, release_id, cost_cap_usd, stop_reason, verdict, "
                       "finished_at) VALUES ('writing_memory', 'x', 0, 0, '{}', 'r2', 'a1', 0.61, true, $1, 10, "
                       "'futility', 'fail', now())", uuid.uuid4())


# ── the result rules ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_checks_and_trigger(conn):
    s = await _set(conn)
    tenant = uuid.uuid4()
    blog = await _item(conn, s, tenant=tenant, split="holdout")
    edited = await _item(conn, s, writer="t2", post_edit=True)
    trap = await _item(conn, s, writer="a1", split="trap")
    base = await conn.fetchval(RUN_SQL, "baseline_import", "t9_blog", s)
    r2_t2 = await conn.fetchval(RUN_SQL, "r2", "t2", s)
    r2_blog = await conn.fetchval(RUN_SQL, "r2", "t9_blog", s)
    ok = await _result(conn, base, blog, arm="control")                                  # holdout -> holdout phase
    await _result(conn, base, blog, arm="control", mode="regenerate", output='{"x": 1}')  # baseline regen: allowed
    await _result(conn, r2_t2, edited, arm="candidate", mode="regenerate")               # a candidate on post_edit: ok
    a1_run = await conn.fetchval(RUN_SQL, "probe", "a1", s)
    await _result(conn, a1_run, trap)                                                     # trap -> probe phase
    await _result(conn, r2_blog, blog, mode="rejudge", scored=ok)                         # rejudge of the same item

    async def refused(*args, match, **kw):
        with pytest.raises(asyncpg.CheckViolationError, match=match):
            async with conn.transaction():
                await _result(conn, *args, **kw)
    await refused(base, blog, output='{"x": 1}', match="eval_item_result_output_check")      # output on score_only
    await refused(base, blog, tenant_id=uuid.uuid4(), match="must equal the item")
    await refused(base, blog, phase="tune", match="does not match the item's split")
    await refused(r2_t2, edited, arm="control", match="post_edit item")
    await refused(r2_blog, blog, arm="control", mode="regenerate", verdict="error", match="only in a baseline_import")
    await refused(r2_t2, blog, match="writing_memory run of writer t9_blog")                 # another writer's run
    await refused(base, blog, mode="rejudge", match="eval_item_result_rejudge_check")       # rejudge without target
    other = await _item(conn, s, tenant=tenant)
    await refused(r2_blog, other, mode="rejudge", scored=ok, match="same item")
    legacy = await conn.fetchval("INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count) "
                                 "VALUES ('s1_old', 'x', 0) RETURNING id")
    await refused(legacy, blog, match="writing_memory run")
    # a run of another golden set (or of none) never takes this set's items
    other_set = await conn.fetchval(RUN_SQL, "r2", "t9_blog", await _set(conn))
    no_set = await conn.fetchval(RUN_SQL, "r2", "t9_blog", None)
    await refused(other_set, blog, match="golden set must be the item's")
    await refused(no_set, blog, match="golden set must be the item's")


@pytest.mark.asyncio
async def test_a_run_with_results_keeps_its_kind_writer_and_set(conn):
    s = await _set(conn)
    i = await _item(conn, s)
    run = await conn.fetchval(RUN_SQL, "baseline_import", "t9_blog", s)
    empty = await conn.fetchval(RUN_SQL, "r2", "t9_blog", s)
    await _result(conn, run, i, arm="control")
    await _as(conn, "cis_app")
    # the bookkeeping columns stay writable (the runner closes its run)
    await conn.execute("UPDATE shared.prompt_eval_runs SET verdict = 'baseline', stop_reason = 'completed', "
                       "finished_at = now(), cost_usd = 0.5 WHERE id = $1", run)
    other = await _set(conn)
    for sql, *args in [("UPDATE shared.prompt_eval_runs SET run_kind = 'r2' WHERE id = $1", run),
                       ("UPDATE shared.prompt_eval_runs SET writer = 't2' WHERE id = $1", run),
                       ("UPDATE shared.prompt_eval_runs SET golden_set_id = $2 WHERE id = $1", run, other),
                       ("UPDATE shared.prompt_eval_runs SET golden_set_id = NULL WHERE id = $1", run)]:
        await _refused(conn, asyncpg.CheckViolationError, sql, *args, match="are frozen")
    # a run without results may still be corrected
    await conn.execute("UPDATE shared.prompt_eval_runs SET run_kind = 'r1' WHERE id = $1", empty)


@pytest.mark.asyncio
async def test_deleting_an_item_removes_its_results(conn):
    s = await _set(conn)
    i = await _item(conn, s)
    run = await conn.fetchval(RUN_SQL, "baseline_import", "t9_blog", s)
    first = await _result(conn, run, i, arm="control")
    await _result(conn, run, i, mode="rejudge", scored=first)
    await conn.execute("DELETE FROM shared.eval_golden_item WHERE item_id = $1", i)
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_item_result WHERE item_id = $1", i) == 0
    # a set with runs is never deleted by accident (RESTRICT from the run ledger)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with conn.transaction():
            await conn.execute("DELETE FROM shared.eval_golden_set WHERE set_id = $1", s)
