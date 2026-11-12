"""AA-1091 item 5 — /admin/reference-prices through the real app with a fake pool: who reaches it (customer_success
reads and writes, content reads only, support never), the list / save / accept / parse / export / import shapes, the
import refusal while a line has an error (nothing saved), the audit rows, and 503 before CIS 401. Invented data only."""
from __future__ import annotations

import base64
import io
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest
from openpyxl import Workbook

from api.main import app
from api.routers import admin
from tests.unit.test_aa1091_reference_price import FakeConn, _row

_SECRET = "aa1091-test-admin-secret"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RouteConn(FakeConn):
    def __init__(self, rows, fail=None):
        super().__init__(rows)
        self.audit = []
        self.fail = fail

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        if self.fail:
            raise self.fail
        return await super().fetch(sql, *args)

    async def execute(self, sql, *args):
        if "acp_shared.audit_log" in sql:
            self.audit.append(args)
            return "INSERT 0 1"
        return await super().execute(sql, *args)


@pytest.fixture
def make_client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "redis", None, raising=False)

    def make(conn):
        pool = MagicMock()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool.acquire = acquire
        monkeypatch.setattr(app.state, "pool", pool, raising=False)
        return TestClient(app, raise_server_exceptions=True)
    return make


def _h(role="customer_success"):
    return {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa1091-staff", "x-admin-role": role}


def _rows():
    return [_row("a", name="Invented Alpha Loop"), _row("b", name="Invented Beta Walk", price_raw="On request"),
            _row("c", name="Invented Gamma Camp", amount=Decimal("900"), origin="parsed", needs_review=True)]


def test_list_for_customer_success_and_content_but_not_support(make_client):
    client = make_client(RouteConn(_rows()))
    r = client.get("/admin/reference-prices?status=no_price", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["counts"]["needs_review"] == 1
    assert {i["source_key"] for i in body["items"]} == {"a", "b"}
    assert client.get("/admin/reference-prices", headers=_h("content")).status_code == 200
    assert client.get("/admin/reference-prices", headers=_h("support")).status_code == 403
    assert client.patch("/admin/reference-prices/a", json={"amount": 1, "currency": "USD", "basis": "from"},
                        headers=_h("content")).status_code == 403
    assert client.get("/admin/reference-prices?status=bogus", headers=_h()).status_code == 422


def test_save_accept_clear_and_their_audit_rows(make_client):
    conn = RouteConn(_rows())
    client = make_client(conn)
    r = client.patch("/admin/reference-prices/a", json={"amount": "1,500", "currency": "usd", "basis": "per person",
                                                         "note": "High season"}, headers=_h())
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["saved"]["amount"] == "1500.00"
    assert client.patch("/admin/reference-prices/a", json={"amount": 0, "currency": "USD", "basis": "from"},
                        headers=_h()).status_code == 422
    assert client.patch("/admin/reference-prices/nope", json={"amount": 5, "currency": "USD", "basis": "from"},
                        headers=_h()).status_code == 404
    r = client.post("/admin/reference-prices/accept", json={"source_keys": ["c", "b"]}, headers=_h())
    assert r.json() == {"confirmed": ["c"], "skipped": [{"source_key": "b", "reason": "nothing_to_accept"}]}
    assert client.delete("/admin/reference-prices/c", headers=_h()).json() == {"source_key": "c", "cleared": True}
    actions = [a[2] for a in conn.audit]
    assert actions == ["reference_price.saved", "reference_price.confirmed", "reference_price.cleared"]
    assert all(a[1] == "admin:aa1091-staff" and a[3] == "tour_reference_price" for a in conn.audit)


def test_parse_dry_run_writes_nothing(make_client):
    conn = RouteConn(_rows())
    client = make_client(conn)
    r = client.post("/admin/reference-prices/parse", json={"dry_run": True}, headers=_h())
    assert r.status_code == 200 and r.json()["would_create"] == 1 and not conn.audit
    r = client.post("/admin/reference-prices/parse", json={"dry_run": False}, headers=_h())
    assert r.json()["created"] == 1 and [a[2] for a in conn.audit] == ["reference_price.parsed"]


def _file(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def test_export_and_import_with_dry_run(make_client):
    conn = RouteConn(_rows())
    client = make_client(conn)
    r = client.get("/admin/reference-prices/export?status=all", headers=_h())
    assert r.status_code == 200 and r.json()["rows"] == 3 and r.json()["filename"].endswith(".xlsx")
    assert base64.b64decode(r.json()["content_base64"])[:2] == b"PK"
    good = _file([["Key", "Amount", "Currency", "Basis"], ["a", 1450, "USD", "per_person"], ["b", 990, "USD", "from"]])
    bad = _file([["Key", "Amount", "Currency", "Basis"], ["a", 1450, "USD", "per_person"], ["zz", 1, "USD", "from"]])
    r = client.post("/admin/reference-prices/import", json={"file_base64": good}, headers=_h())
    assert r.status_code == 200 and r.json()["dry_run"] is True and r.json()["summary"]["changes"] == 2
    assert conn.rows["a"]["amount"] is None and not conn.audit
    r = client.post("/admin/reference-prices/import", json={"file_base64": bad, "dry_run": False}, headers=_h())
    assert r.status_code == 422 and r.json()["detail"]["summary"]["errors"] == 1
    assert conn.rows["a"]["amount"] is None
    r = client.post("/admin/reference-prices/import", json={"file_base64": good, "dry_run": False}, headers=_h())
    assert r.json()["applied"] == 2 and conn.rows["b"]["origin"] == "import"
    assert [a[2] for a in conn.audit] == ["reference_price.imported"] * 2
    assert client.post("/admin/reference-prices/import", json={"file_base64": "!!"}, headers=_h()).status_code == 400
    not_xlsx = base64.b64encode(b"plain text").decode()
    assert client.post("/admin/reference-prices/import", json={"file_base64": not_xlsx},
                       headers=_h()).status_code == 422


def test_503_before_cis_401(make_client):
    client = make_client(RouteConn([], fail=asyncpg.exceptions.UndefinedTableError("missing")))
    r = client.get("/admin/reference-prices", headers=_h())
    assert r.status_code == 503 and "CIS 401" in r.json()["detail"]
