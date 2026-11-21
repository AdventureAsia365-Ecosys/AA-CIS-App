"""AA-1155 (S287, spec tasks T13 / T14) — migration 465: the Staff page's people-role presets as data
(shared.staff_role_preset, four seeded presets) and the CIS role editor grants, against real Postgres.

The SQL runs for real, errors NOT swallowed. The module fixture applies 462 (465 needs it) and 465 twice
(re-runnable). Then: the seed holds exactly today's four presets, the CHECKs refuse what the code also refuses (a bad
code or role, an additive preset that clears or replaces, a preset that sets nothing, an unknown CRM role), no column
names an owner flag, and cis_app may write the editor tables. Invented codes and labels only; every test rolls back."""
import os
import sys

import psycopg2
import psycopg2.errors
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
MIG_462 = os.path.join(_MIG, "462_aa1155_custom_staff_roles.sql")
MIG_465 = os.path.join(_MIG, "465_aa1155_staff_role_presets.sql")

# (code, label, cis_role, booking_mode, booking_role, crm_mode, crm_roles, additive_only, cis_keep, booking_keep, sort)
SEED = [
    ("platform_owner", "Administrator", "admin", "set", "admin", "add", ["platform_admin"], False, [], [], 10),
    ("business_owner", "Business owner", "admin", "set", "admin", "keep", [], False, [], [], 20),
    ("content_ops", "Content + Product & operations", "content_lead", "clear", None, "replace", [], False, [], [], 30),
    ("pricing_team", "Pricing team", "customer_success", "set", "contracting", "keep", [], True,
     ["admin", "content_lead", "customer_success"], ["admin", "contracting"], 40),
]


def _connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)


def _apply(path):
    c = _connect()
    c.autocommit = True   # the file brings its own BEGIN … COMMIT
    try:
        with open(path, encoding="utf-8") as f:
            c.cursor().execute(f.read())
    finally:
        c.close()


@pytest.fixture(scope="module", autouse=True)
def applied():
    _apply(MIG_462)
    _apply(MIG_465)
    _apply(MIG_465)   # re-runnable
    yield


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _insert(cur, code="zz_preset", *, cis_role="content", booking_mode="keep", booking_role=None, crm_mode="keep",
            crm_roles=(), additive_only=False, cis_keep=(), booking_keep=(), label="Invented preset"):
    cur.execute(
        "INSERT INTO shared.staff_role_preset (code, label, cis_role, booking_mode, booking_role, crm_mode, crm_roles, "
        "additive_only, cis_keep_roles, booking_keep_roles, created_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s, %s::text[], %s::text[], 'admin:zz', 'admin:zz')",
        (code, label, cis_role, booking_mode, booking_role, crm_mode, list(crm_roles), additive_only, list(cis_keep),
         list(booking_keep)))


def _refused(conn, **kw):
    cur = conn.cursor()
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(cur, **kw)
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_465_registered_once(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '465'")
    assert cur.fetchone()[0] == 1


def test_465_is_a_contract_file_without_a_directive():
    text = open(MIG_465, encoding="utf-8").read()
    assert text.splitlines()[0].startswith("-- Migration 465")
    assert "-- migrate:" not in text


def test_the_four_presets_are_seeded_exactly(conn):
    cur = conn.cursor()
    cur.execute("SELECT code, label, cis_role, booking_mode, booking_role, crm_mode, crm_roles, additive_only, "
                "cis_keep_roles, booking_keep_roles, sort_order FROM shared.staff_role_preset "
                "WHERE code IN ('platform_owner', 'business_owner', 'content_ops', 'pricing_team') ORDER BY sort_order")
    assert [tuple(r) for r in cur.fetchall()] == [tuple(s) for s in SEED]


def test_no_column_can_hold_an_owner_flag(conn):
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = 'shared' "
                "AND table_name = 'staff_role_preset' AND column_name LIKE '%%owner%%'")
    assert cur.fetchone()[0] == 0


def test_a_preset_with_custom_roles_is_accepted(conn):
    cur = conn.cursor()
    _insert(cur, cis_role="custom_zz_maker", booking_mode="set", booking_role="custom_zz_ops", crm_mode="replace")
    cur.execute("SELECT archived, sort_order FROM shared.staff_role_preset WHERE code = 'zz_preset'")
    assert cur.fetchone() == (False, 100)


@pytest.mark.parametrize("kw", [
    {"code": "Bad-Code"},
    {"code": "x"},
    {"label": "  "},
    {"cis_role": "viewer"},
    {"cis_role": "reviewer"},
    {"cis_role": "superuser"},
    {"booking_mode": "set"},                                         # set without a role
    {"booking_mode": "keep", "booking_role": "sales"},               # a role without set
    {"booking_mode": "set", "booking_role": "content_creator"},      # retired
    {"booking_mode": "drop"},
    {"crm_mode": "add"},                                             # add nothing
    {"crm_mode": "keep", "crm_roles": ["ops"]},
    {"crm_mode": "replace", "crm_roles": ["owner"]},
    {"additive_only": True, "booking_mode": "clear"},
    {"additive_only": True, "crm_mode": "replace"},
    {"cis_keep": ["admin"]},                                         # keep roles without additive_only
    {"cis_role": None},                                              # sets nothing
])
def test_the_checks_refuse_what_the_code_refuses(conn, kw):
    _refused(conn, **kw)


def test_cis_app_may_write_the_editor_tables(conn):
    cur = conn.cursor()
    checks = {
        ("shared.staff_role_preset", "SELECT"): True, ("shared.staff_role_preset", "INSERT"): True,
        ("shared.staff_role_preset", "UPDATE"): True,
        ("shared.staff_role", "INSERT"): True, ("shared.staff_role", "UPDATE"): True,
        ("shared.staff_role_capability", "INSERT"): True, ("shared.staff_role_capability", "DELETE"): True,
    }
    for (table, privilege), expected in checks.items():
        cur.execute("SELECT has_table_privilege('cis_app', %s, %s) AS ok", (table, privilege))
        assert cur.fetchone()[0] is expected, (table, privilege)
