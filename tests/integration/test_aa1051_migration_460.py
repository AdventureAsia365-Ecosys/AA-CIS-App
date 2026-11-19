"""AA-1051 (S285 C2) — CIS 460: the T2 writer proof's D12 cap default goes from 5 to 6.5 USD (tech lead, 19/11/2026;
conftest replays every migration). Registered; the T2 cap row and the total's row equal the compiled defaults the reader
serves (default and guide), the total stays 33, descriptions, bounds and approval are unchanged, the guides stay
complete, the w5-t2 pre-registration's cap fits the default in force; a re-run changes nothing. Runs in a transaction
that is rolled back."""
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
M460 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "460_aa1051_t2_proof_cap.sql")
T2, TOTAL = "cis.spend.writer_proof_t2_cap_usd", "cis.spend.writer_proof_total_cap_usd"
EDIT_RE = re.compile(r"\$edit\$(.*?)\$edit\$", re.DOTALL)


def _block() -> list[dict]:
    return json.loads(EDIT_RE.search(pathlib.Path(M460).read_text(encoding="utf-8")).group(1))


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
async def test_460_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '460'") == "460"
    assert sorted(r["key"] for r in _block()) == sorted([T2, TOTAL])
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value::text AS d, description, guide::text AS g, min_value::float AS lo, "
        "max_value::float AS hi, approval, impact, enforced, shared.setting_guide_complete(guide) AS guided "
        "FROM shared.setting_definition WHERE key = ANY($1::text[])", [T2, TOTAL])}
    assert json.loads(rows[T2]["d"]) == 6.5 == settings.definition(T2).default_value
    assert json.loads(rows[TOTAL]["d"]) == 33 == settings.definition(TOTAL).default_value
    for key, r in rows.items():
        d = settings.definition(key)
        assert json.loads(r["g"]) == settings.guide(key) and r["guided"] is True, key
        assert r["description"] == d.description and (r["lo"], r["hi"]) == (0.01, d.max_value), key
        assert (r["approval"], r["impact"], r["enforced"]) == ("maker_checker", "high", True), key
    assert "6.5" in settings.guide(T2)["en"]["example"] and "34.5" in settings.guide(TOTAL)["en"]["example"]


def test_the_t2_pre_registration_fits_the_default_in_force():
    from services.writer_proof import prereg as pr
    pre = pr.load("w5-t2")
    assert pre.doc["cap_usd"] == 6.5 == pr.workflow_cap("t2")


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = pathlib.Path(M460).read_text(encoding="utf-8").replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    sql = "SELECT key, default_value::text AS d, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(sql)
    await conn.execute(body)
    after = await conn.fetch(sql)
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
