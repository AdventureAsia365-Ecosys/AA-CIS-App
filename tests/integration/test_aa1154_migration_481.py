"""AA-1154 (A0) — CIS 481 on a real Postgres (conftest replays every migration): the twelve assistant settings are
registered and equal the compiled defaults (S285 advisory values, maker-checker, a complete guide); a value outside its
bounds is refused; a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_assist_settings as a  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M481 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "481_aa1154_ops_assist_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")

EXPECTED = {
    a.EVIDENCE_WINDOW_MIN: (30, "int", "minutes", (5, 240)),
    a.CHANGES_WINDOW_MIN: (120, "int", "minutes", (15, 1440)),
    a.REFUSED_TRIGGER_CALLS: (3, "int", "calls", (1, 1000)),
    a.THROTTLED_TRIGGER_CALLS: (3, "int", "calls", (1, 1000)),
    a.SPEND_SPIKE_FACTOR: (3, "numeric", "x", (1.5, 20)),
    a.SPEND_BASELINE_DAYS: (7, "int", "days", (1, 30)),
    a.SPEND_SPIKE_FLOOR_USD_H: (0.1, "usd", "USD/hour", (0, 10)),
    a.WIDE_SCOPE_STAGES: (5, "int", "stages", (1, 100)),
    a.SMALL_MONEY_USD: (0.1, "usd", "USD", (0, 10)),
    a.HISTORY_MIN_N: (5, "int", "outcomes", (3, 100)),
    a.CARD_TTL_MIN: (30, "int", "minutes", (5, 240)),
    a.REASON_MIN_CHARS: (20, "int", "characters", (10, 500)),
}


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


def test_every_reader_key_is_seeded():
    assert set(EXPECTED) == set(a.A0) and set(a.ALL) == set(a.NAMES.values())


@pytest.mark.asyncio
async def test_481_registered_and_the_keys_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '481'") == "481"
    for key, (default, vtype, unit, bounds) in EXPECTED.items():
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "low", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_value_must_stay_inside_its_bounds(conn):
    for key, bad in ((a.EVIDENCE_WINDOW_MIN, "4"), (a.EVIDENCE_WINDOW_MIN, "241"), (a.REASON_MIN_CHARS, "9"),
                     (a.SPEND_SPIKE_FACTOR, "1.2"), (a.SMALL_MONEY_USD, "11"), (a.CARD_TTL_MIN, '"30"'),
                     (a.SPEND_BASELINE_DAYS, "0"), (a.SPEND_BASELINE_DAYS, "31"), (a.SPEND_SPIKE_FLOOR_USD_H, "11"),
                     (a.THROTTLED_TRIGGER_CALLS, "0")):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, a.CARD_TTL_MIN, "15") is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M481, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetch("SELECT key, label, description, guide::text AS g FROM shared.setting_definition "
                              "ORDER BY key")
    await conn.execute(body)
    after = await conn.fetch("SELECT key, label, description, guide::text AS g FROM shared.setting_definition "
                             "ORDER BY key")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
