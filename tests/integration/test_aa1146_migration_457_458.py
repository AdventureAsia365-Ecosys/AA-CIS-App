"""AA-1146 (S285) — CIS 457 (raw_tour_revision, the draft fields of generated_content, the published_tours lock) and
CIS 458 (the content publish settings). The conftest replays every migration. Each test runs in a transaction that is
rolled back; invented tour ids, actors and text only."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M457 = os.path.join(MIG, "457_aa1146_tour_versions_expand.sql")
M458 = os.path.join(MIG, "458_aa1146_content_publish_settings.sql")
INS = ("INSERT INTO silver_aa_internal.raw_tour_revision (tour_id, revision_num, source, fields, before, after, actor, "
       "reason, resolutions) VALUES ($1::uuid, $2, $3, $4::text[], $5::jsonb, $6::jsonb, $7, $8, $9::jsonb) RETURNING id")
KEYS = ("cis.content.publish_two_person", "cis.content.t2_rerun_monthly_cap_usd", "cis.content.t2_rerun_alert_pct")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _body(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


@pytest.mark.asyncio
async def test_457_and_458_are_registered(conn):
    got = await conn.fetch("SELECT version AS v FROM shared.schema_versions WHERE version IN ('457', '458')")
    assert sorted(r["v"] for r in got) == ["457", "458"]


@pytest.mark.asyncio
async def test_457_new_columns_exist_and_are_nullable(conn):
    rows = await conn.fetch(
        "SELECT table_name AS t, column_name AS c, is_nullable AS n FROM information_schema.columns "
        "WHERE (table_schema, table_name) IN (('silver_aa_internal', 'generated_content'), "
        "('gold_aa_internal', 'published_tours')) AND column_name IN ('origin', 'parent_version_num', 'edit_reason', "
        "'author_id', 'human_lock_at', 'human_lock_by', 'human_lock_reason')")
    assert {(r["t"], r["c"]) for r in rows} == {
        ("generated_content", "origin"), ("generated_content", "parent_version_num"),
        ("generated_content", "edit_reason"), ("generated_content", "author_id"),
        ("published_tours", "human_lock_at"), ("published_tours", "human_lock_by"),
        ("published_tours", "human_lock_reason")}
    assert {r["n"] for r in rows} == {"YES"}


@pytest.mark.asyncio
async def test_raw_tour_revision_takes_a_staff_edit_and_a_merge(conn):
    tour = str(uuid.uuid4())
    assert await conn.fetchval(INS, tour, 1, "staff_edit", ["duration"], json.dumps({"duration": "3 days"}),
                               json.dumps({"duration": "4 days"}), "admin:zz-editor", None, None)
    assert await conn.fetchval(INS, tour, 2, "excel_merge", ["duration", "inclusions"],
                               json.dumps({"duration": "4 days", "inclusions": "Water"}),
                               json.dumps({"duration": "4 days", "inclusions": "Water, lunch"}), "admin:zz-editor",
                               "zz supplier sent a new sheet", json.dumps({"duration": "current"}))
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():
            await conn.fetchval(INS, tour, 2, "staff_edit", ["period"], "{}", "{}", "admin:zz-editor", None, None)


@pytest.mark.parametrize("source,fields,before,reason,resolutions", [
    ("import", ["duration"], {}, None, None),                       # unknown source
    ("staff_edit", [], {}, None, None),                              # no field
    ("staff_edit", ["duration"], [], None, None),                    # before is not an object
    ("staff_edit", ["duration"], {}, "zz", None),                    # reason too short
    ("staff_edit", ["duration"], {}, None, {"duration": "current"}),  # resolutions on a staff edit
    ("excel_merge", ["duration"], {}, None, None),                   # a merge without resolutions
])
@pytest.mark.asyncio
async def test_raw_tour_revision_refuses_bad_rows(conn, source, fields, before, reason, resolutions):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.fetchval(INS, "00000000-0000-4000-8000-000000000001", 1, source, fields,
                                json.dumps(before), "{}", "admin:zz-editor", reason,
                                None if resolutions is None else json.dumps(resolutions))


@pytest.mark.asyncio
async def test_raw_tour_revision_is_append_only(conn):
    tour = str(uuid.uuid4())
    rid = await conn.fetchval(INS, tour, 1, "staff_edit", ["period"], "{}", "{}", "admin:zz-editor", None, None)
    for sql in ("UPDATE silver_aa_internal.raw_tour_revision SET reason = 'zz changed' WHERE id = $1",
                "DELETE FROM silver_aa_internal.raw_tour_revision WHERE id = $1"):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute(sql, rid)
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("TRUNCATE silver_aa_internal.raw_tour_revision")


@pytest.mark.asyncio
async def test_cis_app_may_read_and_insert_only(conn):
    privs = await conn.fetch(
        "SELECT privilege_type AS p FROM information_schema.role_table_grants WHERE grantee = 'cis_app' "
        "AND table_schema = 'silver_aa_internal' AND table_name = 'raw_tour_revision'")
    assert {r["p"] for r in privs} >= {"SELECT", "INSERT"}
    assert not {r["p"] for r in privs} & {"UPDATE", "DELETE"}


@pytest.mark.asyncio
async def test_458_rows_are_the_compiled_defaults(conn):
    rows = await conn.fetch(
        "SELECT key, value_type, default_value::text AS d, area, approval, enforced, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = ANY($1::text[])",
        list(KEYS))
    by = {r["key"]: r for r in rows}
    assert set(by) == set(KEYS)
    for k in KEYS:
        d = settings.definition(k)
        assert by[k]["value_type"] == d.value_type and json.loads(by[k]["d"]) == d.default_value
        assert by[k]["area"] == "content" and by[k]["guided"] is True and by[k]["enforced"] is d.enforced
    assert json.loads(by["cis.content.publish_two_person"]["d"]) is False
    assert by["cis.content.publish_two_person"]["approval"] == "maker_checker"
    assert by["cis.content.publish_two_person"]["enforced"] is True
    assert json.loads(by["cis.content.t2_rerun_monthly_cap_usd"]["d"]) == 10
    assert json.loads(by["cis.content.t2_rerun_alert_pct"]["d"]) == 80


@pytest.mark.asyncio
async def test_457_and_458_re_run_change_nothing(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body(M458))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    # 457 creates its function without OR REPLACE (an expand file): a re-run stops there, nothing else changes.
    with pytest.raises(asyncpg.exceptions.DuplicateFunctionError):
        async with conn.transaction():
            await conn.execute(_body(M457))
