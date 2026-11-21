"""AA-1155 spec tasks T13 / T14 against real Postgres (CIS 462 + 465 applied), as the runtime role ``cis_app``: the
CIS role editor and the people-role preset editor write what they say, with the grants CIS 465 gives, and audit
before and after; the directory applies a preset read from the table.

- T13 (R5.6): an owner clones a built-in role into a custom role (approve class left out), ticks capabilities,
  relabels it, cannot archive it while an active account holds it, archives it once free; an approve-class or
  unknown capability is refused before any write; a non-owner is refused.
- T14 (R5.7): an owner creates and edits a preset (audited before and after); change_roles applies an edited preset
  by code; an archived preset is refused.

Invented names, codes and addresses only; every row this module adds is removed at the end (audit rows stay:
append-only).
"""
import json
import os
import sys
import uuid

import asyncpg
import psycopg2
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.core import staff_identity  # noqa: E402
from api.services import staff_directory as sd  # noqa: E402
from api.services import staff_presets as sp  # noqa: E402
from api.services import staff_role_editor as sre  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIGRATIONS = [os.path.join(_MIG, "462_aa1155_custom_staff_roles.sql"),
              os.path.join(_MIG, "465_aa1155_staff_role_presets.sql")]
ROLE = "custom_zz_frontdesk"
PRESET = "zz_frontdesk_preset"


@pytest.fixture(scope="module", autouse=True)
def applied():
    c = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    c.autocommit = True   # each file brings its own BEGIN … COMMIT (re-runnable)
    try:
        for path in MIGRATIONS:
            with open(path, encoding="utf-8") as f:
                c.cursor().execute(f.read())
    finally:
        c.close()
    yield


async def _as_cis_app(conn):
    """Every pooled connection runs as the runtime role (the test cluster's user is a superuser)."""
    await conn.execute("SET ROLE cis_app")


class NoCognito:
    """The directory writer's Cognito half, recording the groups (no AWS)."""

    def __init__(self):
        self.calls = []

    def add_to_group(self, email, group):
        self.calls.append(("add", group))

    def remove_from_group(self, email, group):
        self.calls.append(("remove", group))


@pytest_asyncio.fixture
async def db():
    admin = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    # cis_app's runtime grants on the tables these writes touch besides the editor tables (prod has them through the
    # other migrations' grants; this throwaway database may not), granted for the test only.
    await admin.execute(
        "GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app; "
        "GRANT SELECT, UPDATE ON shared.admin_users TO cis_app; "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON shared.staff_app_grant, shared.staff_crm_grant TO cis_app; "
        "GRANT INSERT, SELECT ON acp_shared.audit_log TO cis_app")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, init=_as_cis_app)
    ids: list = []
    staff_identity.clear_cache()

    async def add(role="admin", owner=False, custom_role=None):
        uid = uuid.uuid4()
        await admin.execute(
            "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, display_name, "
            "is_owner, custom_role) VALUES ($1, $2, NULL, $3::shared.admin_role, true, $4, 'Test Person', $5, $6)",
            uid, f"aa1155e-{uid.hex[:8]}", role, f"aa1155e-{uid.hex[:8]}@adventure.asia", owner, custom_role)
        ids.append(uid)
        return str(uid)

    class Ns:
        pass

    ns = Ns()
    ns.pool, ns.admin, ns.add = pool, admin, add
    yield ns
    staff_identity.clear_cache()
    await admin.execute("DELETE FROM shared.admin_users WHERE id = ANY($1::uuid[])", ids)
    await admin.execute("DELETE FROM shared.staff_role WHERE code = $1", ROLE)
    await admin.execute("DELETE FROM shared.staff_role_preset WHERE code = $1", PRESET)
    await pool.close()
    await admin.close()


async def _audit(db, action, resource_id):
    rows = await db.admin.fetch(
        "SELECT details FROM acp_shared.audit_log WHERE action = $1 AND resource_id = $2 "
        "ORDER BY created_at, chain_seq",
        action, resource_id)
    return [json.loads(r["details"]) if isinstance(r["details"], str) else r["details"] for r in rows]


@pytest.mark.asyncio
async def test_the_role_editor_writes_as_cis_app_and_audits_before_and_after(db):
    owner = await db.add(owner=True)
    role = await sre.create_role(db.pool, actor_id=owner, code=ROLE, label="Zz front desk", based_on="content")
    assert role["based_on"] == "content" and role["capabilities"]
    assert not any(c.endswith(".approve") for c in role["capabilities"])     # cloned without the approve class
    created = await _audit(db, sre.ROLE_CREATED, ROLE)
    assert created and created[-1]["after"]["code"] == ROLE

    after = await sre.update_role(db.pool, actor_id=owner, code=ROLE, label="Zz help desk",
                                  capabilities=["content.read", "tenant.read"])
    assert after["label"] == "Zz help desk" and after["capabilities"] == ["content.read", "tenant.read"]
    updated = await _audit(db, sre.ROLE_UPDATED, ROLE)
    assert updated[-1]["before"]["label"] == "Zz front desk" and updated[-1]["after"]["label"] == "Zz help desk"

    holder = await db.add(role="content", custom_role=ROLE)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sre.update_role(db.pool, actor_id=owner, code=ROLE, archived=True)
    assert (e.value.status, e.value.code) == (409, "role_in_use")
    await db.admin.execute("UPDATE shared.admin_users SET is_active = false WHERE id = $1", uuid.UUID(holder))
    archived = await sre.update_role(db.pool, actor_id=owner, code=ROLE, archived=True)
    assert archived["archived"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("caps, code", [
    (["content.read", "settings.approve"], "approve_class_capability"),
    (["content.read", "staff.write"], "approve_class_capability"),
    (["content.read", "made.up"], "unknown_capability"),
])
async def test_the_role_editor_refuses_before_any_write(db, caps, code):
    owner = await db.add(owner=True)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sre.create_role(db.pool, actor_id=owner, code=ROLE, label="Zz", based_on="content", capabilities=caps)
    assert (e.value.status, e.value.code) == (400, code)
    assert await db.admin.fetchval("SELECT count(*) FROM shared.staff_role WHERE code = $1", ROLE) == 0


@pytest.mark.asyncio
async def test_only_an_owner_edits_roles_and_presets(db):
    admin = await db.add(owner=False)
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sre.create_role(db.pool, actor_id=admin, code=ROLE, label="Zz", based_on="content")
    assert e.value.status == 403
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sp.create_preset(db.pool, actor_id=admin, fields={"code": PRESET, "label": "Zz", "cis_role": "support"})
    assert e.value.status == 403


@pytest.mark.asyncio
async def test_a_preset_is_created_edited_audited_and_applied_from_the_table(db):
    owner = await db.add(owner=True)
    target = await db.add(role="viewer")
    created = await sp.create_preset(db.pool, actor_id=owner, fields={
        "code": PRESET, "label": "Zz front desk", "cis_role": "support", "crm_mode": "add", "crm_roles": ("support",),
        "sort_order": 900})
    assert created.cis_role == "support"
    assert (await _audit(db, sp.PRESET_CREATED, PRESET))[-1]["after"]["label"] == "Zz front desk"

    edited = await sp.update_preset(db.pool, actor_id=owner, code=PRESET,
                                    fields={"label": "Zz help desk", "cis_role": "customer_success"})
    audit = (await _audit(db, sp.PRESET_UPDATED, PRESET))[-1]
    assert audit["before"]["cis_role"] == "support" and audit["after"]["cis_role"] == "customer_success"
    assert edited.label == "Zz help desk"

    await sd.change_roles(db.pool, NoCognito(), actor_id=owner, staff_id=target, preset=PRESET)
    row = await db.admin.fetchrow("SELECT role::text AS role FROM shared.admin_users WHERE id = $1",
                                  uuid.UUID(target))
    assert row["role"] == "customer_success"
    crm = await db.admin.fetch("SELECT role FROM shared.staff_crm_grant WHERE admin_user_id = $1", uuid.UUID(target))
    assert {r["role"] for r in crm} == {"support"}

    await sp.update_preset(db.pool, actor_id=owner, code=PRESET, fields={"archived": True})
    with pytest.raises(sd.StaffDirectoryError) as e:
        await sd.change_roles(db.pool, NoCognito(), actor_id=owner, staff_id=target, preset=PRESET)
    assert (e.value.status, e.value.code) == (400, "preset")


@pytest.mark.asyncio
async def test_the_seeded_presets_are_read_in_sort_order(db):
    presets = await sp.list_presets(db.pool)
    seeded = [p.code for p in presets if p.code in {"platform_owner", "business_owner", "content_ops", "pricing_team"}]
    assert seeded == ["platform_owner", "business_owner", "content_ops", "pricing_team"]
