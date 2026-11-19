"""AA-1051 S285 — the null gate's golden carry reads the stored items of two golden versions on real Postgres as cis_app
(SET ROLE), with the INVENTED fixture of tests/unit/aa919_fake_golden.py. Each test rolls back.

Covers: `null_carry.load_pair` / `read_items` run on the real tables (every compared column exists and casts), two
imports of the same items give a carry with every GATE item compared, and one changed stored label refuses it.

Needs CIS 400 (shared.eval_golden_split); a checkout without it skips this module."""
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
from services.golden_set import derive, importer  # noqa: E402
from services.writer_proof import null_carry as nc  # noqa: E402
from services.writer_proof import prereg as pr  # noqa: E402
from services.writer_proof import splits  # noqa: E402
from tests.unit.aa919_fake_golden import TENANT_A, TENANT_B, fake_fixture  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "400_eval_golden_split.sql")
pytestmark = pytest.mark.skipif(not os.path.exists(MIGRATION), reason="CIS 400 is not in this checkout yet")


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
        await c.execute(migration_body(284))
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.prompt_eval_runs TO cis_app")
        await c.execute("GRANT SELECT ON shared.tenants TO cis_app")
        await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_golden_split TO cis_app")
        await c.execute("DO $$ BEGIN IF to_regclass('shared.writing_memory_release') IS NOT NULL THEN "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON shared.writing_memory_release TO cis_app; END IF; "
                        "END $$")
        for tid in (TENANT_A, TENANT_B):
            await c.execute("""
                INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active,
                                            posts_per_week)
                VALUES ($1::uuid, 'AA-1051 synthetic', $2, 'starter', 60, true, 3)
                ON CONFLICT (tenant_id) DO NOTHING""", tid, f"zz1051c-{tid[:8]}")
        yield c
    finally:
        await tr.rollback()
        await c.close()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return _A()


class _NoTx:
    """`load_pair` opens a READ ONLY transaction; inside the test's own transaction a savepoint stands in for it."""

    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def transaction(self, readonly=False):
        return self.conn.transaction()


async def _import(c, name: str, version: str) -> None:
    gs = derive.build(fake_fixture(version), name=name, source_uri="file:fake.json", source_sha256="f" * 64,
                      imported_by="pytest")
    rep = await importer.run(gs, derive.baseline(gs), apply=True, conn=c)
    assert rep["db"]["set_id"]


def _pres(name: str):
    """An a1 measured run (s1 workflow: writer a1) on name@1.0.0 listing the fixture's a1 non-trap GATE items, and a
    proof on name@1.1.0."""
    gs = derive.build(fake_fixture("1.0.0"), name=name, source_uri="file:fake.json", source_sha256="f" * 64,
                      imported_by="pytest")
    gate = sorted(i["sample_id"] for i in gs.items
                  if i["writer"] == "a1" and not i["trap"] and splits.split_of(i["sample_id"]) == "gate")
    assert gate
    base = {k: v for k, v in pr.load("w5-s1").doc.items() if k != "sample"}
    # S285 l29: w5-s1 is registered (>= 25 ids); a draft copy takes the fixture's few GATE items
    base.update(status="draft", registered_by=None, registered_on=None)
    null = pr.from_doc({**base, "golden_set": {"name": name, "version": "1.0.0"}, "sample_ids": gate})
    proof = pr.from_doc({**base, "golden_set": {"name": name, "version": "1.1.0"}, "sample_ids": gate})
    return null, proof


@pytest.mark.asyncio
async def test_two_versions_with_the_same_items_carry_on_the_stored_rows(conn):
    name = f"zz1051c_{uuid.uuid4().hex[:6]}"
    await conn.execute("SET LOCAL ROLE cis_app")
    await _import(conn, name, "1.0.0")
    await _import(conn, name, "1.1.0")
    null, proof = _pres(name)
    old, new = await nc.load_pair(_Pool(_NoTx(conn)), null, proof)
    assert old["version"] == "1.0.0" and new["version"] == "1.1.0"
    assert {c for c in nc.ITEM_COLUMNS + nc.SPLIT_COLUMNS} <= set(old["items"][0])
    got = nc.null_covers(null, proof, old, new)
    assert got["ok"], got["differs"]
    assert got["golden_carried_from"] == "1.0.0" and got["items_compared"] >= got["sample_items"] > 0


@pytest.mark.asyncio
async def test_one_changed_stored_label_refuses_the_carry(conn):
    name = f"zz1051c_{uuid.uuid4().hex[:6]}"
    await conn.execute("SET LOCAL ROLE cis_app")
    await _import(conn, name, "1.0.0")
    await _import(conn, name, "1.1.0")
    null, proof = _pres(name)
    sid = null.doc["sample_ids"][0]
    await conn.execute("RESET ROLE")                              # cis_app has no UPDATE on the items (by design)
    await conn.execute("""
        UPDATE shared.eval_golden_item g SET label = g.label || '{"zz_note": "changed"}'::jsonb
          FROM shared.eval_golden_set s
         WHERE s.set_id = g.set_id AND s.name = $1 AND s.version = '1.1.0' AND g.sample_id = $2""", name, sid)
    await conn.execute("SET LOCAL ROLE cis_app")
    old, new = await nc.load_pair(_Pool(_NoTx(conn)), null, proof)
    got = nc.null_covers(null, proof, old, new)
    assert not got["ok"] and any(f"{sid} differs in label" in d for d in got["differs"])
    assert await nc.read_items(conn, name, "9.9.9", "a1") is None
