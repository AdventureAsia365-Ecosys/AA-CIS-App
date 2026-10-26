"""AA-919 (WM-17) — the golden-set importer's DB half (services/golden_set/store.py, importer.run) on real Postgres,
logged in as cis_app (SET ROLE), with the INVENTED fixture of tests/unit/aa919_fake_golden.py. Each test rolls back.

Covers: a DRY RUN reads only (READ ONLY transaction) and reports the plan; an apply writes the set, 24 items and one
baseline_import run per writer with its control results (post_edit items never a score_only control) and reads the
counts back; a re-run is a no-op; other bytes for the same version, a missing tenant and an offboarded tenant are
refused before anything is written; a new version supersedes the old one and an older one is refused unless
allow_downgrade; the frozen string scores are kept; the platform set row holds no tenant pack.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, X284_STANDIN_MARK, X284_PREDECESSORS  # noqa: E402
from _rls_fixture_helpers import migration_body  # noqa: E402
from services.golden_set import derive, importer, store  # noqa: E402
from tests.unit.aa919_fake_golden import TENANT_A, TENANT_B, fake_fixture  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        for version in X284_PREDECESSORS:
            await c.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                            "VALUES ($1, now(), $2) ON CONFLICT (version) DO NOTHING", version, X284_STANDIN_MARK)
        await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
        await c.execute(migration_body(284))                       # its grants (other modules revoke cis_app's)
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")   # Dev (244)
        await c.execute("GRANT SELECT ON shared.tenants TO cis_app")
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tenants(conn, cancelled=()):
    for tid in (TENANT_A, TENANT_B):
        await conn.execute("""
            INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week,
                                        cancelled_at)
            VALUES ($1::uuid, 'AA-919 synthetic', $2, 'starter', 60, true, 3,
                    CASE WHEN $3 THEN now() END)
            ON CONFLICT (tenant_id) DO UPDATE SET cancelled_at = EXCLUDED.cancelled_at""",
                           tid, f"zz919-{tid[:8]}", tid in cancelled)


def _gs(version="1.0.0", sha="e" * 64, name=None):
    gs = derive.build(fake_fixture(version), name=name or f"zz919_{uuid.uuid4().hex[:6]}", source_uri="file:fake.json",
                      source_sha256=sha, imported_by="pytest")
    return gs, derive.baseline(gs)


async def _cis_app(conn):
    await conn.execute("SET LOCAL ROLE cis_app")


@pytest.mark.asyncio
async def test_dry_run_reads_only(conn):
    await _tenants(conn)
    gs, b = _gs()
    await _cis_app(conn)
    rep = await importer.run(gs, b, apply=False, conn=conn)
    assert rep["ok"] and rep["db"]["action"] == "insert" and rep["db"]["problems"] == []
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_golden_set WHERE name = $1", gs.set_row["name"]) == 0


@pytest.mark.asyncio
async def test_apply_writes_set_items_and_one_baseline_run_per_writer(conn):
    await _tenants(conn)
    gs, b = _gs()
    await _cis_app(conn)
    rep = await importer.run(gs, b, apply=True, conn=conn)
    db = rep["db"]
    assert db["action"] == "insert" and set(db["baseline_runs"]) == {"a1", "t2", "t9_blog", "t9_social"}
    assert db["counts"]["by_writer_split"]["a1"] == {"holdout": 3, "trap": 2, "tune": 5}
    assert db["counts"]["by_writer_split"]["t9_social"] == {"holdout": 2, "trap": 1, "tune": 2}
    assert db["counts"]["by_writer_tier"]["t2"]["post_edit"] == 1
    set_id = db["set_id"]
    row = await conn.fetchrow("SELECT status, item_count, shared_inputs, holdout_pct FROM shared.eval_golden_set "
                              "WHERE set_id = $1::uuid", set_id)
    assert (row["status"], row["item_count"], str(row["holdout_pct"])) == ("active", 24, "0.300")
    assert list(json.loads(row["shared_inputs"])["brand_rules"]) == ["s1:default"]
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_golden_item WHERE set_id = $1::uuid", set_id) == 24
    # the tenant pack sits on the tenant's T2 items only
    packs = await conn.fetch("SELECT sample_id, tenant_id::text AS t FROM shared.eval_golden_item "
                             "WHERE set_id = $1::uuid AND input ? 'brand_rules'", set_id)
    assert {r["sample_id"] for r in packs} == {"t2-001", "t2-002", "t2-003", "t2-004"}
    assert {r["t"] for r in packs} == {TENANT_A}
    runs = await conn.fetch("SELECT writer, run_kind, verdict, stop_reason, finished_at IS NOT NULL AS done, "
                            "prompt_version, tour_count FROM shared.prompt_eval_runs "
                            "WHERE golden_set_id = $1::uuid ORDER BY writer", set_id)
    assert [(r["writer"], r["run_kind"], r["verdict"], r["stop_reason"], r["done"]) for r in runs] == [
        (w, "baseline_import", "baseline", "completed", True) for w in ("a1", "t2", "t9_blog", "t9_social")]
    # post_edit items are never a score_only control
    assert await conn.fetchval("""
        SELECT count(*) FROM shared.eval_item_result r JOIN shared.eval_golden_item i USING (item_id)
         WHERE i.set_id = $1::uuid AND i.post_edit AND r.arm = 'control' AND r.mode = 'score_only'""", set_id) == 0
    assert await conn.fetchval("""
        SELECT count(*) FROM shared.eval_item_result r JOIN shared.eval_golden_item i USING (item_id)
         WHERE i.set_id = $1::uuid""", set_id) == sum(len(v["results"]) for v in b.values())
    # the fixture's string scores ("7.50", "8.00") survive into the a1 / t2 frozen controls
    scores = await conn.fetch("""
        SELECT r.writer, count(*) FILTER (WHERE r.score IS NULL)::int AS missing, count(*)::int AS n
          FROM shared.eval_item_result r JOIN shared.eval_golden_item i USING (item_id)
         WHERE i.set_id = $1::uuid AND r.writer IN ('a1', 't2') GROUP BY 1 ORDER BY 1""", set_id)
    assert [(r["writer"], r["missing"]) for r in scores] == [("a1", 0), ("t2", 0)] and all(r["n"] for r in scores)


@pytest.mark.asyncio
async def test_rerun_is_a_noop_and_other_bytes_are_refused(conn):
    await _tenants(conn)
    gs, b = _gs()
    await _cis_app(conn)
    first = (await importer.run(gs, b, apply=True, conn=conn))["db"]
    again = (await importer.run(gs, b, apply=True, conn=conn))["db"]
    assert again["action"] == "exists" and again["set_id"] == first["set_id"]
    assert {w: v["action"] for w, v in again["baseline_runs"].items()} == {w: "exists" for w in first["baseline_runs"]}
    assert again["counts"] == first["counts"]
    changed, cb = _gs(sha="f" * 64, name=gs.set_row["name"])
    with pytest.raises(store.ImportRefused, match="other bytes"):
        await importer.run(changed, cb, apply=True, conn=conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "offboarded"])
async def test_missing_or_offboarded_tenant_refuses_before_writing(conn, case):
    if case == "offboarded":
        await _tenants(conn, cancelled=(TENANT_B,))
    else:
        await conn.execute("DELETE FROM shared.tenants WHERE tenant_id IN ($1::uuid, $2::uuid)", TENANT_A, TENANT_B)
    gs, b = _gs()
    await _cis_app(conn)
    dry = await importer.run(gs, b, apply=False, conn=conn)
    assert not dry["ok"] and dry["db"]["problems"]
    with pytest.raises(store.ImportRefused, match="offboarded" if case == "offboarded" else "not in shared.tenants"):
        async with conn.transaction():
            await store.apply(conn, gs, b)
    assert await conn.fetchval("SELECT count(*) FROM shared.eval_golden_set WHERE name = $1", gs.set_row["name"]) == 0


@pytest.mark.asyncio
async def test_a_new_version_supersedes_the_active_one(conn):
    await _tenants(conn)
    old, ob = _gs("1.0.0")
    await _cis_app(conn)
    first = (await importer.run(old, ob, apply=True, conn=conn))["db"]
    new, nb = _gs("1.1.0", sha="9" * 64, name=old.set_row["name"])
    second = (await importer.run(new, nb, apply=True, conn=conn))["db"]
    assert second["superseded"] == [{"set_id": first["set_id"], "version": "1.0.0"}]
    rows = await conn.fetch("SELECT version, status FROM shared.eval_golden_set WHERE name = $1 ORDER BY version",
                            old.set_row["name"])
    assert [(r["version"], r["status"]) for r in rows] == [("1.0.0", "superseded"), ("1.1.0", "active")]
    # an older version never takes the active slot by accident; --allow-downgrade does it on purpose
    older, ob2 = _gs("0.9.0", sha="8" * 64, name=old.set_row["name"])
    with pytest.raises(store.ImportRefused, match="0.9.0 is not newer than the active 1.1.0"):
        async with conn.transaction():
            await importer.run(older, ob2, apply=True, conn=conn)
    forced = (await importer.run(older, ob2, apply=True, conn=conn, allow_downgrade=True))["db"]
    assert forced["superseded"] == [{"set_id": second["set_id"], "version": "1.1.0"}]
    # re-importing the superseded 1.0.0 (same bytes) is "exists": it writes nothing and supersedes nothing
    again = await importer.run(old, ob, apply=False, conn=conn)
    assert (again["db"]["action"], again["db"]["supersedes"]) == ("exists", None)
