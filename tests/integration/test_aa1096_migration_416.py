"""AA-1096 l23 — CIS 416: the operator guides (EN + VI) and plain labels of the platform settings (conftest replays
every file, 407 to 416). Registered; every key of every app (CIS and TripPlanner) has the label and guide of the
compiled defaults the admin page serves (shared/settings/compiled_defaults.json, generated from the guide block); the
CHECK refuses an incomplete guide; a re-run changes nothing; the definition guard still refuses a write by cis_app.
Runs in a transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M416 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "416_setting_definition_guides.sql")
PARTS = ("what", "raise", "lower", "when", "example")
KEY = "cis.publish.daily_limit_default"


def _body() -> str:
    return open(M416, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


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


@pytest.mark.asyncio
async def test_416_registered_and_every_row_has_the_compiled_label_and_guide(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '416'") == "416"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, label, guide::text AS guide_text, shared.setting_guide_complete(guide) AS complete "
        "FROM shared.setting_definition")}
    compiled = {d["key"]: d for d in settings.load_definitions()}
    assert set(rows) == set(compiled)
    assert {d["app"] for d in compiled.values()} >= {"cis", "tripplanner"}
    for key, row in rows.items():
        assert row["complete"] is True, key
        assert row["label"] == compiled[key]["label"], key
        assert json.loads(row["guide_text"]) == compiled[key]["guide"], key


@pytest.mark.asyncio
async def test_an_incomplete_guide_is_refused_and_null_is_allowed(conn):
    good = {lang: {p: f"{lang} text of {p}" for p in PARTS} for lang in ("en", "vi")}
    bad = {"en": good["en"], "vi": {**good["vi"], "when": " "}}
    for guide in (bad, {"en": good["en"]}, {**good, "vi": {**good["vi"], "example": 3}}):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute("UPDATE shared.setting_definition SET guide = $1::jsonb WHERE key = $2",
                                   json.dumps(guide), KEY)
    async with conn.transaction():
        await conn.execute("UPDATE shared.setting_definition SET guide = NULL WHERE key = $1", KEY)
    assert await conn.fetchval("SELECT guide IS NULL AS ok FROM shared.setting_definition WHERE key = $1", KEY)


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = _body()
    before = await conn.fetch("SELECT key, label, guide::text AS g FROM shared.setting_definition ORDER BY key")
    await conn.execute(body)
    after = await conn.fetch("SELECT key, label, guide::text AS g FROM shared.setting_definition ORDER BY key")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


@pytest.mark.asyncio
async def test_cis_app_still_cannot_write_a_guide(conn):
    """The CIS 407 definition guard covers the new column: even with the CIS 245 default DML grants (granted here,
    rolled back, as the 407 test does) cis_app cannot rewrite a label or a guide."""
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute("GRANT SELECT, UPDATE ON shared.setting_definition TO cis_app")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE cis_app")
            await conn.execute("UPDATE shared.setting_definition SET guide = NULL, label = 'Zz label' WHERE key = $1",
                               KEY)
    await conn.execute("RESET ROLE")
    assert await conn.fetchval("SELECT guide IS NOT NULL AS ok FROM shared.setting_definition WHERE key = $1", KEY)


@pytest.mark.asyncio
async def test_the_post_check_refuses_a_misspelt_key(conn):
    body = _body().replace('{"key": "cis.a1.attempts_before_review"', '{"key": "cis.zz.misspelt"', 1)
    assert "cis.zz.misspelt" in body
    with pytest.raises(asyncpg.exceptions.RaiseError, match="cis.zz.misspelt"):
        async with conn.transaction():
            await conn.execute(body)


@pytest.mark.asyncio
async def test_the_post_check_refuses_a_definition_left_without_a_guide(conn):
    await conn.execute(
        "INSERT INTO shared.setting_definition (key, app, area, label, description, value_type, default_value, "
        "impact, approval) VALUES ('cis.zz.unguided', 'cis', 'zz', 'Zz label', "
        "'A test definition the guide post-check must refuse.', 'int', '1'::jsonb, 'low', 'single')")
    with pytest.raises(asyncpg.exceptions.RaiseError, match="cis.zz.unguided"):
        async with conn.transaction():
            await conn.execute(_body())
