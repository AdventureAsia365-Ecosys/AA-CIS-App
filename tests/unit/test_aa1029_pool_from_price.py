"""AA-1029 R4 — the pool's "from" price reader (api/routers/v1_tours.py _from_prices / _with_from_prices), offline.

The price comes only from AA-Booking's contract view booking.v_from_prices (price.from.v1), read on MAIN by the catalog
key; any read error is "no price", never a failed page. Real-Postgres checks: tests/integration/
test_aa1029_pool_from_price.py.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest

from api.routers import v1_tours


class _Conn:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn, self.acquired = conn, 0

    @asynccontextmanager
    async def acquire(self, timeout=None):
        self.acquired += 1
        yield self.conn


def _row(source_id, amount, **over):
    row = {"source_id": source_id, "amount": amount, "currency": "USD", "basis": "per_person", "pax": 2,
           "season": "Low", "valid_to": date(2027, 3, 31)}
    row.update(over)
    return row


def _run(coro):
    return asyncio.run(coro)


def test_sql_reads_only_the_contract_view_by_catalog_key():
    sql = " ".join(v1_tours._FROM_PRICE_SQL.split())
    assert "FROM booking.v_from_prices" in sql
    assert "source_system = 'cis' AND source_id = ANY($1::text[])" in sql
    assert "booking." not in sql.replace("booking.v_from_prices", "")


def test_prices_by_key_with_the_contract_shape():
    conn = _Conn([_row("k1", "235.00"), _row("k2", Decimal("410.50"), currency="USD ", season=None,
                                               valid_to=date(9999, 12, 31))])
    got = _run(v1_tours._from_prices(_Pool(conn), ["k2", None, "k1", "k1"]))
    assert got == {
        "k1": {"amount": "235.00", "currency": "USD", "basis": "per_person", "pax": 2, "season": "Low",
               "valid_to": "2027-03-31"},
        "k2": {"amount": "410.50", "currency": "USD", "basis": "per_person", "pax": 2, "season": None,
               "valid_to": "9999-12-31"},
    }
    assert conn.calls[0][1] == (["k1", "k2"],)    # distinct keys, None dropped


def test_no_key_no_query():
    pool = _Pool(_Conn())
    assert _run(v1_tours._from_prices(pool, [None, ""])) == {}
    assert pool.acquired == 0


def test_a_row_without_amount_is_no_price():
    assert _run(v1_tours._from_prices(_Pool(_Conn([_row("k1", None)])), ["k1"])) == {}


@pytest.mark.parametrize("error", [RuntimeError("relation booking.v_from_prices does not exist"),
                                   PermissionError("permission denied for schema booking"), TimeoutError()])
def test_any_read_error_is_no_price(error):
    assert _run(v1_tours._from_prices(_Pool(_Conn(error=error)), ["k1"])) == {}


def test_rows_get_from_price_and_lose_the_internal_key():
    rows = [{"id": "a", "catalog_source_id": "k1"}, {"id": "b", "catalog_source_id": "k2"},
            {"id": "c", "catalog_source_id": None}]
    out = _run(v1_tours._with_from_prices(_Pool(_Conn([_row("k1", "235.00")])), rows))
    assert [r["id"] for r in out] == ["a", "b", "c"]
    assert out[0]["from_price"]["amount"] == "235.00"
    assert out[1]["from_price"] is None and out[2]["from_price"] is None
    assert all("catalog_source_id" not in r for r in out)


def test_from_price_passes_the_tenant_key_rule():
    """The tenant key-walk guard (AA-796) must not strip or flag the new field."""
    from api.routers._tenant_projection import TENANT_FORBIDDEN_KEY_RE
    for key in ("from_price", *_row("k", "1").keys()):
        assert not TENANT_FORBIDDEN_KEY_RE.search(key), key
