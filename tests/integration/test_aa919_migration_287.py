"""AA-919 (WM-18 / WM-20) — CIS 287 (writing-memory releases) and the release code on real Postgres.

conftest replays 287 like every file (with stand-in 284-286 registry rows where missing). Each test runs in one
transaction that is rolled back.

  * the file applied and a re-run is a no-op; guard 1 refuses while 284, 285 or 286 is unregistered and over other
    columns; RLS on and forced on release and shadow with exactly three policies; exact grants; the tenant pool has no
    privilege; the four writer rows are seeded with memory off;
  * the double check of ADR 0010 decision 1: the API mirror (releases.activate) and the DB (trigger + CHECK, also with
    the API bypassed by a direct UPDATE as cis_app) refuse the same activation;
  * activate / supersede / rollback through services/writing_memory/releases (audit rows), the rollback drill: the
    next write after a rollback resolves and stamps the previous release (T9 writer_input, T2 job result);
  * tenant A's release never resolves for tenant B; erasing a release another names as previous sets it NULL; a rule
    a release holds is not expired; the reverify scan flags a changed golden set; release 0 from a replay report.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
from conftest import (DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X287_PREDECESSORS,  # noqa: E402
                      X287_STANDIN_MARK)
from _rls_fixture_helpers import NO_PRIVILEGE_WITHOUT_RLS_SQL_V3, migration_body  # noqa: E402

from services.writing_memory import drill, insertion, release0  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402
from services.writing_memory import rules as rules_mod  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SHA = "c" * 64
ADMIN = "11111111-1111-1111-1111-111111111111"
AGENT = "agent:pytest-aa919"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X287_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X287_STANDIN_MARK)
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _as(conn, role, guc=None):
    # other modules re-create the roles or revoke ALL from cis_app (X1 teardown): re-apply 287 for its grants
    await conn.execute(f"GRANT USAGE ON SCHEMA shared TO {role}")
    await conn.execute(migration_body(287))
    if role == "cis_app":     # Dev's grants on the run ledger and the rule store (CIS 244 / 280)
        await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")
        await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.writing_rule TO cis_app")
    await conn.execute(f"SET LOCAL ROLE {role}")
    if guc is not None:
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", guc)


async def _refused(conn, exc, sql, *args, match=None):
    with pytest.raises(exc, match=match):
        async with conn.transaction():
            await conn.execute(sql, *args)


async def _golden(conn):
    """An active aa729 golden set (the existing one superseded inside the rolled-back transaction)."""
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = 'aa729' AND status = 'active'")
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, imported_by) "
        "VALUES ('aa729', '9.' || (floor(random() * 9000) + 1000)::int || '.' || (floor(random() * 9000) + 1000)::int, 'file:zz.json', $1, 729, 0, 'pytest') "
        "RETURNING set_id", SHA)


async def _run(conn, writer, kind, verdict="pass", details=None, finished=True, golden=None, release=None,
               stop_reason="completed"):
    return str(await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, golden_set_id, stop_reason, verdict, finished_at, release_id) "
        "VALUES ('writing_memory', 'zz919', 0, 0, $1::jsonb, 'pytest', $2, $3, $4, $5, $6, "
        "CASE WHEN $7 THEN now() END, $8::uuid) RETURNING id",
        json.dumps(details or {}), kind, writer, golden, stop_reason, verdict, finished, release))


async def _evidence(conn, writer, golden=None):
    golden = golden or await _golden(conn)
    r0 = await _run(conn, writer, "r0_replay", details={"inputs": 20, "byte_diffs": 0})
    base = await _run(conn, writer, "baseline_import", "baseline", golden=golden)
    return r0, base


async def _release0(conn, writer, *, memory_on=True, tenant=None):
    r0, base = await _evidence(conn, writer)
    d = await rel.create_draft(conn, writer=writer, scope="tenant" if tenant else "platform", tenant_id=tenant,
                               created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    out = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    if memory_on and not tenant:
        await rel.set_memory_enabled(conn, writer, True, actor=f"admin:{ADMIN}")
    return out


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_287_applied_and_rerun_is_a_noop(conn):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '287'"), \
        "conftest swallowed an error in 287 (see its log)"
    await _release0(conn, "t9_blog")            # guard 3 probes t9_social: its rows must not collide
    before = await conn.fetchval("SELECT count(*) FROM shared.writing_memory_release")
    await conn.execute(migration_body(287))
    assert await conn.fetchval("SELECT count(*) FROM shared.writing_memory_release") == before
    assert await conn.fetchval("SELECT memory_enabled FROM shared.writing_memory_writer WHERE writer = 't9_blog'")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", X287_PREDECESSORS)
async def test_guard_requires_every_predecessor(conn, missing):
    await conn.execute("DELETE FROM shared.schema_versions WHERE version = $1", missing)
    await _refused(conn, asyncpg.RaiseError, migration_body(287), match=f"migration 287: {missing} not registered")


@pytest.mark.asyncio
async def test_guard_refuses_a_rerun_over_other_columns(conn):
    await conn.execute("ALTER TABLE shared.writing_memory_release ADD COLUMN zz919 text")
    await _refused(conn, asyncpg.RaiseError, migration_body(287), match="other columns")


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["writing_memory_release", "writing_memory_shadow"])
async def test_rls_forced_with_exactly_three_policies(conn, table):
    assert tuple(await conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                     f"WHERE oid = 'shared.{table}'::regclass")) == (True, True)
    rows = await conn.fetch("SELECT policyname, roles::text[] AS roles, cmd FROM pg_policies "
                            "WHERE schemaname = 'shared' AND tablename = $1 ORDER BY policyname", table)
    assert sorted((r["policyname"], r["roles"][0], r["cmd"]) for r in rows) == sorted([
        (f"{table}_cis_app_all", "cis_app", "ALL"), (f"{table}_cis_owner_all", "cis_owner", "ALL"),
        ("tenant_isolation", "aa_app_user", "ALL")])


@pytest.mark.asyncio
async def test_privileges_are_exact_and_the_tenant_pool_has_none(conn):
    for t in ("writing_memory_release", "writing_memory_shadow", "writing_memory_writer"):
        await conn.execute(f"GRANT SELECT ON shared.{t} TO catalog_owner, PUBLIC, aa_app_user")
        await conn.execute(f"GRANT TRUNCATE, REFERENCES ON shared.{t} TO cis_app")
    await conn.execute(migration_body(287))
    want = {"writing_memory_release": {"SELECT", "INSERT", "UPDATE", "DELETE"},
            "writing_memory_shadow": {"SELECT", "INSERT", "DELETE"}, "writing_memory_writer": {"SELECT", "UPDATE"}}
    for t, privs in want.items():
        rows = await conn.fetch(f"""
            SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END AS r, a.privilege_type AS p
              FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
             WHERE c.oid = 'shared.{t}'::regclass
               AND (a.grantee = 0 OR a.grantee IN ('cis_app'::regrole, 'aa_app_user'::regrole,
                                                   'catalog_owner'::regrole))""")
        assert sorted((r["r"], r["p"]) for r in rows) == sorted(("cis_app", p) for p in privs), t
    bad = [r for r in await conn.fetch(NO_PRIVILEGE_WITHOUT_RLS_SQL_V3) if "writing_memory" in r["rel"]]
    assert bad == []
    tenant = str(uuid.uuid4())
    await _as(conn, "aa_app_user", tenant)
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "SELECT 1 FROM shared.writing_memory_release")
    await _refused(conn, asyncpg.InsufficientPrivilegeError, "SELECT 1 FROM shared.writing_memory_writer")


@pytest.mark.asyncio
async def test_four_writers_seeded_with_memory_off_and_the_switch_needs_a_release(conn):
    await conn.execute("UPDATE shared.writing_memory_writer SET memory_enabled = false")
    await conn.execute("DELETE FROM shared.writing_memory_release")
    rows = await conn.fetch("SELECT writer, memory_enabled FROM shared.writing_memory_writer ORDER BY writer")
    assert [(r["writer"], r["memory_enabled"]) for r in rows] == [("a1", False), ("t2", False), ("t9_blog", False),
                                                                  ("t9_social", False)]
    with pytest.raises(rel.ReleaseError, match="needs an active platform release"):
        await rel.set_memory_enabled(conn, "t2", True, actor=f"admin:{ADMIN}")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE shared.writing_memory_writer SET memory_enabled = true, updated_by = 'x' WHERE writer = 't2'")


# ── the double check (ADR 0010 decision 1) ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_and_db_both_refuse_an_activation_without_evidence(conn):
    r0_bad = await _run(conn, "t2", "r0_replay", details={"inputs": 20, "byte_diffs": 1})
    _, base = await _evidence(conn, "t2")
    d = await rel.create_draft(conn, writer="t2", created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0_bad, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    # the API half: a readable 409
    with pytest.raises(rel.ReleaseError) as e:
        await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    assert e.value.code == "r0_not_byte_identical"
    # the DB half, with the API bypassed: a direct UPDATE as the runtime role
    await _as(conn, "cis_app")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE shared.writing_memory_release SET status = 'active' WHERE release_id = $1::uuid",
                   d["release_id"], match="0 byte diffs")
    await conn.execute("RESET ROLE")
    # and the CHECK alone (trigger off, as the owner): an active empty release without R0 cannot exist
    await conn.execute("ALTER TABLE shared.writing_memory_release DISABLE TRIGGER writing_memory_release_guard")
    await _refused(conn, asyncpg.CheckViolationError,
                   "UPDATE shared.writing_memory_release SET status = 'active', activated_at = now(), "
                   "r0_run_id = NULL WHERE release_id = $1::uuid", d["release_id"],
                   match="writing_memory_release_evidence_check")
    await conn.execute("ALTER TABLE shared.writing_memory_release ENABLE TRIGGER writing_memory_release_guard")


@pytest.mark.asyncio
async def test_db_refusal_is_reported_as_db_refused_when_the_mirror_passes(conn, monkeypatch):
    r0, base = await _evidence(conn, "t2")
    d = await rel.create_draft(conn, writer="t2", created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    await conn.execute("UPDATE shared.prompt_eval_runs SET details = '{\"inputs\": 20, \"byte_diffs\": 2}' "
                       "WHERE id = $1::uuid", r0)

    async def blind(*a, **k):                       # a buggy API mirror that sees nothing wrong
        return None
    monkeypatch.setattr(rel, "check", blind)
    with pytest.raises(rel.DBRefused):
        async with conn.transaction():
            await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


@pytest.mark.asyncio
async def test_a_rules_release_is_not_wired_yet_and_needs_shadow(conn):
    await _release0(conn, "t9_blog")
    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at) VALUES ('writer_instruction', 'platform', 't9_blog', 'zz919-w', 'Open on the place.', "
        "'approved', 'x', now()) RETURNING rule_id::text")
    d = await rel.create_draft(conn, writer="t9_blog", rule_ids=[rule], created_by=f"admin:{ADMIN}")
    assert (d["content_class"], d["train_kind"], d["release_no"]) == ("rules", "regular", 1)
    r2 = await _run(conn, "t9_blog", "r2", release=d["release_id"], details={"candidate": "release"})
    await rel.attach_evidence(conn, d["release_id"], eval_run_id=r2, actor=f"admin:{ADMIN}")
    assert (await rel.get_release(conn, d["release_id"]))["status"] == "draft"        # no shadow yet
    sh = await _run(conn, "t9_blog", "shadow", release=d["release_id"])
    await rel.attach_evidence(conn, d["release_id"], shadow_run_id=sh, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    with pytest.raises(rel.ReleaseError) as e:
        await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    assert e.value.code == "content_not_wired"
    # the evidence itself is complete: without the wiring policy it activates and takes the month's train slot
    out = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}", enforce_wiring=False)
    assert out["status"] == "active" and out["train_month"] is not None


@pytest.mark.asyncio
async def test_attach_refuses_an_eval_or_shadow_run_of_another_release_or_none(conn):
    await _release0(conn, "t9_blog")
    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at) VALUES ('writer_instruction', 'platform', 't9_blog', 'zz919-l', 'Open on the place.', "
        "'approved', 'x', now()) RETURNING rule_id::text")
    d = await rel.create_draft(conn, writer="t9_blog", rule_ids=[rule], created_by=f"admin:{ADMIN}")
    other = await rel.create_draft(conn, writer="t9_blog", created_by=f"admin:{ADMIN}")
    code_train = await _run(conn, "t9_blog", "r2", details={"candidate": "current"})          # release_id NULL
    seeded = await _run(conn, "t9_blog", "r2", release=d["release_id"], details={"seeded_fault": "qa_trace"})
    theirs = await _run(conn, "t9_blog", "r2", release=other["release_id"])
    shadow_none = await _run(conn, "t9_blog", "shadow")
    for kw, code in (({"eval_run_id": code_train}, "eval_wrong_release"),
                     ({"eval_run_id": seeded}, "eval_seeded_fault"),
                     ({"eval_run_id": theirs}, "eval_wrong_release"),
                     ({"shadow_run_id": shadow_none}, "shadow_wrong_release")):
        with pytest.raises(rel.ReleaseError) as e:
            await rel.attach_evidence(conn, d["release_id"], actor=f"admin:{ADMIN}", **kw)
        assert e.value.status == 409 and e.value.code == code


@pytest.mark.asyncio
async def test_292_db_refuses_an_activation_on_runs_of_no_release(conn):
    """CIS 292, the DB half: with the API bypassed (evidence written directly), a rules release whose r2 / shadow run
    names no release does not activate; its own runs do."""
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute(migration_body(287))                 # the fixture DB may hold another module's grants
    await conn.execute(migration_body(292))
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = '292'") == 1
    await conn.execute("UPDATE shared.writing_memory_writer SET train_closed_month = NULL WHERE writer = 't9_blog'")
    tenant = str(uuid.uuid4())
    rule = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, tenant_id, writer, cluster_key, rule_text, status, approved_by, "
        "approved_at, support_tenants) VALUES ('writer_instruction', 'tenant', $1::uuid, 't9_blog', 'zz919-m', "
        "'Open on the place.', 'approved', 'x', now(), 1) RETURNING rule_id::text", tenant)
    r2 = await _run(conn, "t9_blog", "r2", details={"candidate": "current"})
    sh = await _run(conn, "t9_blog", "shadow")
    rid = await conn.fetchval(
        "INSERT INTO shared.writing_memory_release (writer, scope, tenant_id, release_no, train_kind, content_class, "
        "rule_ids, items_sha256, created_by, eval_run_id, shadow_run_id) VALUES ('t9_blog', 'tenant', $1::uuid, 1, "
        "'regular', 'rules', ARRAY[$2::uuid], $3, 'pytest', $4::uuid, $5::uuid) RETURNING release_id::text",
        tenant, rule, SHA, r2, sh)
    await conn.execute("UPDATE shared.writing_memory_release SET status = 'evaluated' WHERE release_id = $1::uuid", rid)
    await conn.execute("UPDATE shared.writing_memory_release SET status = 'approved', approved_by = 'pytest', "
                       "approved_at = now() WHERE release_id = $1::uuid", rid)
    activate = "UPDATE shared.writing_memory_release SET status = 'active' WHERE release_id = $1::uuid"
    await _refused(conn, asyncpg.CheckViolationError, activate, rid, match="not an r2 run of this release")
    await conn.execute("UPDATE shared.prompt_eval_runs SET release_id = $1::uuid, details = '{\"candidate\": "
                       "\"release\"}' WHERE id = $2::uuid", rid, r2)
    await _refused(conn, asyncpg.CheckViolationError, activate, rid, match="not a shadow run of this release")
    await conn.execute("UPDATE shared.prompt_eval_runs SET release_id = $1::uuid WHERE id = $2::uuid", rid, sh)
    await conn.execute(activate, rid)
    assert await conn.fetchval("SELECT status FROM shared.writing_memory_release WHERE release_id = $1::uuid",
                               rid) == "active"


# ── activate, supersede, rollback and the drill ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_activate_supersede_rollback_and_audit(conn):
    first = await _release0(conn, "t2")
    assert first["release_no"] == 0 and first["previous_release_id"] is None
    r0, base = await _evidence(conn, "t2")
    d = await rel.create_draft(conn, writer="t2", created_by=f"admin:{ADMIN}")
    assert d["release_no"] == 1 and d["content_class"] == "empty"
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    with pytest.raises(rel.ReleaseError, match="signed-in admin"):
        await rel.approve(conn, d["release_id"], approved_by="job:writing_memory_mine")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    second = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    assert second["previous_release_id"] == first["release_id"]
    assert (await rel.get_release(conn, first["release_id"]))["retired_reason"] == "superseded"
    with pytest.raises(rel.ReleaseError) as e:
        await rel.rollback(conn, first["release_id"], actor=f"admin:{ADMIN}", reason="x")
    assert e.value.code == "not_active"
    back = await rel.rollback(conn, second["release_id"], actor=f"admin:{ADMIN}", reason="bad tone")
    assert back["release_id"] == first["release_id"] and back["status"] == "active"
    gone = await rel.get_release(conn, second["release_id"])
    assert gone["status"] == "retired" and gone["retired_reason"] == "rollback: bad tone"
    with pytest.raises(rel.ReleaseError) as e:                       # release 0 has nowhere to go back to
        await rel.rollback(conn, first["release_id"], actor=f"admin:{ADMIN}", reason="x")
    assert e.value.code == "no_previous"
    actions = [r["action"] for r in await conn.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE resource_type = 'writing_memory_release' "
        "AND resource_id = ANY($1::text[]) ORDER BY created_at, chain_seq NULLS LAST",
        [first["release_id"], second["release_id"]])]
    for a in ("writing_memory.release_created", "writing_memory.release_evidence", "writing_memory.release_approved",
              "writing_memory.release_activated", "writing_memory.release_rolled_back"):
        assert a in actions, (a, actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["t9_social", "t2"])
async def test_rollback_drill_next_write_uses_the_previous_release(conn, writer):
    await _release0(conn, writer)
    tenant = str(uuid.uuid4())
    out = await drill.rollback_drill(conn, writer=writer, tenant_id=tenant, actor=AGENT, approval_ref="pytest-ref")
    assert out["next_write_uses_previous"] is True
    assert out["release_numbers"] == [0, 1]
    assert out["stamp_before_rollback"]["tenant"]["release_no"] == 1
    assert out["stamp_after_rollback"]["tenant"]["release_no"] == 0
    assert out["stamp_after_rollback"]["platform"]["release_no"] == 0


@pytest.mark.asyncio
async def test_a1_run_stamps_the_release_the_v2_writer_wrote_with(conn):
    """The next A1 job after a rollback: admin_pipeline resolves the view, the v2 writer consumes and stamps it."""
    from scripts.writing_memory import wm20_release0_replay as replay
    from tests.unit import aa919_release0_fakes as fakes

    first = await _release0(conn, "a1")
    r0, base = await _evidence(conn, "a1")
    d = await rel.create_draft(conn, writer="a1", created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    second = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    inp = fakes.a1_inputs(1)[0]
    stamp = replay.capture_a1(inp, await insertion.resolve(conn, "a1"))["consumed"]
    assert stamp["platform"]["release_id"] == second["release_id"] and stamp["writer_version"] == "v2"
    await rel.rollback(conn, second["release_id"], actor=f"admin:{ADMIN}", reason="drill")
    stamp = replay.capture_a1(inp, await insertion.resolve(conn, "a1"))["consumed"]
    assert stamp["platform"]["release_id"] == first["release_id"] and stamp["platform"]["release_no"] == 0
    await rel.set_memory_enabled(conn, "a1", False, actor=f"admin:{ADMIN}")
    assert await insertion.resolve(conn, "a1") is None                  # the kill switch: no stamp, no memory


# ── isolation, erasure, expiry, reverify ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_a_release_never_resolves_for_tenant_b(conn):
    await _release0(conn, "t9_blog")
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ta = await _release0(conn, "t9_blog", tenant=a)
    await _as(conn, "cis_app")                                   # the writers' role: USING (true), filter in code
    view_a = await insertion.resolve(conn, "t9_blog", a)
    view_b = await insertion.resolve(conn, "t9_blog", b)
    assert view_a["tenant"]["release_id"] == ta["release_id"]
    assert view_b["tenant"] is None and view_b["platform"] is not None
    rule_b = await conn.fetchval(
        "INSERT INTO shared.writing_rule (kind, scope, tenant_id, writer, cluster_key, rule_text, status, "
        "support_tenants) VALUES ('writer_instruction', 'tenant', $1::uuid, 't9_blog', 'zz919-b', 'Keep it short.', "
        "'candidate', 1) RETURNING rule_id::text", b)
    await conn.execute("RESET ROLE")
    with pytest.raises(rel.ReleaseError) as e:
        await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=a, rule_ids=[rule_b],
                               created_by=f"admin:{ADMIN}")
    assert e.value.code == "rule_isolation"


@pytest.mark.asyncio
async def test_erasing_a_previous_release_sets_the_pointer_null(conn):
    await _release0(conn, "t2")
    tenant = str(uuid.uuid4())
    t0 = await _release0(conn, "t2", tenant=tenant)
    r0, base = await _evidence(conn, "t2")
    d = await rel.create_draft(conn, writer="t2", scope="tenant", tenant_id=tenant, created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN)
    t1 = await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")
    assert t1["previous_release_id"] == t0["release_id"]
    await _as(conn, "cis_app")
    # the AA-839 erasure deletes the tenant's releases (shadow rows first, then releases)
    await conn.execute("DELETE FROM shared.writing_memory_release WHERE release_id = $1::uuid", t0["release_id"])
    assert await conn.fetchval("SELECT previous_release_id FROM shared.writing_memory_release "
                               "WHERE release_id = $1::uuid", t1["release_id"]) is None
    assert await conn.fetchval("DELETE FROM shared.writing_memory_release WHERE tenant_id = $1::uuid "
                               "RETURNING 1", tenant) == 1


@pytest.mark.asyncio
async def test_expiry_skips_a_rule_a_release_holds(conn):
    ids = []
    for k in ("h", "f"):
        ids.append(await conn.fetchval(
            "INSERT INTO shared.writing_rule (kind, scope, writer, cluster_key, rule_text, status, approved_by, "
            f"approved_at, expires_at) VALUES ('repair_hint', 'platform', 't2', 'zz919-{k}', 'Cut the date.', "
            "'approved', 'x', now(), now() - interval '1 day') RETURNING rule_id::text"))
    held, free = ids
    await _release0(conn, "t2")
    await rel.create_draft(conn, writer="t2", rule_ids=[held], created_by=f"admin:{ADMIN}")
    await rules_mod.expire_stale(conn)
    st = {r["rule_id"]: r["status"] for r in await conn.fetch(
        "SELECT rule_id::text, status FROM shared.writing_rule WHERE rule_id = ANY($1::uuid[])", [held, free])}
    assert st == {held: "approved", free: "expired"}


@pytest.mark.asyncio
async def test_reverify_scan_flags_a_changed_golden_set_and_keeps_serving(conn):
    first = await _release0(conn, "t9_social")
    assert (await rel.reverify_scan(conn)) == []
    await _golden(conn)                                   # a new golden-set version supersedes the old one
    flagged = await rel.reverify_scan(conn)
    assert flagged == [{"release_id": first["release_id"], "writer": "t9_social", "reasons": ["golden_set"]}]
    row = await rel.get_release(conn, first["release_id"])
    assert row["status"] == "active" and row["reverify"] and row["reverify_reasons"] == ["golden_set"]
    assert (await rel.reverify_scan(conn)) == []          # flagged once


@pytest.mark.asyncio
async def test_release0_apply_from_a_replay_report(conn):
    golden = await _golden(conn)
    for w in rel.WRITERS:
        await _run(conn, w, "baseline_import", "baseline", golden=golden)
    rows = []
    for w in rel.WRITERS:
        for i in range(20):
            rows.append({"writer": w, "input_id": f"{w}-{i}", "source": "x", "system_sha256": "s" * 64,
                         "user_sha256": f"u{i}", "none_vs_r0_equal": True, "pre_system_sha256": "s" * 64,
                         "pre_user_sha256": f"u{i}"})
    report = {"schema": release0.SCHEMA, "phase": "post", "pre_key": "s3:pre.json", "a1_v2_enabled_here": True,
              "rows": rows}
    results = release0.verdicts(report)
    assert {r["verdict"] for r in results.values()} == {"pass"}
    with pytest.raises(rel.ReleaseError, match="signed-in admin"):     # the agent needs the human decision ref
        async with conn.transaction():
            await release0.apply(conn, report, results, actor=AGENT, report_key="s3:post.json", approve=True)
    out = await release0.apply(conn, report, results, actor=AGENT, report_key="s3:post.json", approve=True,
                               approval_ref="https://example.invalid/decision", activate=True, memory_on=True)
    for w in rel.WRITERS:
        assert out[w]["status"] == "active" and out[w]["memory_enabled"] is True, out[w]
        row = await rel.get_release(conn, out[w]["release_id"])
        assert (row["release_no"], row["content_class"], row["reverify"]) == (0, "empty", True)
        assert row["approved_by"] == AGENT and row["approval_ref"]
        run = await conn.fetchrow("SELECT run_kind, verdict, cost_usd, details FROM shared.prompt_eval_runs "
                                  "WHERE id = $1::uuid", out[w]["r0_run_id"])
        assert (run["run_kind"], run["verdict"], float(run["cost_usd"])) == ("r0_replay", "pass", 0.0)
        assert json.loads(run["details"])["byte_diffs"] == 0
    # re-running reuses release 0 and writes only new r0 runs
    again = await release0.apply(conn, report, results, actor=AGENT, report_key="s3:post.json")
    assert {again[w]["release_id"] for w in rel.WRITERS} == {out[w]["release_id"] for w in rel.WRITERS}


# ── the Dev replay script's read-only queries run on the real schema ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replay_script_input_queries_run_read_only(conn):
    from scripts.writing_memory import wm20_release0_replay as replay

    async with conn.transaction(readonly=True):
        assert await conn.fetch(replay._A1_SQL, 5, None) is not None
        assert await conn.fetch(replay._A1_SQL, 5, ["x"]) == []
        assert await conn.fetch(replay._T2_SQL, 5, None) is not None
        assert await conn.fetch(replay._T9_SQL, 5, None, True) is not None
        assert await conn.fetch(replay._T9_SQL, 5, ["00000000-0000-0000-0000-000000000000"], False) == []
        assert await conn.fetchrow(replay._PT_SQL, str(uuid.uuid4())) is None
        assert isinstance(await replay._a1_brand_rules(conn), dict)
        assert await conn.fetchrow("SELECT release_id::text FROM shared.writing_memory_release WHERE writer = $1 "
                                   "AND scope = 'platform' AND release_no = 0", "a1") is None
