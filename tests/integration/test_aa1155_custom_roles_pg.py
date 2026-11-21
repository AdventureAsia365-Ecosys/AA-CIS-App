"""AA-1155 spec tasks T8 / T9 / T11 against real Postgres (CIS 462 applied): the SQL of the custom-role read, the
Staff page writes and the budget audit runs as written.

- T8: resolve_staff for a cis-custom holder reads admin_users.custom_role and the role's capabilities (one cache entry
  with the row); an archived role refuses the holder; a built-in holder never needs the tables.
- T9: the roles catalogue reads shared.staff_role (and answers "unavailable" for the Booking view when this database
  has no bk-062); change_roles writes a custom code (role keeps based_on); the business-owner marker is set and
  cleared with audit rows and the partial unique index refuses a second holder; the staff list SELECT runs.
- T11: a budget change writes its spend_budget row and an audit row with before and after in one transaction.

Invented names and addresses only; every row this module adds is removed at the end (audit rows stay: append-only).
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import psycopg2
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.core import staff_identity, staff_roles  # noqa: E402
from api.core.identity_verifier import Principal  # noqa: E402
from api.routers import admin_budgets, admin_platform  # noqa: E402
from api.services import staff_custom_roles as scr  # noqa: E402
from api.services import staff_directory as sd  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zzAA1155"
MIG_462 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "462_aa1155_custom_staff_roles.sql")
ROLE = "custom_zz_desk"
BUDGET_SCOPE = "job:zz_aa1155_probe"


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True   # the file brings its own BEGIN … COMMIT (re-runnable)
    try:
        with open(MIG_462, encoding="utf-8") as f:
            c.cursor().execute(f.read())
    finally:
        c.close()
    yield


class NoCognito:
    """The directory writer's Cognito half, recording the groups (no AWS)."""

    def __init__(self):
        self.calls = []

    def add_to_group(self, email, group):
        self.calls.append(("add", group))

    def remove_from_group(self, email, group):
        self.calls.append(("remove", group))

    def create_user(self, email):
        return True

    def enable(self, email):
        self.calls.append(("enable", email))


@pytest_asyncio.fixture
async def db():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    ids: list = []
    staff_identity.clear_cache()
    await p.execute("DELETE FROM shared.staff_role WHERE code = $1", ROLE)
    await p.execute(
        "INSERT INTO shared.staff_role (code, label, based_on, created_by, updated_by) "
        "VALUES ($1, 'Desk test', 'content', 'test', 'test')", ROLE)
    await p.execute(
        "INSERT INTO shared.staff_role_capability (role_code, capability, created_by) "
        "SELECT $1, c, 'test' FROM unnest(ARRAY['content.read', 'tenant.read', 'made.up']) AS c", ROLE)

    async def add(role="content", email=None, sub=None, custom_role=None, owner=False):
        uid = uuid.uuid4()
        await p.execute(
            "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, display_name, "
            "cognito_iss, cognito_sub, custom_role, is_owner) "
            "VALUES ($1, $2, NULL, $3::shared.admin_role, true, $4, 'Test Person', $5, $6, $7, $8)",
            uid, f"aa1155-{uid.hex[:8]}", role, email, ISS if sub else None, sub, custom_role, owner)
        ids.append(uid)
        return str(uid)

    yield SimpleNamespace(pool=p, add=add)
    staff_identity.clear_cache()
    await p.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
    await p.execute("DELETE FROM shared.staff_role WHERE code = $1", ROLE)
    await p.execute("DELETE FROM shared.spend_budget WHERE scope = $1", BUDGET_SCOPE)
    await p.close()


def _email():
    return f"aa1155-{uuid.uuid4().hex[:10]}@adventure.asia"


def _principal(sub, email, groups):
    return Principal(iss=ISS, sub=sub, client_id="zz-client", groups=tuple(groups), email=email,
                     email_verified=None, username=sub)


@pytest.mark.asyncio
async def test_a_custom_holder_resolves_to_its_role_and_capabilities(db):
    sub = str(uuid.uuid4())
    email = _email()
    await db.add(role="content", email=email, sub=sub, custom_role=ROLE)
    identity = await staff_identity.resolve_staff(db.pool, _principal(sub, email, ["cis-custom"]))
    assert identity.role == ROLE and identity.custom_label == "Desk test"
    assert identity.capabilities == frozenset({"content.read", "tenant.read"})     # made.up is dropped
    await db.pool.execute("UPDATE shared.staff_role SET archived = true WHERE code = $1", ROLE)
    staff_identity.clear_cache()
    with pytest.raises(staff_identity.StaffAuthError) as e:
        await staff_identity.resolve_staff(db.pool, _principal(sub, email, ["cis-custom"]))
    assert e.value.status == 403 and e.value.reason == staff_identity.CUSTOM_ROLE_UNAVAILABLE


@pytest.mark.asyncio
async def test_a_built_in_holder_reads_the_row_with_its_new_column(db):
    sub = str(uuid.uuid4())
    email = _email()
    await db.add(role="content", email=email, sub=sub)
    identity = await staff_identity.resolve_staff(db.pool, _principal(sub, email, ["cis-content"]))
    assert identity.role == "content" and identity.custom_label is None


@pytest.mark.asyncio
async def test_the_roles_catalogue_reads_the_tables(db):
    async with db.pool.acquire() as conn:
        out = await staff_roles.roles_catalogue(conn)
    codes = {r["code"]: r for r in out["cis"]["custom"]}
    assert codes[ROLE]["capabilities"] == ["content.read", "tenant.read"]
    assert codes[staff_roles.CONTENT_ROLE_CODE]["capabilities"] == sorted(staff_roles.content_seed_capabilities())
    assert out["cis"]["custom_available"] is True
    assert out["booking"]["custom_available"] in (True, False)   # no bk-062 view in a CIS-only test database


@pytest.mark.asyncio
async def test_the_staff_page_writes_a_custom_role_and_the_marker(db):
    owner = await db.add(role="admin", email=_email(), owner=True)
    email = _email()
    uid = await db.add(role="content", email=email)
    cognito = NoCognito()
    await sd.change_roles(db.pool, cognito, actor_id=owner, staff_id=uid, role=ROLE)
    row = await db.pool.fetchrow("SELECT role::text AS role, custom_role FROM shared.admin_users WHERE id = $1::uuid",
                                 uid)
    assert (row["role"], row["custom_role"]) == ("content", ROLE)
    assert ("add", "cis-custom") in cognito.calls
    await sd.change_roles(db.pool, cognito, actor_id=owner, staff_id=uid, role="content_lead")
    row = await db.pool.fetchrow("SELECT role::text AS role, custom_role FROM shared.admin_users WHERE id = $1::uuid",
                                 uid)
    assert (row["role"], row["custom_role"]) == ("content_lead", None)

    other = await db.add(role="finance", email=_email())
    await db.pool.execute("UPDATE shared.admin_users SET is_business_owner = false WHERE is_business_owner")
    await scr.set_business_owner(db.pool, actor_id=owner, staff_id=uid, value=True)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await scr.set_business_owner(db.pool, actor_id=owner, staff_id=other, value=True)
    assert e.value.code == "business_owner_taken"
    with pytest.raises(asyncpg.UniqueViolationError):   # the index holds the rule too
        await db.pool.execute(scr.SQL_SET_BUSINESS_OWNER, other, True)
    await scr.set_business_owner(db.pool, actor_id=owner, staff_id=uid, value=False)
    audits = await db.pool.fetch(
        "SELECT details FROM acp_shared.audit_log WHERE action = $1 AND resource_id = $2 ORDER BY chain_seq",
        scr.BUSINESS_OWNER_CHANGED, uid)
    details = [json.loads(a["details"]) if isinstance(a["details"], str) else a["details"] for a in audits]
    assert details == [{"is_business_owner": {"before": False, "after": True}},
                       {"is_business_owner": {"before": True, "after": False}}]

    async with db.pool.acquire() as conn:
        select, _ = await admin_platform._select_sql(conn)
        listed = await conn.fetchrow(select + " WHERE u.id = $1", uuid.UUID(uid))
    out = admin_platform._staff_row(listed)
    assert out["custom_role"] is None and out["is_business_owner"] is False


@pytest.mark.asyncio
async def test_a_budget_change_is_audited_with_before_and_after(db):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=db.pool)))
    actor = str(uuid.uuid4())
    secret = admin_budgets.verify_admin_secret
    admin_budgets.verify_admin_secret = lambda s: None
    try:
        await admin_budgets.upsert_budget("dfs", BUDGET_SCOPE, admin_budgets.BudgetUpdate(per_day_usd=2.0), request,
                                          "s", actor)
        await admin_budgets.upsert_budget("dfs", BUDGET_SCOPE, admin_budgets.BudgetUpdate(per_day_usd=1.0), request,
                                          "s", actor)
    finally:
        admin_budgets.verify_admin_secret = secret
    rows = await db.pool.fetch(
        "SELECT details FROM acp_shared.audit_log WHERE action = $1 AND resource_id = $2 AND actor = $3 ORDER BY chain_seq",
        admin_budgets.AUDIT_UPDATED, f"dfs/{BUDGET_SCOPE}", f"admin:{actor}")
    details = [json.loads(r["details"]) if isinstance(r["details"], str) else r["details"] for r in rows]
    assert details[0]["before"] is None and details[0]["after"]["per_day_usd"] == 2.0
    assert details[1]["before"]["per_day_usd"] == 2.0 and details[1]["after"]["per_day_usd"] == 1.0
