"""AA-814 — Facts Entry API + writer precedence.

- services/acp_shared/facts_store: field rules, subject key, patch / conflict rules, every statement names its scope.
- services/acp_content_writing/facts: pick_facts (a tenant fact replaces the platform facts on its subject; latest
  stated wins inside a scope), the seed entry names its source, the read SQL drops expired facts.
- /v1/facts through the router with the real tenant JWT check and the Content studio role middleware: tenant
  isolation (a fake tenant connection that applies the CIS 229 policies AND the statements' own filters), viewer read
  only, the tenant id never comes from the body, audit rows on the main pool.
- /admin/facts through the real app: reads open to every staff role, writes admin only (quality.write), the audit row
  is written on the same connection as the change.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import staff_authz
from api.middleware import tenant_role
from api.routers import v1_facts
from api.routers.auth import _create_partner_session_jwt
from services.acp_content_writing import facts as writer_facts
from services.acp_shared import facts_store as fs

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
T0 = datetime(2026, 10, 1, 8, 0, 0, 123456, tzinfo=timezone.utc)
FAR = date.today() + timedelta(days=365)
PAST = date.today() - timedelta(days=3)


# ── a fake acp_shared.facts behind the tenant pool ─────────────────────────────────────────────

class FakeFactsDB:
    """An in-memory acp_shared.facts. A connection opened for a tenant applies the CIS 229 policies the way Postgres
    does for aa_app_user (SELECT: platform rows + own tenant rows; INSERT / UPDATE / DELETE: own tenant rows only,
    WITH CHECK on new rows), AND evaluates the WHERE clauses facts_store writes. Every statement must name its
    scope, so a query that forgot its filter fails here even though RLS would have hidden the rows."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.statements: list[tuple[str, tuple]] = []

    def add(self, **row):
        base = {"scope": "tenant", "tenant_id": None, "kind": "other", "country": None, "stated_on": None,
                "valid_until": None, "updated_by": None, "created_at": T0, "updated_at": T0}
        base.update(row)
        if base["tenant_id"] is not None:
            base["tenant_id"] = uuid.UUID(str(base["tenant_id"]))
        self.rows[base["fact_id"]] = base
        return base

    def conn(self, session_tenant: str | None):
        return _FakeConn(self, uuid.UUID(session_tenant) if session_tenant else None)


_CLAUSES = [
    (re.compile(r"fact_id = \$(\d+)"), lambda r, v: r["fact_id"] == v),
    (re.compile(r"scope = 'tenant' AND tenant_id = \$(\d+)::uuid"),
     lambda r, v: r["scope"] == "tenant" and r["tenant_id"] == uuid.UUID(str(v))),
    (re.compile(r"kind = \$(\d+)"), lambda r, v: r["kind"] == v),
    (re.compile(r"lower\(country\) = lower\(\$(\d+)::text\)"), lambda r, v: (r["country"] or "").lower() == v.lower()),
    (re.compile(r"title ILIKE \$(\d+)"),
     lambda r, v: any(v.strip("%").lower() in (r[k] or "").lower() for k in ("title", "body", "provenance"))),
]
_PLATFORM = "scope = 'platform' AND tenant_id IS NULL"
_VALID = "valid_until IS NULL OR valid_until >= current_date"


class _FakeConn:
    def __init__(self, db: FakeFactsDB, tenant: uuid.UUID | None):
        self.db, self.tenant = db, tenant

    # policies (aa_app_user); tenant None = the main pool (cis_app, USING true)
    def _visible(self, row, write: bool) -> bool:
        if self.tenant is None:
            return True
        own = row["scope"] == "tenant" and row["tenant_id"] == self.tenant
        return own or (not write and row["scope"] == "platform")

    def _where(self, sql: str, params: tuple):
        where = sql.split("WHERE", 1)[1]
        where = re.split(r"ORDER BY|FOR UPDATE|RETURNING|LIMIT", where)[0]
        assert "scope = 'platform'" in where or "scope = 'tenant'" in where, f"unscoped statement: {sql}"
        preds = []
        if _PLATFORM in where:
            preds.append(lambda r: r["scope"] == "platform" and r["tenant_id"] is None)
        if _VALID in where:
            preds.append(lambda r: r["valid_until"] is None or r["valid_until"] >= date.today())
        for rx, fn in _CLAUSES:
            m = rx.search(where)
            if m:
                v = params[int(m.group(1)) - 1]
                preds.append(lambda r, fn=fn, v=v: fn(r, v))
        return lambda r: all(p(r) for p in preds)

    async def fetch(self, sql, *params):
        self.db.statements.append((sql, params))
        pred = self._where(sql, params)
        rows = [r for r in self.db.rows.values() if self._visible(r, False) and pred(r)]
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)

    async def fetchval(self, sql, *params):
        return len(await self.fetch(sql, *params))

    async def fetchrow(self, sql, *params):
        self.db.statements.append((sql, params))
        head = sql.strip().split()[0].upper()
        if head == "SELECT":
            pred = self._where(sql, params)
            hits = [r for r in self.db.rows.values() if self._visible(r, False) and pred(r)]
            return dict(hits[0]) if hits else None
        if head == "INSERT":
            (fid, scope, tid, kind, title, body, prov, country, stated, until, actor) = params
            row = {"fact_id": fid, "scope": scope, "tenant_id": uuid.UUID(str(tid)) if tid else None, "kind": kind,
                   "title": title, "body": body, "provenance": prov, "country": country, "stated_on": stated,
                   "valid_until": until, "updated_by": actor, "created_at": T0, "updated_at": T0}
            if not self._visible(row, True):
                raise PermissionError("new row violates row-level security policy")
            self.db.rows[fid] = row
            return dict(row)
        pred = self._where(sql, params)
        hits = [r for r in self.db.rows.values() if self._visible(r, True) and pred(r)]
        if not hits:
            return None
        row = hits[0]
        if head == "DELETE":
            return dict(self.db.rows.pop(row["fact_id"]))
        assert head == "UPDATE"
        (_, kind, title, body, prov, country, stated, until, actor) = params[:9]
        row.update(kind=kind, title=title, body=body, provenance=prov, country=country, stated_on=stated,
                   valid_until=until, updated_by=actor, updated_at=row["updated_at"] + timedelta(seconds=1))
        return dict(row)


def _token(tenant: str, role: str = "editor") -> str:
    token, _ = _create_partner_session_jwt(tenant, "T", "growth", membership_id=str(uuid.uuid4()),
                                           content_studio_role=role)
    return token


@pytest.fixture
def tenant_api(monkeypatch):
    db = FakeFactsDB()
    db.add(fact_id="fact_platvisa", scope="platform", kind="visa_entry", title="Laos visa", body="E-visa $50.",
           provenance="Embassy site", country="Laos")
    db.add(fact_id="fact_platold", scope="platform", kind="price", title="Old promo", body="10% off.",
           provenance="Ops", valid_until=PAST)
    db.add(fact_id="fact_aaown01", tenant_id=TENANT_A, kind="price", title="Mekong cruise price",
           body="From $900 per person.", provenance="Our 2027 rate sheet")
    db.add(fact_id="fact_bbown01", tenant_id=TENANT_B, kind="price", title="Mekong cruise price",
           body="From $700 per person.", provenance="B rate sheet")
    calls = []

    @asynccontextmanager
    async def fake_scoped(request, tenant_id, flag_key, *, fail_closed=True):
        calls.append((tenant_id, flag_key, fail_closed))
        yield db.conn(tenant_id), True

    monkeypatch.setattr(v1_facts, "acquire_scoped_conn", fake_scoped)
    app = FastAPI()
    app.middleware("http")(tenant_role.tenant_role_middleware)
    app.include_router(v1_facts.router)
    main_pool = MagicMock()
    main_pool.execute = AsyncMock(return_value="INSERT 0 1")   # write_audit_log(pool, ...)
    app.state.pool = main_pool
    client = TestClient(app)
    client.db, client.calls, client.audit = db, calls, main_pool.execute
    return client


def _h(tenant=TENANT_A, role="editor"):
    return {"Authorization": f"Bearer {_token(tenant, role)}"}


# ── store rules ──────────────────────────────────────────────────────────────────────────────────

def test_fields_collapse_whitespace_and_refuse_blank_or_unknown():
    f = fs.FactFields(kind="price", title="  Mekong   cruise ", body="From\n$900", provenance=" sheet ",
                      country="  ")
    assert (f.title, f.body, f.provenance, f.country) == ("Mekong cruise", "From $900", "sheet", None)
    for bad in ({"title": "   ", "body": "b", "provenance": "p"},
                {"title": "t", "body": "b", "provenance": "p", "kind": "rumour"},
                {"title": "t", "body": "b", "provenance": "p", "tenant_id": TENANT_B},
                {"title": "t", "body": "b", "provenance": "p", "scope": "platform"},
                {"title": "t", "body": "b", "provenance": "p", "stated_on": "2026-10-10", "valid_until": "2026-10-01"},
                {"title": "t" * (fs.MAX_TITLE + 1), "body": "b", "provenance": "p"}):
        with pytest.raises(ValueError):
            fs.FactFields(**bad)


def test_subject_key_ignores_case_and_spacing_but_not_kind_or_country():
    a = {"kind": "price", "title": "Mekong  Cruise price", "country": "Laos"}
    assert fs.subject_key(a) == fs.subject_key({"kind": "price", "title": "mekong cruise PRICE", "country": "laos"})
    assert fs.subject_key(a) != fs.subject_key({**a, "kind": "season"})
    assert fs.subject_key(a) != fs.subject_key({**a, "country": None})


def test_patch_conflict_noop_and_changes():
    cur = {"kind": "price", "title": "t", "body": "b", "provenance": "p", "country": None, "stated_on": None,
           "valid_until": None, "updated_at": T0}
    with pytest.raises(fs.FactConflict):
        fs.apply_patch(cur, fs.FactPatch(body="new", expected_updated_at=T0 + timedelta(seconds=1)))
    with pytest.raises(fs.FactError, match="nothing to change"):
        fs.apply_patch(cur, fs.FactPatch(body=" b ", expected_updated_at=T0))
    fields, changes = fs.apply_patch(cur, fs.FactPatch(body="new", valid_until=FAR, expected_updated_at=T0))
    assert fields.body == "new" and changes == {"body": ["b", "new"], "valid_until": [None, FAR.isoformat()]}
    # a client that dropped the offset of the UTC timestamp still matches
    naive = T0.replace(tzinfo=None)
    assert fs.apply_patch(cur, fs.FactPatch(body="x", expected_updated_at=naive))[1] == {"body": ["b", "x"]}
    with pytest.raises(fs.FactError):            # clearing a required field
        fs.apply_patch(cur, fs.FactPatch.model_validate({"title": None}))
    fields, _ = fs.apply_patch(cur | {"country": "Laos"}, fs.FactPatch.model_validate({"country": None}))
    assert fields.country is None                # clearing an optional one is allowed


@pytest.mark.asyncio
async def test_tenant_fact_limit():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fs.MAX_TENANT_FACTS)
    with pytest.raises(fs.FactError, match="at most"):
        await fs.create_fact(conn, fs.FactFields(title="t", body="b", provenance="p"), scope="tenant",
                             tenant_id=uuid.UUID(TENANT_A), actor="tenant:x")
    conn.fetchrow.assert_not_called()
    with pytest.raises(fs.FactError, match="no tenant"):
        await fs.create_fact(conn, fs.FactFields(title="t", body="b", provenance="p"), scope="platform",
                             tenant_id=uuid.UUID(TENANT_A), actor="admin:x")


# ── writer precedence + seed ─────────────────────────────────────────────────────────────────────

def _f(fid, scope, title="Mekong cruise price", kind="price", country=None, stated=None, edited=T0, **kw):
    return {"fact_id": fid, "scope": scope, "tenant_id": None, "kind": kind, "title": title, "body": f"body {fid}",
            "provenance": kw.get("provenance", f"src {fid}"), "country": country, "stated_on": stated,
            "valid_until": None, "updated_at": edited}


def test_tenant_fact_replaces_the_platform_facts_on_its_subject():
    plat = _f("fact_p1", "platform", stated=date(2027, 1, 1))
    other = _f("fact_p2", "platform", title="Laos visa", kind="visa_entry")
    own = _f("fact_t1", "tenant", title="MEKONG  cruise price", stated=date(2026, 1, 1))
    kept = writer_facts.pick_facts([plat, other, own])
    assert [f["fact_id"] for f in kept] == ["fact_p2", "fact_t1"]       # input order kept


def test_same_title_other_kind_or_country_is_another_subject():
    plat = _f("fact_p1", "platform", country="Laos")
    own_any = _f("fact_t1", "tenant", country=None)
    own_season = _f("fact_t2", "tenant", kind="season", country="Laos")
    assert len(writer_facts.pick_facts([plat, own_any, own_season])) == 3


def test_inside_a_scope_the_latest_stated_then_latest_edit_wins():
    old = _f("fact_t1", "tenant", stated=date(2026, 1, 1))
    new = _f("fact_t2", "tenant", stated=date(2026, 6, 1))
    assert [f["fact_id"] for f in writer_facts.pick_facts([old, new])] == ["fact_t2"]
    a = _f("fact_t3", "tenant", edited=T0)
    b = _f("fact_t4", "tenant", edited=T0 + timedelta(hours=1))
    assert [f["fact_id"] for f in writer_facts.pick_facts([a, b])] == ["fact_t4"]


def test_no_override_keeps_every_fact_in_order():
    facts = [_f("fact_p1", "platform", title="A"), _f("fact_p2", "platform", title="B"),
             _f("fact_t1", "tenant", title="C")]
    assert writer_facts.pick_facts(facts) == facts


def test_seed_entry_names_source_and_owner_and_keeps_the_head_line():
    block = writer_facts.format_facts_block([
        _f("fact_t1", "tenant", provenance="Our  2027 rate sheet"), _f("fact_p2", "platform", title="Laos visa")])
    first, second = block.split("\n\n")
    assert first.splitlines() == ["[Fact id=fact_t1]", "Mekong cruise price: body fact_t1",
                                  "Source: Our 2027 rate sheet; agency fact"]
    assert second.splitlines()[0] == "[Fact id=fact_p2]" and second.endswith("Source: src fact_p2; platform fact")


@pytest.mark.asyncio
async def test_writer_read_drops_expired_and_applies_precedence():
    conn = AsyncMock()
    conn.fetch.return_value = [_f("fact_p1", "platform"), _f("fact_t1", "tenant")]
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    facts = await writer_facts.fetch_facts_for_writing(uuid.UUID(TENANT_A), pool, country="Laos")
    assert [f["fact_id"] for f in facts] == ["fact_t1"]
    sql = conn.fetch.call_args.args[0]
    assert "valid_until IS NULL OR valid_until >= current_date" in sql
    assert "scope = 'tenant' AND tenant_id = $1" in sql


# ── /v1/facts: isolation, roles, audit ───────────────────────────────────────────────────────────

def test_list_shows_own_and_valid_platform_facts_only(tenant_api):
    r = tenant_api.get("/v1/facts", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["fact_id"] for f in body["own"]] == ["fact_aaown01"]
    assert {f["fact_id"] for f in body["platform"]} == {"fact_platvisa"}      # expired platform fact hidden
    assert body["kinds"] == list(fs.FACT_KINDS) and body["max_own"] == fs.MAX_TENANT_FACTS
    own = body["own"][0]
    assert "tenant_id" not in own and "updated_by" not in own
    assert tenant_api.calls[-1] == (TENANT_A, v1_facts.FACTS_FLAG, True)


def test_a_platform_fact_the_tenant_replaces_says_so(tenant_api):
    tenant_api.db.add(fact_id="fact_platmek", scope="platform", kind="price", title="Mekong Cruise Price",
                      body="From $1000.", provenance="AA")
    plat = {f["fact_id"]: f for f in tenant_api.get("/v1/facts", headers=_h()).json()["platform"]}
    assert plat["fact_platmek"]["overridden_by"] == "fact_aaown01"
    assert plat["fact_platvisa"]["overridden_by"] is None


def test_another_tenant_cannot_read_edit_or_delete_my_fact(tenant_api):
    listing = tenant_api.get("/v1/facts", headers=_h(TENANT_B)).json()
    assert [f["fact_id"] for f in listing["own"]] == ["fact_bbown01"]
    assert "fact_aaown01" not in {f["fact_id"] for f in listing["own"] + listing["platform"]}
    r = tenant_api.patch("/v1/facts/fact_aaown01", json={"body": "hijack"}, headers=_h(TENANT_B))
    assert r.status_code == 404
    assert tenant_api.delete("/v1/facts/fact_aaown01", headers=_h(TENANT_B)).status_code == 404
    assert tenant_api.db.rows["fact_aaown01"]["body"] == "From $900 per person."


def test_a_tenant_cannot_edit_or_delete_a_platform_fact(tenant_api):
    assert tenant_api.patch("/v1/facts/fact_platvisa", json={"body": "x"}, headers=_h()).status_code == 404
    assert tenant_api.delete("/v1/facts/fact_platvisa", headers=_h()).status_code == 404
    assert "fact_platvisa" in tenant_api.db.rows


def test_body_cannot_name_a_tenant_or_scope(tenant_api):
    for extra in ({"tenant_id": TENANT_B}, {"scope": "platform"}):
        r = tenant_api.post("/v1/facts", json={"kind": "price", "title": "t", "body": "b", "provenance": "p",
                                               **extra}, headers=_h())
        assert r.status_code == 422, extra


def test_create_edit_delete_own_fact_with_audit(tenant_api):
    r = tenant_api.post("/v1/facts", json={"kind": "price", "title": "Halong cruise price", "body": "From $400.",
                                           "provenance": "Our sheet", "valid_until": FAR.isoformat()}, headers=_h())
    assert r.status_code == 201, r.text
    fact = r.json()
    row = tenant_api.db.rows[fact["fact_id"]]
    assert row["scope"] == "tenant" and str(row["tenant_id"]) == TENANT_A
    assert row["updated_by"].startswith(f"tenant:{TENANT_A}:")
    audit = tenant_api.audit.call_args
    assert TENANT_A in audit.args and "fact.created" in audit.args and "Halong cruise price" in audit.args[-1]

    r = tenant_api.patch(f"/v1/facts/{fact['fact_id']}",
                         json={"body": "From $450.", "expected_updated_at": fact["updated_at"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "From $450."
    stale = tenant_api.patch(f"/v1/facts/{fact['fact_id']}",
                             json={"body": "From $500.", "expected_updated_at": fact["updated_at"]}, headers=_h())
    assert stale.status_code == 409

    assert tenant_api.delete(f"/v1/facts/{fact['fact_id']}", headers=_h()).status_code == 200
    assert fact["fact_id"] not in tenant_api.db.rows
    assert "fact.deleted" in tenant_api.audit.call_args.args


def test_audit_failure_does_not_undo_the_write(tenant_api):
    tenant_api.audit.side_effect = RuntimeError("audit down")
    r = tenant_api.post("/v1/facts", json={"title": "t", "body": "b", "provenance": "p"}, headers=_h())
    assert r.status_code == 201 and r.json()["fact_id"] in tenant_api.db.rows


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/facts", {"title": "t", "body": "b", "provenance": "p"}),
    ("PATCH", "/v1/facts/fact_aaown01", {"body": "x"}),
    ("DELETE", "/v1/facts/fact_aaown01", None),
])
def test_viewer_reads_but_never_writes(tenant_api, method, path, body):
    assert tenant_api.get("/v1/facts", headers=_h(role="viewer")).status_code == 200
    r = tenant_api.request(method, path, json=body, headers=_h(role="viewer"))
    assert r.status_code == 403
    assert tenant_api.db.rows["fact_aaown01"]["body"] == "From $900 per person."


def test_no_token_is_refused(tenant_api):
    assert tenant_api.get("/v1/facts").status_code in (401, 403)


# ── /admin/facts: roles and audit ────────────────────────────────────────────────────────────────

_SECRET = "aa814-test-admin-secret"


@pytest.fixture
def admin_api(monkeypatch):
    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    db = FakeFactsDB()
    db.add(fact_id="fact_platvisa", scope="platform", kind="visa_entry", title="Laos visa", body="E-visa $50.",
           provenance="Embassy site", country="Laos")
    db.add(fact_id="fact_aaown01", tenant_id=TENANT_A, kind="price", title="Tenant price", body="b", provenance="p")
    conn = db.conn(None)                                       # main pool: cis_app, no RLS filter
    conn.execute = AsyncMock(return_value="INSERT 0 1")        # the audit row, on the same connection
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    client = TestClient(app)
    client.db, client.conn = db, conn
    return client


def _audits(conn) -> list[tuple]:
    """The acp_shared.audit_log INSERTs on the connection (the usage middleware also writes on the main pool)."""
    return [c.args for c in conn.execute.call_args_list if "acp_shared.audit_log" in c.args[0]]


def _staff(role):
    return {"X-Admin-Secret": _SECRET, "x-admin-role": role, "x-admin-user-id": "staff-1"}


def test_admin_write_routes_need_quality_write_which_only_admin_holds():
    for key in (("POST", "/admin/facts"), ("PATCH", "/admin/facts/{fact_id}"), ("DELETE", "/admin/facts/{fact_id}")):
        assert staff_authz.POLICY[key] == staff_authz.QUALITY_WRITE
    holders = {r for r, perms in staff_authz.ROLE_PERMS.items() if staff_authz.QUALITY_WRITE in perms}
    assert holders == {"admin"}


@pytest.mark.parametrize("role", ["admin", "reviewer", "content", "viewer"])
def test_every_staff_role_lists_platform_facts_only(admin_api, role):
    r = admin_api.get("/admin/facts", headers=_staff(role))
    assert r.status_code == 200, r.text
    assert [f["fact_id"] for f in r.json()["facts"]] == ["fact_platvisa"]     # never a tenant's fact


@pytest.mark.parametrize("role", ["reviewer", "content", "viewer"])
def test_non_admin_staff_cannot_write(admin_api, role):
    body = {"kind": "price", "title": "t", "body": "b", "provenance": "p"}
    assert admin_api.post("/admin/facts", json=body, headers=_staff(role)).status_code == 403
    assert admin_api.patch("/admin/facts/fact_platvisa", json={"body": "x"}, headers=_staff(role)).status_code == 403
    assert admin_api.delete("/admin/facts/fact_platvisa", headers=_staff(role)).status_code == 403
    assert admin_api.db.rows["fact_platvisa"]["body"] == "E-visa $50."
    assert _audits(admin_api.conn) == []


def test_admin_create_edit_delete_with_audit_on_the_same_connection(admin_api):
    r = admin_api.post("/admin/facts", json={"kind": "season", "title": "Laos rainy season", "body": "May-Oct.",
                                             "provenance": "Met office", "country": "Laos"}, headers=_staff("admin"))
    assert r.status_code == 201, r.text
    fid = r.json()["fact_id"]
    row = admin_api.db.rows[fid]
    assert row["scope"] == "platform" and row["tenant_id"] is None and row["updated_by"] == "admin:staff-1"
    args = _audits(admin_api.conn)[-1]
    assert "fact.created" in args and "admin:staff-1" in args and None in args

    r = admin_api.patch(f"/admin/facts/{fid}", json={"body": "May to October.",
                                                     "expected_updated_at": r.json()["updated_at"]},
                        headers=_staff("admin"))
    assert r.status_code == 200, r.text
    assert "fact.updated" in _audits(admin_api.conn)[-1]
    assert admin_api.patch(f"/admin/facts/{fid}", json={"body": "x", "expected_updated_at": T0.isoformat()},
                           headers=_staff("admin")).status_code == 409
    assert admin_api.delete(f"/admin/facts/{fid}", headers=_staff("admin")).status_code == 200
    assert fid not in admin_api.db.rows and "fact.deleted" in _audits(admin_api.conn)[-1]


def test_admin_cannot_reach_a_tenant_fact(admin_api):
    assert admin_api.get("/admin/facts/fact_aaown01", headers=_staff("admin")).status_code == 404
    assert admin_api.patch("/admin/facts/fact_aaown01", json={"body": "x"},
                           headers=_staff("admin")).status_code == 404
    assert admin_api.delete("/admin/facts/fact_aaown01", headers=_staff("admin")).status_code == 404
    assert "fact_aaown01" in admin_api.db.rows
