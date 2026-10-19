"""S243 (AA-858 / AA-884) against real Postgres.

- ``platform_cost.ai_spend_by_provider`` (the legacy overview's month-to-date AI per provider) groups every bedrock*
  provider as "bedrock" and its total equals ``cost_by_app(...)["totals"]["ai_usd"]`` for the same month — the two
  pages show one AI number (finding 5);
- ``staff_reconcile.load_directory`` + ``readiness_from_rows`` (the live readiness of the Staff page) counts only
  active rows, so a disabled account drops out at once (finding 3).
Rows are tagged and deleted afterwards; no Redis, so no Cost Explorer call.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.services import platform_cost  # noqa: E402
from services import staff_reconcile as sr  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest.mark.asyncio
async def test_ai_spend_by_provider_matches_cost_by_app(pool):
    tag = f"s243-{uuid.uuid4().hex[:8]}"
    before = await platform_cost.ai_spend_by_provider(pool, now=NOW)
    rows = [("bedrock-satellite", "acc3", 1.25), ("bedrock", "acc2", 0.5), ("openai", None, 0.25),
            ("typesafe", None, 0.125)]
    try:
        for provider, account, cost in rows:
            await pool.execute(
                "INSERT INTO shared.llm_call_log (stage, role, model, provider, account, cost_usd, quality_signal, "
                "created_at) VALUES ('adhoc_s243', 'writer', 'm', $1, $2, $3, $4::jsonb, now())",
                provider, account, cost, f'{{"app": "cis", "test": "{tag}"}}')
        after = await platform_cost.ai_spend_by_provider(pool, now=NOW)
        delta = {k: round(after["by_provider"].get(k, 0) - before["by_provider"].get(k, 0), 4)
                 for k in ("bedrock", "openai", "other")}
        assert delta == {"bedrock": 1.75, "openai": 0.25, "other": 0.125}
        full = await platform_cost.cost_by_app(pool, None, redis=None, now=NOW)
        assert abs(full["totals"]["ai_usd"] - after["total_usd"]) <= 0.001     # rounding per row vs per family
        cis = next(r for r in full["apps"] if r["app"] == "cis")
        assert cis["ai_outside_aws_usd"] == round(cis["ai_usd"] - cis["ai_in_aws_acc2_usd"], 4)
    finally:
        await pool.execute("DELETE FROM shared.llm_call_log WHERE quality_signal ->> 'test' = $1", tag)


@pytest.mark.asyncio
async def test_live_readiness_drops_a_disabled_account(pool):
    name = f"s243-{uuid.uuid4().hex[:8]}"
    try:
        await pool.execute(
            "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active) "
            "VALUES ($1::uuid, $2, 'x', 'admin', true)", str(uuid.uuid4()), name)
        rows, _ = await sr.load_directory(pool)
        live = sr.readiness_from_rows(rows)
        assert name in live["missing_email"] and name in live["missing_cognito_sub"]
        await pool.execute("UPDATE shared.admin_users SET is_active = false WHERE username = $1", name)
        rows, _ = await sr.load_directory(pool)
        after = sr.readiness_from_rows(rows)
        assert name not in after["missing_email"] and after["active_rows"] == live["active_rows"] - 1
    finally:
        await pool.execute("DELETE FROM shared.admin_users WHERE username = $1", name)
