"""AA-884 — audit trail read side: the Python canonical hash, the chain verifier and the list query (no DB)."""
import hashlib
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import platform_audit as pa

T0 = datetime(2026, 10, 19, 1, 2, 3, 456789, tzinfo=timezone.utc)


def _chain(n, start_seq=1, prev=None):
    rows = []
    for i in range(n):
        r = {"id": uuid.UUID(int=i + 1), "tenant_id": None, "actor": "admin:x", "actor_type": "staff",
             "action": "staff.request", "resource_type": "http_route", "resource_id": f"/admin/x/{i}",
             "details_text": '{"auth": "cognito"}', "created_at": T0, "http_method": "POST",
             "route": "/admin/x/{id}", "status": 200, "chain_seq": start_seq + i, "prev_hash": prev}
        r["row_hash"] = pa.row_hash(r)
        prev = r["row_hash"]
        rows.append(r)
    return rows


def _pool(rows):
    pool = MagicMock()

    async def fetch(sql, after, limit):
        return [r for r in rows if r["chain_seq"] > after][:limit]

    pool.fetch = AsyncMock(side_effect=fetch)
    pool.fetchval = AsyncMock(return_value=3)
    return pool


def test_canonical_text_matches_the_migration_order():
    r = _chain(1)[0]
    text = pa.canonical_text(r)
    assert text == ("|00000000-0000-0000-0000-000000000001|1||admin:x|staff|staff.request|http_route|/admin/x/0|"
                    '{"auth": "cognito"}|2026-10-19T01:02:03.456789Z|POST|/admin/x/{id}|200')
    assert pa.row_hash(r) == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.asyncio
async def test_verify_ok():
    out = await pa.verify_chain(_pool(_chain(5)))
    assert out["ok"] is True and out["checked"] == 5 and out["last_seq"] == 5


@pytest.mark.asyncio
async def test_verify_detects_changed_content():
    rows = _chain(4)
    rows[2]["details_text"] = '{"auth": "forged"}'
    out = await pa.verify_chain(_pool(rows))
    assert out["ok"] is False and out["first_bad_seq"] == 3 and "content" in out["problem"]


@pytest.mark.asyncio
async def test_verify_detects_a_deleted_row():
    rows = _chain(5)
    del rows[2]
    out = await pa.verify_chain(_pool(rows))
    assert out["ok"] is False and out["first_bad_seq"] == 3 and "missing" in out["problem"]


@pytest.mark.asyncio
async def test_verify_detects_a_broken_link_and_a_wrong_start():
    rows = _chain(3)
    rows[1]["prev_hash"] = "0" * 64
    rows[1]["row_hash"] = pa.row_hash(rows[1])  # self-consistent row, wrong link
    out = await pa.verify_chain(_pool(rows))
    assert out["ok"] is False and "prev_hash" in out["problem"]
    out = await pa.verify_chain(_pool(_chain(2, start_seq=4, prev="f" * 64)))
    assert out["ok"] is False and "expected 1" in out["problem"]


@pytest.mark.asyncio
async def test_verify_from_seq_and_batches(monkeypatch):
    monkeypatch.setattr(pa, "VERIFY_BATCH", 2)
    rows = _chain(7)
    out = await pa.verify_chain(_pool(rows), from_seq=3)
    assert out["ok"] is True and out["checked"] == 5 and out["from_seq"] == 3


@pytest.mark.asyncio
async def test_verify_cached_counts_pre_chain_rows():
    class R:
        def __init__(self):
            self.d = {}

        async def get(self, k):
            return self.d.get(k)

        async def set(self, k, v, ex=None):
            self.d[k] = v

    redis = R()
    pool = _pool(_chain(2))
    first = await pa.verify_chain_cached(pool, redis)
    second = await pa.verify_chain_cached(pool, redis)
    assert first["pre_chain_rows"] == 3 and first["_cached"] is False and second["_cached"] is True
    assert (await pa.verify_chain_cached(pool, redis, from_seq=2))["_cached"] is False  # partial: never cached


def test_list_query_binds_every_filter_and_escapes_the_prefix():
    sql, args = pa.build_list_query(scope="staff", actor="admin:1", action="staff_x%", resource_type="http_route",
                                    route="/admin/x/{id}", since=T0, limit=10)
    assert "a.actor_type::text IN ('staff', 'machine') OR a.actor LIKE 'admin:%'" in sql
    assert args[:4] == ["admin:1", "staff\\_x\\%%", "http_route", "/admin/x/{id}"]
    assert args[-1] == 11  # one extra row tells whether there is a next page
    assert "password_hash" not in sql and "cognito_" not in sql
    sql_all, _ = pa.build_list_query(scope="all")
    assert "actor_type::text IN" not in sql_all
    with pytest.raises(ValueError):
        pa.build_list_query(scope="tenants")


def test_cursor_round_trip_and_garbage():
    c = pa.encode_cursor(T0, uuid.UUID(int=5))
    assert pa.decode_cursor(c) == (T0, str(uuid.UUID(int=5)))
    with pytest.raises(ValueError):
        pa.decode_cursor("not-a-cursor")
