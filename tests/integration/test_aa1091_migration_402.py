"""AA-1091 item 5 — CIS 402 silver_aa_internal.tour_reference_price on real Postgres (conftest replays every migration).

The table, its key (one row per catalog key), its CHECKs (amount range, ISO currency shape, basis and origin values,
review pair, text lengths), the grants (cis_app SELECT / INSERT / UPDATE / DELETE; catalog_owner's SELECT comes with
403) and the registration. The writers and readers are tested in the code PR (test_aa1091_reference_price_db.py).

Invented values only; everything runs in a transaction that is rolled back."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TABLE = "silver_aa_internal.tour_reference_price"
KEY = "zz-aa1091-key-1"


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


async def _row(c, **over):
    row = {"source_key": KEY, "amount": "1450.00", "currency": "USD", "basis": "per_person", "origin": "staff",
           "updated_by": "admin:pytest", "reviewed_by": None, "reviewed_at": None, "note": None}
    row.update(over)
    await c.execute(
        f"INSERT INTO {TABLE} (source_key, amount, currency, basis, origin, updated_by, reviewed_by, reviewed_at, note) "
        "VALUES ($1, $2::text::numeric, $3, $4, $5, $6, $7, $8::text::timestamptz, $9)",
        row["source_key"], row["amount"], row["currency"], row["basis"], row["origin"], row["updated_by"],
        row["reviewed_by"], row["reviewed_at"], row["note"])


@pytest.mark.asyncio
async def test_402_registered_and_one_row_per_key(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '402'") == "402"
    await _row(conn)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():
            await _row(conn, amount="99.00")
    row = await conn.fetchrow(f"SELECT needs_review, review_reasons, created_at, updated_at FROM {TABLE} "
                              "WHERE source_key = $1", KEY)
    assert row["needs_review"] is False and row["review_reasons"] == []
    assert row["created_at"] is not None and row["updated_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"amount": "0"}, {"amount": "-5"},
    {"currency": "usd"}, {"currency": "US"}, {"currency": "DOLLAR"},
    {"basis": "per_day"}, {"basis": ""},
    {"origin": "jev"},
    {"reviewed_by": "admin:pytest"},                     # reviewed_by without reviewed_at
    {"reviewed_at": "2026-11-12T00:00:00Z"},             # reviewed_at without reviewed_by
    {"note": "x" * 501}, {"updated_by": ""}, {"source_key": ""}, {"source_key": "k" * 65},
])
async def test_402_checks_refuse(conn, over):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await _row(conn, **over)


@pytest.mark.asyncio
async def test_402_amount_out_of_range(conn):
    with pytest.raises(asyncpg.exceptions.NumericValueOutOfRangeError):
        await _row(conn, amount="10000000000")


@pytest.mark.asyncio
async def test_402_every_basis_and_origin_accepted(conn):
    for i, (basis, origin) in enumerate([("per_person", "parsed"), ("per_group", "staff"), ("from", "import")]):
        await _row(conn, source_key=f"{KEY}-{i}", basis=basis, origin=origin, reviewed_by="admin:pytest",
                   reviewed_at="2026-11-12T00:00:00Z")
    assert await conn.fetchval(f"SELECT count(*) AS ok FROM {TABLE} WHERE source_key LIKE $1", f"{KEY}-%") == 3


@pytest.mark.asyncio
async def test_402_grants(conn):
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert await conn.fetchval("SELECT has_table_privilege('cis_app', $1, $2) AS ok", TABLE, priv) is True, priv
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalog_owner') AS ok"):
        # 403 (skipped by the replay) grants catalog_owner SELECT; never a write
        assert await conn.fetchval("SELECT has_table_privilege('catalog_owner', $1, 'INSERT') AS ok", TABLE) is False
    for role in ("booking", "tripplanner", "crm_app"):
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1) AS ok", role):
            assert await conn.fetchval("SELECT has_table_privilege($1, $2, 'SELECT') AS ok", role, TABLE) is False, role
