"""AA-788 (S287) — CIS 468: the settings text of the F12 scope mode names the evidence the server accepts for block
(conftest replays every migration). Registered; the row equals the compiled default the reader serves (description and
guide), the value, default and approval rule are unchanged, the guide stays complete and says block needs the pilot
record reference; the API refuses replay evidence for block and takes it for warn; a re-run changes nothing. Runs in a
transaction that is rolled back; invented values only."""
import json
import os
import pathlib
import re
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M468 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "468_aa788_f12_scope_evidence_text.sql")
KEY = "cis.quality_modes.t10_f12_scope"
EDIT_RE = re.compile(r"\$edit\$(.*?)\$edit\$", re.DOTALL)


def _body() -> str:
    return pathlib.Path(M468).read_text(encoding="utf-8").replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


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
async def test_468_registered_and_the_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '468'") == "468"
    block = json.loads(EDIT_RE.search(pathlib.Path(M468).read_text(encoding="utf-8")).group(1))
    assert [r["key"] for r in block] == [KEY] and set(block[0]) == {"key", "description", "guide"}
    r = await conn.fetchrow(
        "SELECT description, guide::text AS g, default_value::text AS d, allowed_values::text AS av, approval, "
        "evidence_required AS ev, enforced, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = $1", KEY)
    d = settings.definition(KEY)
    assert r["description"] == d.description and json.loads(r["g"]) == settings.guide(KEY) and r["guided"] is True
    assert json.loads(r["d"]) == "warn" == d.default_value and json.loads(r["av"]) == ["warn", "block"]
    assert (r["approval"], r["ev"], r["enforced"]) == ("maker_checker", True, True)
    assert "aa788-pilot:" in d.description and "AA-788" not in d.description
    for lang in ("en", "vi"):
        assert "aa788-pilot:" in settings.guide(KEY)[lang]["when"], lang


def test_the_text_matches_the_server_rule():
    """The prefix the text names is the one the server checks, and the server refuses replay evidence for block."""
    from api.services import settings_registry as reg
    from services.quality import quality_settings
    d = settings.definition(KEY)
    assert quality_settings.F12_PILOT_EVIDENCE_PREFIX in d.description
    with pytest.raises(reg.SettingsError) as e:
        reg._check_evidence(d, "block", "replay run of stored pieces")
    assert e.value.code == "evidence_mismatch"
    assert reg._check_evidence(d, "warn", "replay run of stored pieces") == "replay run of stored pieces"
    assert reg._check_evidence(d, "block", "aa788-pilot:p202611210000abcd:0123456789ab")


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    sql = "SELECT key, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(sql)
    await conn.execute(_body())
    after = await conn.fetch(sql)
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


@pytest.mark.asyncio
async def test_the_post_check_refuses_a_misspelt_key(conn):
    body = _body().replace(f'{{"key": "{KEY}"', '{"key": "cis.quality_modes.zz_misspelt"', 1)
    with pytest.raises(asyncpg.RaiseError, match="CIS 468: no definition or not edited"):
        await conn.execute(body)
