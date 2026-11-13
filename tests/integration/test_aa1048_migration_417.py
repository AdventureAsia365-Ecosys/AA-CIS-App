"""AA-1048 (S279 lane l22) — CIS 417: the five limits of the claim-instrument evaluation in shared.setting_definition
(conftest replays 407, 416 and 417). Registered; the rows equal the compiled defaults the reader serves
(shared/settings/compiled_defaults.json, generated from the seed); enforced is false until the evaluation code ships;
the go / no-go bars need evidence on a change; each key carries its label and a complete guide; a re-run changes
nothing. Runs in a transaction that is rolled back."""
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
M417 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "417_aa1048_instrument_eval_settings.sql")
KEYS = {"cis.spend.instrument_eval_cap_usd": 1.5, "cis.spend.instrument_eval_max_claims": 50,
        "cis.spend.instrument_eval_trial_claims": 3, "cis.quality.instrument_eval_fp_drop_min_pct": 60,
        "cis.quality.instrument_eval_tp_keep_min_pct": 90}
BARS = {"cis.quality.instrument_eval_fp_drop_min_pct", "cis.quality.instrument_eval_tp_keep_min_pct"}


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
async def test_417_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '417'") == "417"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, approval, evidence_required, max_value, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = ANY($1)", list(KEYS))}
    assert set(rows) == set(KEYS)
    for key, default in KEYS.items():
        assert json.loads(rows[key]["default_value"]) == default == settings.definition(key).default_value
        assert rows[key]["enforced"] is False and settings.definition(key).enforced is False
        assert rows[key]["approval"] == "maker_checker"
        assert rows[key]["evidence_required"] is (key in BARS)
        # every key ships its plain label and complete EN + VI guide (CIS 416 column and check function)
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label
    # the evaluation cap can never pass the R1 harness hard cap (services/ground_truth/r1.HARD_CAP_USD)
    assert float(rows["cis.spend.instrument_eval_cap_usd"]["max_value"]) == 2


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M417, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
