"""AA-756 — admin Jev credit top-ups API: GET estimated-balance math, POST validation, DELETE.

Covers:
  - GET /admin/decisions/jev-credit: total = sum(amount_usd), spend read since the first top-up,
    estimated_left = total − spend; an empty table returns nulls (never an error).
  - POST /admin/decisions/jev-credit/topups: amount_usd must be > 0 (422 otherwise); writes an
    audit_log row.
  - DELETE /admin/decisions/jev-credit/topups/{id}: 404 on an unknown id; writes an audit_log row.
Mock-only: no DB, no TypeSafe call."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routers import admin_decisions as m
from api.routers.admin_decisions import TopupCreate

_NOW = datetime(2026, 10, 10, 8, 9, tzinfo=timezone.utc)


class _FakePool:
    """Serves rows per SQL text and records every write. transaction() + acquire() mimic asyncpg so
    the POST/DELETE routes (which open `async with pool.acquire()` + `conn.transaction()`) run."""

    def __init__(self, *, topups=None, spent=0.0, last_alert=None):
        self._topups = topups or []
        self._spent = spent
        self._last_alert = last_alert
        self.executed = []        # (sql, args) for every execute() — includes audit_log inserts

    # -- read helpers (GET uses pool.fetch / pool.fetchrow directly) --
    async def fetch(self, sql, *args):
        if "FROM shared.jev_credit_topup" in sql:
            return self._topups
        return []

    async def fetchrow(self, sql, *args):
        if "coalesce(sum(cost_usd)" in sql:
            # _JEV_SPEND_SQL: 0 when first_on ($1) is None, else the configured spend.
            return {"spent": self._spent if args[0] is not None else 0.0}
        if "max(created_at) AS last_at" in sql:
            return {"last_at": self._last_alert}
        return None

    # -- write path (POST/DELETE) --
    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False

        return _Tx()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


def _req(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(m, "verify_admin_secret", lambda _s: None)


# ── GET math ──────────────────────────────────────────────────────────────────────────────────

def test_get_empty_table_returns_nulls_not_error():
    pool = _FakePool(topups=[], spent=0.0)
    out = asyncio.run(m.jev_credit(_req(pool), x_admin_secret="x"))
    assert out["topups"] == []
    assert out["total_topped_up_usd"] is None
    assert out["first_topup_on"] is None
    assert out["spent_since_first_topup_usd"] is None
    assert out["estimated_left_usd"] is None
    assert out["last_exhausted_alert_at"] is None


def test_get_computes_total_spend_and_estimated_left():
    # Rows come back newest-first (ORDER BY topped_up_on DESC); first_topup_on is the LAST row.
    topups = [
        {"id": 2, "topped_up_on": date(2026, 10, 10), "amount_usd": 10.0, "note": None,
         "created_by": "admin:x", "created_at": _NOW},
        {"id": 1, "topped_up_on": date(2026, 10, 4), "amount_usd": 10.0, "note": "first",
         "created_by": "admin:x", "created_at": _NOW},
    ]
    pool = _FakePool(topups=topups, spent=14.54, last_alert=_NOW)
    out = asyncio.run(m.jev_credit(_req(pool), x_admin_secret="x"))
    assert out["total_topped_up_usd"] == 20.0
    assert out["first_topup_on"] == "2026-10-04"
    assert out["spent_since_first_topup_usd"] == 14.54
    assert round(out["estimated_left_usd"], 2) == 5.46
    assert out["last_exhausted_alert_at"] == _NOW.isoformat()
    # top-up date + created_at are ISO strings in the payload.
    assert out["topups"][0]["topped_up_on"] == "2026-10-10"
    assert isinstance(out["topups"][0]["created_at"], str)


# ── POST validation ─────────────────────────────────────────────────────────────────────────

def test_post_rejects_non_positive_amount():
    with pytest.raises(ValidationError):
        TopupCreate(topped_up_on=date(2026, 10, 10), amount_usd=0)
    with pytest.raises(ValidationError):
        TopupCreate(topped_up_on=date(2026, 10, 10), amount_usd=-5)


def test_post_accepts_positive_amount_and_writes_audit():
    created = {"id": 7, "topped_up_on": date(2026, 10, 10), "amount_usd": 10.0, "note": "x",
               "created_by": "admin:u1", "created_at": _NOW}

    class _PostPool(_FakePool):
        async def fetchrow(self, sql, *args):
            if "INSERT INTO shared.jev_credit_topup" in sql:
                return created
            return await super().fetchrow(sql, *args)

    pool = _PostPool()
    body = TopupCreate(topped_up_on=date(2026, 10, 10), amount_usd=10.0, note="x")
    out = asyncio.run(m.create_jev_topup(body, _req(pool), x_admin_secret="x", x_admin_user_id="u1"))
    assert out["id"] == 7 and out["topped_up_on"] == "2026-10-10"
    # an audit_log row was written for the add.
    assert any("INSERT INTO acp_shared.audit_log" in sql for sql, _ in pool.executed)


# ── DELETE ──────────────────────────────────────────────────────────────────────────────────

def test_delete_unknown_id_is_404():
    class _DelPool(_FakePool):
        async def fetchrow(self, sql, *args):
            if "DELETE FROM shared.jev_credit_topup" in sql:
                return None
            return await super().fetchrow(sql, *args)

    pool = _DelPool()
    with pytest.raises(HTTPException) as e:
        asyncio.run(m.delete_jev_topup(999, _req(pool), x_admin_secret="x", x_admin_user_id="u1"))
    assert e.value.status_code == 404


def test_delete_existing_writes_audit_and_returns_deleted():
    row = {"id": 3, "topped_up_on": date(2026, 10, 4), "amount_usd": 10.0}

    class _DelPool(_FakePool):
        async def fetchrow(self, sql, *args):
            if "DELETE FROM shared.jev_credit_topup" in sql:
                return row
            return await super().fetchrow(sql, *args)

    pool = _DelPool()
    out = asyncio.run(m.delete_jev_topup(3, _req(pool), x_admin_secret="x", x_admin_user_id="u1"))
    assert out == {"id": 3, "deleted": True}
    assert any("INSERT INTO acp_shared.audit_log" in sql for sql, _ in pool.executed)
