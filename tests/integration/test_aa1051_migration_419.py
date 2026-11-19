"""AA-1051 (S280) — CIS 419: the D12 caps and minimum samples of the W5 writer proofs in shared.setting_definition
(conftest replays every migration; CIS 460 raised the T2 cap to 6.5). Registered; the rows equal the compiled
defaults the reader serves (shared/settings/compiled_defaults.json, generated from the seed and later edits);
enforced since CIS 427 (the proof code is on main); the minimum samples need evidence on a change; each key carries
its label and a complete guide; a re-run changes nothing. Runs in a transaction that is rolled back."""
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
M419 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "419_aa1051_w5_proof_settings.sql")
# the defaults in force after every migration: CIS 460 (S285 C2, tech lead 19/11/2026) raised the T2 cap 5 -> 6.5
CAPS = {"cis.spend.writer_proof_null_run_cap_usd": 5, "cis.spend.writer_proof_t9_cap_usd": 15, "cis.spend.writer_proof_s1_cap_usd": 8,
        "cis.spend.writer_proof_t2_cap_usd": 6.5, "cis.spend.writer_proof_total_cap_usd": 33}
# the 419 seed: root ADR 0020 D12 as written (null 5, T9 15, S1 8, T2 5 = the total 33)
SEED_419 = {**CAPS, "cis.spend.writer_proof_t2_cap_usd": 5}
MINS = {"cis.quality.writer_proof_null_run_min_items": 12, "cis.quality.writer_proof_t9_min_items": 12,
        "cis.quality.writer_proof_s1_min_items": 25, "cis.quality.writer_proof_t2_min_items": 20}
KEYS = {**CAPS, **MINS}


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
async def test_419_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '419'") == "419"
    rows = {r["key"]: r for r in await conn.fetch(
        "SELECT key, default_value, enforced, approval, evidence_required, impact, label, "
        "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = ANY($1)",
        list(KEYS))}
    assert set(rows) == set(KEYS)
    for key, default in KEYS.items():
        assert json.loads(rows[key]["default_value"]) == default == settings.definition(key).default_value
        assert rows[key]["enforced"] is True and settings.definition(key).enforced is True   # CIS 427
        assert rows[key]["approval"] == "maker_checker"
        assert rows[key]["evidence_required"] is (key in MINS)
        assert rows[key]["impact"] == ("high" if key in CAPS else "medium")
        assert rows[key]["guided"] is True and rows[key]["label"] == settings.definition(key).label
    # root ADR 0020 D12: the four workflow caps of the 419 seed add up to the total; CIS 460 keeps the total at 33
    seed = open(M419, encoding="utf-8").read()
    for key, v in SEED_419.items():
        assert f'"key": "{key}"' in seed and f'"default_value": {v},' in seed.split(f'"key": "{key}"', 1)[1]
    total = "cis.spend.writer_proof_total_cap_usd"
    assert sum(v for k, v in SEED_419.items() if k != total) == SEED_419[total] == CAPS[total]


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M419, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
