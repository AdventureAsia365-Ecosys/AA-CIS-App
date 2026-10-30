"""AA-922 (WM-30 / WM-31) — CIS 314 and the weekly KPI job on real Postgres (every migration replayed by conftest).
Nothing calls a model. Each test runs in one transaction that is rolled back.

  * the file applied: RLS on and forced (keep, disagreement) with exactly three policies; the tenant pool holds no
    privilege; the report table is a platform table;
  * a DRY RUN computes every KPI block on the real tables and stores / moves nothing; two applied weekly runs store
    two reports (one scheduled report per week);
  * a soft stop (re-flag over 8% on >= 20 writes since activation) rolls a tenant release back to its previous one,
    with an audit row; a keep for that code leaves it serving;
  * an active rule past its expiry with no fresh signal expires; one with a fresh signal of its cluster is
    re-confirmed for 60 days;
  * a T9 piece the gates passed but a human labelled reject lands in the disagreement queue once, and resolves in one
    click.
"""
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X287_PREDECESSORS, X287_STANDIN_MARK  # noqa: E402

from services.writing_memory import disagreements as dis  # noqa: E402
from services.writing_memory import kpi  # noqa: E402
from services.writing_memory import releases as rel  # noqa: E402
from services.writing_memory import rules as rules_mod  # noqa: E402
from services.writing_memory import signals as wm  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ADMIN = "11111111-1111-1111-1111-111111111111"
SHA = "e" * 64
FAILED = json.dumps([{"gate": "F2_lexicon", "passed": False, "blocking": True}])
PASSED = json.dumps([{"gate": "F2_lexicon", "passed": True, "blocking": True}])


class _OneConnPool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self, timeout=None):
        yield self.conn

    async def fetchval(self, *a):
        return await self.conn.fetchval(*a)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X287_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X287_STANDIN_MARK)
        await c.execute("DELETE FROM shared.writing_memory_kpi_report")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenant(c):
    tid = uuid.uuid4()
    await c.execute("INSERT INTO shared.tenants (tenant_id, name, slug, posts_per_week) VALUES ($1, $2, $3, 3)",
                    tid, f"AA-922x {tid.hex[:8]}", f"zz922x-{tid.hex[:8]}")
    return str(tid)


async def _piece(c, tid, ledger=PASSED, status="approved", created=None):
    req = await c.fetchval("INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, channel, status) "
                           "VALUES ($1::uuid, 'aa922x-atom', 'blog', 'approved') RETURNING request_id", tid)
    return str(await c.fetchval(
        "INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number, content_text, "
        "status, gate_ledger, repair_log, writer_input, created_at) VALUES ($1::uuid, $2, 1, 'Body.', $3, $4::jsonb, "
        "'[]'::jsonb, '{\"channel\": \"blog\"}'::jsonb, coalesce($5, now())) RETURNING piece_id",
        tid, req, status, ledger, created))


async def _run(conn, writer, kind, verdict="pass", details=None, release=None, golden=None):
    return str(await conn.fetchval(
        "INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, details, triggered_by, "
        "run_kind, writer, golden_set_id, stop_reason, verdict, finished_at, release_id) "
        "VALUES ('writing_memory', 'zz922', 0, 0, $1::jsonb, 'pytest', $2, $3, $4, 'completed', $5, now(), $6::uuid) "
        "RETURNING id", json.dumps(details or {}), kind, writer, golden, verdict, release))


async def _golden(conn):
    await conn.execute("UPDATE shared.eval_golden_set SET status = 'superseded', superseded_at = now() "
                       "WHERE name = 'aa729' AND status = 'active'")
    return await conn.fetchval(
        "INSERT INTO shared.eval_golden_set (name, version, source_uri, source_sha256, seed, item_count, imported_by) "
        "VALUES ('aa729', $2, 'file:zz.json', $1, 729, 0, 'pytest') RETURNING set_id", SHA,
        f"9.{uuid.uuid4().int % 9000 + 1000}.{uuid.uuid4().int % 9000 + 1000}")


async def _release0(conn, writer, tenant=None):
    golden = await _golden(conn)
    r0 = await _run(conn, writer, "r0_replay", details={"inputs": 20, "byte_diffs": 0})
    base = await _run(conn, writer, "baseline_import", "baseline", golden=golden)
    d = await rel.create_draft(conn, writer=writer, scope="tenant" if tenant else "platform", tenant_id=tenant,
                               created_by=f"admin:{ADMIN}")
    await rel.attach_evidence(conn, d["release_id"], r0_run_id=r0, eval_run_id=base, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    return await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


async def _rule(c, tid, writer="t9_blog", cluster="t9_blog|reason:ai_phrasing", status="approved"):
    rid = await rules_mod.insert_candidate(
        c, kind="writer_instruction", scope="tenant", tenant_id=tid, writer=writer, cluster_key=cluster,
        rule_text="Cut any sentence that only restates the heading.", lint={"ok": True})
    await c.execute("UPDATE shared.writing_rule SET status = $2, approved_by = $3, approved_at = now() - interval "
                    "'70 days' WHERE rule_id = $1::uuid", rid, status, ADMIN)
    return rid


async def _rules_release(conn, tid):
    await _release0(conn, "t9_blog", tenant=tid)
    rid = await _rule(conn, tid, cluster=f"zz922:{uuid.uuid4().hex[:8]}")
    d = await rel.create_draft(conn, writer="t9_blog", scope="tenant", tenant_id=tid, rule_ids=[rid],
                               created_by=f"admin:{ADMIN}")
    ev = await _run(conn, "t9_blog", "r2", release=d["release_id"], details={"candidate": "release"})
    sh = await _run(conn, "t9_blog", "shadow", release=d["release_id"])
    await rel.attach_evidence(conn, d["release_id"], eval_run_id=ev, shadow_run_id=sh, actor=f"admin:{ADMIN}")
    await rel.approve(conn, d["release_id"], approved_by=ADMIN, approval_ref="pytest")
    return await rel.activate(conn, d["release_id"], actor=f"admin:{ADMIN}")


# ── the file ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["writing_memory_keep", "writing_memory_disagreement"])
async def test_314_applied_rls_forced_no_tenant_privilege(conn, table):
    assert await conn.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = '314'"), \
        "conftest swallowed an error in 314 (see its log)"
    rls = await conn.fetchrow("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
                              f"shared.{table}")
    assert rls["relrowsecurity"] and rls["relforcerowsecurity"]
    pols = sorted(r["policyname"] for r in await conn.fetch(
        "SELECT policyname FROM pg_policies WHERE schemaname = 'shared' AND tablename = $1", table))
    assert pols == sorted([f"{table}_cis_app_all", f"{table}_cis_owner_all", "tenant_isolation"])
    assert not await conn.fetchval("SELECT has_table_privilege('aa_app_user', $1, 'SELECT')", f"shared.{table}")


# ── the report ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_measures_everything_and_stores_nothing_then_two_weeks_store_two_reports(conn):
    t = await _tenant(conn)
    for i in range(3):
        await _piece(conn, t, FAILED if i == 0 else PASSED)
    pool = _OneConnPool(conn)
    week = kpi.week_of(datetime.now(timezone.utc).date())
    dry = await kpi.run_weekly(pool, week_start=week, apply=False)
    blog = dry["kpis"]["writers"]["t9_blog"]
    assert blog["overall"]["writes"] == 3 and blog["overall"]["flagged"] == 1
    assert blog["tenants"][t]["reflag_rate"] == round(1 / 3, 4)
    assert set(dry["kpis"]["writers"]) == set(kpi.WRITERS) and "learning_spend" in dry["kpis"]
    assert await conn.fetchval("SELECT count(*) FROM shared.writing_memory_kpi_report") == 0
    one = await kpi.run_weekly(pool, week_start=week - timedelta(days=7), apply=True, trigger="schedule")
    two = await kpi.run_weekly(pool, week_start=week, apply=True, trigger="schedule")
    again = await kpi.run_weekly(pool, week_start=week, apply=True, trigger="schedule")
    assert one["stored"] and two["stored"] and not again["stored"]           # one scheduled report per week
    reports = await kpi.list_reports(conn)
    assert [r["week_start"] for r in reports] == [week.isoformat(), (week - timedelta(days=7)).isoformat()]
    assert reports[0]["kpis"]["writers"]["t9_blog"]["overall"]["writes"] == 3


# ── soft stops ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reflag_soft_stop_rolls_back_unless_kept(conn):
    t = await _tenant(conn)
    rel_row = await _rules_release(conn, t)
    for _ in range(24):
        await _piece(conn, t, FAILED)
    pool = _OneConnPool(conn)
    dry = await kpi.run_weekly(pool, apply=False)
    mine = [a for a in dry["actions"] if a["release_id"] == rel_row["release_id"]][0]
    assert mine["action"] == "dry_run" and "reflag_over_8pct" in mine["codes"]
    assert (await rel.get_release(conn, rel_row["release_id"]))["status"] == "active"

    keep = await kpi.record_keep(conn, rel_row["release_id"], reason="a known gate bug this week",
                                 actor=f"admin:{ADMIN}", codes=["reflag_over_8pct"], days=7)
    kept = await kpi.run_weekly(pool, apply=True, trigger="manual")
    mine = [a for a in kept["actions"] if a["release_id"] == rel_row["release_id"]][0]
    assert mine["action"] == "kept" and (await rel.get_release(conn, rel_row["release_id"]))["status"] == "active"

    await conn.execute("DELETE FROM shared.writing_memory_keep WHERE keep_id = $1::uuid", keep["keep_id"])
    out = await kpi.run_weekly(pool, apply=True, trigger="manual")
    mine = [a for a in out["actions"] if a["release_id"] == rel_row["release_id"]][0]
    assert mine["action"] == "rolled_back"
    after = await rel.get_release(conn, rel_row["release_id"])
    assert after["status"] == "retired" and after["retired_reason"].startswith("rollback: soft_stop: reflag_over_8pct")
    assert await conn.fetchval("SELECT count(*) FROM acp_shared.audit_log WHERE resource_id = $1 "
                               "AND action = 'writing_memory.release_soft_stop'", rel_row["release_id"]) == 1


@pytest.mark.asyncio
async def test_a_keep_needs_an_active_release_and_a_bounded_time(conn):
    t = await _tenant(conn)
    r0 = await _release0(conn, "t9_blog", tenant=t)
    with pytest.raises(kpi.KeepError, match="1 to 30 days"):
        await kpi.record_keep(conn, r0["release_id"], reason="why not", actor="admin:x", days=31)
    with pytest.raises(kpi.KeepError, match="codes must be"):
        await kpi.record_keep(conn, r0["release_id"], reason="why not", actor="admin:x", codes=["zz"])


# ── rule expiry ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_active_rule_expires_unless_a_fresh_signal_reconfirms_it(conn):
    t = await _tenant(conn)
    stale = await _rule(conn, t, cluster="t9_blog|reason:voice_off_brand", status="active")
    fresh = await _rule(conn, t, cluster="t9_blog|reason:ai_phrasing", status="active")
    await conn.execute("UPDATE shared.writing_rule SET expires_at = now() - interval '1 day' "
                       "WHERE rule_id = ANY($1::uuid[])", [stale, fresh])
    await wm.record_writer_feedback(
        conn, writer="t9_blog", scope="tenant", tenant_id=t, channel="blog", before_text="A truly epic view.",
        after_text="A view.", reason_code="ai_phrasing", action="tenant_edit", actor_type="tenant",
        actor_id=f"tenant:{t}", source_table="content_piece", source_id=str(uuid.uuid4()), fact_status="pass")
    dry = await kpi.expire_rules(conn, apply=False)
    assert stale in dry["expired"] and fresh in dry["reconfirmed"]
    assert await conn.fetchval("SELECT status FROM shared.writing_rule WHERE rule_id = $1::uuid", stale) == "active"
    out = await kpi.expire_rules(conn, apply=True)
    assert stale in out["expired"]
    rows = {r["rule_id"]: r for r in await conn.fetch(
        "SELECT rule_id::text AS rule_id, status, expires_at > now() + interval '59 days' AS renewed "
        "FROM shared.writing_rule WHERE rule_id = ANY($1::uuid[])", [stale, fresh])}
    assert rows[stale]["status"] == "expired" and rows[fresh]["status"] == "active" and rows[fresh]["renewed"]


# ── the disagreement queue ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_passed_piece_labelled_reject_is_queued_once_and_resolved(conn):
    t = await _tenant(conn)
    piece = await _piece(conn, t, PASSED)
    held = await _piece(conn, t, FAILED, status="held")
    ok = await _piece(conn, t, PASSED)
    for pid, label in ((piece, "reject"), (held, "light"), (ok, "as_is")):
        await conn.execute("INSERT INTO acp_shared.content_piece_label (piece_id, labelled_by, label, defects) "
                           "VALUES ($1::uuid, 'pytest', $2, '{}'::jsonb)", pid, label)
    week = kpi.week_of(datetime.now(timezone.utc).date())
    out = await dis.fill_queue(conn, week, apply=True)
    assert out["added"] == 2 and out["below_min"] is True
    assert (await dis.fill_queue(conn, week, apply=True))["added"] == 0
    items = await dis.list_items(conn)
    by_ref = {i["source_ref"]: i for i in items if i["tenant_id"] == t}
    assert by_ref[piece]["machine_verdict"] == "passed" and by_ref[piece]["human_label"] == "reject"
    assert by_ref[held]["machine_verdict"] == "held" and ok not in by_ref
    done = await dis.resolve(conn, by_ref[piece]["item_id"], resolution="human_right", actor=f"admin:{ADMIN}",
                             note="  the gate missed an invented price  ")
    assert done["status"] == "resolved" and done["note"] == "the gate missed an invented price"
    with pytest.raises(dis.DisagreementError, match="already resolved"):
        await dis.resolve(conn, by_ref[piece]["item_id"], resolution="unclear", actor=f"admin:{ADMIN}")
