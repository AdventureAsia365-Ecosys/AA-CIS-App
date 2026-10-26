"""AA-990 D1 — migration 285 (geo.component_redirect + catalog.v_component_redirect) applied the way DB Migrate
applies a cis/ file since X1 S22, and services/places/store.write_redirects on it.

conftest skips 285 in the replay (its catalog recipe needs 245's owners). This module builds on
test_aa860_migration_246's module fixture (Dev after 244 + 245, db_migrator with rl-002's memberships) and applies 285
as db_migrator under the live-owner switch (errors NOT swallowed), twice. Teardown drops what 285 created, so later
modules see the replay state.

  * the migration: registered once; named CHECKs; the view is catalog_owner's, security_barrier, readable by
    tripplanner only; the table is cis_app's (+ catalog_owner SELECT for the view), nothing for the consumers;
    CREATE on schema catalog revoked;
  * write_redirects (as cis_app): the served ids' rows are cleared, the dropped ids upserted (a second build moves a
    redirect), a chain is visible to tripplanner through the view; old_id = new_id is refused by the CHECK.
"""
import os
import sys
import uuid

import asyncpg
import psycopg2
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa860_migration_245 as t245  # noqa: E402
import test_aa860_migration_246 as t246  # noqa: E402
from test_aa860_migration_245 import rds_like_245  # noqa: E402,F401  (module fixture: Dev after 244 + 245)
from test_aa860_migration_246 import db_migrator_login  # noqa: E402,F401  (module fixture: db_migrator, 246)
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.places import store  # noqa: E402

HERE = os.path.dirname(__file__)
MIGRATION = os.path.join(HERE, "..", "..", "api", "migrations", "285_geo_component_redirect.sql")
DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


def _apply(path):
    c = t246._migrator()
    c.autocommit = True
    try:
        c.cursor().execute(t246._switched(open(path, encoding="utf-8").read()))     # raises on any error
    finally:
        c.close()


def undo_285(a):
    a.execute("DROP VIEW IF EXISTS catalog.v_component_redirect")
    a.execute("DROP TABLE IF EXISTS geo.component_redirect")
    a.execute("DELETE FROM shared.schema_versions WHERE version = '285'")
    a.execute("REVOKE CREATE ON SCHEMA catalog FROM catalog_owner")


@pytest.fixture(scope="module", autouse=True)
def applied_285(db_migrator_login):  # noqa: F811
    admin = t245._connect()
    admin.autocommit = True
    a = admin.cursor()
    try:
        undo_285(a)
        _apply(MIGRATION)
        _apply(MIGRATION)                                    # idempotent re-run
        yield
    finally:
        undo_285(a)
        admin.close()


@pytest.fixture
def cur():
    c = t245._connect()
    c.autocommit = False
    try:
        yield c.cursor()
    finally:
        c.rollback()
        c.close()


def test_registered_once_with_its_checks(cur):
    cur.execute("SELECT count(*) FROM shared.schema_versions WHERE version = '285'")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'geo.component_redirect'::regclass "
                "AND contype = 'c'")
    assert {r[0] for r in cur.fetchall()} == {"component_redirect_not_self_chk", "component_redirect_rule_chk"}


def test_view_owner_barrier_and_grants(cur):
    cur.execute("SELECT pg_get_userbyid(relowner), reloptions FROM pg_class "
                "WHERE oid = 'catalog.v_component_redirect'::regclass")
    owner, opts = cur.fetchone()
    assert owner == "catalog_owner" and "security_barrier=true" in (opts or [])
    cur.execute("SELECT has_table_privilege('tripplanner', 'catalog.v_component_redirect', 'SELECT')")
    assert cur.fetchone()[0] is True
    for role in ("booking", "aa_app_user"):
        cur.execute("SELECT has_table_privilege(%s, 'catalog.v_component_redirect', 'SELECT')", (role,))
        assert cur.fetchone()[0] is False, role
    cur.execute("SELECT has_schema_privilege('catalog_owner', 'catalog', 'CREATE')")
    assert cur.fetchone()[0] is False


def test_table_is_cis_internal(cur):
    cur.execute("SELECT has_table_privilege('cis_app', 'geo.component_redirect', 'SELECT, INSERT, UPDATE, DELETE')")
    assert cur.fetchone()[0] is True
    cur.execute("SELECT has_table_privilege('catalog_owner', 'geo.component_redirect', 'SELECT')")
    assert cur.fetchone()[0] is True
    for role in ("tripplanner", "booking", "aa_app_user"):
        cur.execute("SELECT has_table_privilege(%s, 'geo.component_redirect', 'SELECT')", (role,))
        assert cur.fetchone()[0] is False, role


def test_not_self_check(cur):
    same = str(uuid.uuid4())
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO geo.component_redirect (old_id, new_id, source_id, rule) "
                    "VALUES (%s, %s, 'src', 'spelling')", (same, same))


@pytest.mark.asyncio
async def test_write_redirects_clears_served_upserts_dropped_and_chains_through_the_view():
    a, b, c, d = (str(uuid.uuid4()) for _ in range(4))
    src = f"aa990-{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE cis_app")
            # build 1: a dropped → b
            assert await store.write_redirects(conn, src, {a: b}, {a: "spelling"}, [b]) == 0
            # build 2: b dropped → c (chain a → b → c); a is served again (the place came back): its row goes
            assert await store.write_redirects(conn, src, {b: c}, {b: "card"}, [a, c]) == 1
            # build 3: b re-dropped → d (the upsert moves it)
            await store.write_redirects(conn, src, {b: d}, {b: "destination"}, [d])
            rows = await conn.fetch("SELECT old_id::text, new_id::text, rule FROM geo.component_redirect "
                                    "WHERE source_id = $1 ORDER BY old_id", src)
            assert [tuple(r) for r in rows] == [(b, d, "destination")]
            await store.write_redirects(conn, src, {c: d}, {c: "link_name"}, [d])
            await conn.execute("SET LOCAL ROLE tripplanner")
            got = await conn.fetch("SELECT component_id::text, new_component_id::text FROM catalog.v_component_redirect "
                                   "WHERE component_id = ANY($1::uuid[]) ORDER BY component_id", [a, b, c])
            assert sorted(tuple(r) for r in got) == sorted([(b, d), (c, d)])
        finally:
            await tx.rollback()
    finally:
        await conn.close()
