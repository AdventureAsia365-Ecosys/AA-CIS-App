"""X2 RLS catalogue guard on the replayed migrations DB (rules in rls_catalogue_rules.py).

Fails when the catalog the CIS migrations build breaks a rule (a tenant_id table without RLS + an app.tenant_id
policy, a policy TO PUBLIC, app.current_tenant, current_setting('app.tenant_id') outside NULLIF, a foreign key across
owners, a grant to PUBLIC, a grant to another owner's role, an unconditional `true` policy) and the object is on
neither the shared Dev allowlist (rls_catalogue_rules.ALLOWLIST, the same file AA-CIS-Infra runs on the nightly
Dev dump) nor CI_ONLY_ALLOWLIST below.

CI_ONLY_ALLOWLIST: what only this replay has. conftest skips 001/002/007_seed/007b/008/244/245/246, patches
004/005/006, drops some FKs, keeps legacy tables Dev dropped (X1_CI_ONLY_LEGACY_RLS) and creates the CI-only role
app_user. Every entry names why it is CI-only; nothing in it may exist on Dev (the Infra run would flag it there).

A new migration that trips the guard: fix the object (RLS + a NULLIF policy, no PUBLIC, no cross-owner FK), or add a
one-line reason to ALLOWLIST in rls_catalogue_rules.py and ship the same bytes to AA-CIS-Infra db/ (Copy Guard).
"""
import os
import sys

import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import rls_catalogue_rules as rules  # noqa: E402
from conftest import X1_CI_ONLY_LEGACY_RLS, _get_conn  # noqa: E402

_LEGACY = "CI-only legacy table (dropped on Dev, conftest X1_CI_ONLY_LEGACY_RLS): pre-NULLIF tenant_isolation policy"
CI_ONLY_ALLOWLIST = {
    "b": {f"{t}:tenant_isolation": f"{_LEGACY} TO PUBLIC" for t in X1_CI_ONLY_LEGACY_RLS},
    "d": {f"{t}:tenant_isolation": _LEGACY for t in X1_CI_ONLY_LEGACY_RLS},
}


def read_catalog(conn) -> dict:
    """The rules' input (a format-2 dump subset) from the live catalog of `conn`."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(rules.CATALOG_SQL["schemas"])
        rows = {"schemas": [dict(r) for r in cur.fetchall()]}
        names = [s["schema"] for s in rows["schemas"]]
        for key, sql in rules.CATALOG_SQL.items():
            if key != "schemas":
                cur.execute(sql, {"schemas": names})
                rows[key] = [dict(r) for r in cur.fetchall()]
    return rules.dump_from_rows(rows)


@pytest.fixture(scope="module")
def catalog():
    conn = _get_conn()
    try:
        yield read_catalog(conn)
    finally:
        conn.close()


def test_allowlists_are_well_formed():
    assert rules.check_allowlist(rules.ALLOWLIST) == []
    assert rules.check_allowlist(CI_ONLY_ALLOWLIST) == []
    overlap = {(r, k) for r, e in CI_ONLY_ALLOWLIST.items() for k in e if k in rules.ALLOWLIST.get(r, {})}
    assert not overlap, f"CI-only entries already on the shared allowlist: {sorted(overlap)}"


def test_replay_reached_the_catalog(catalog):
    """A replay that silently failed (conftest swallows migration errors) would pass the guard on an empty DB."""
    rels = catalog["relations"]
    assert "raw_tours" in rels.get("silver_aa_internal", {})
    assert "job" in rels.get("shared", {})
    assert any(r.get("policies") for s in rels.values() for r in s.values())


def test_catalogue_has_no_unallowlisted_violation(catalog):
    result = rules.check(catalog, rules.merge_allowlists(rules.ALLOWLIST, CI_ONLY_ALLOWLIST))
    assert not result.violations, (
        f"{len(result.violations)} RLS catalogue violation(s) (AA-848 X2 guard). Fix the object or add a one-line "
        f"reason to rls_catalogue_rules.ALLOWLIST (and copy the file to AA-CIS-Infra db/):\n"
        + rules.format_findings(result.violations))


def test_ci_only_entries_are_used(catalog):
    """A CI-only entry that no longer matches anything here is dead weight: remove it."""
    result = rules.check(catalog, rules.merge_allowlists(rules.ALLOWLIST, CI_ONLY_ALLOWLIST))
    stale = [(r, k) for r, k in result.stale if k in CI_ONLY_ALLOWLIST.get(r, {})]
    assert not stale, f"unused CI-only allowlist entries: {stale}"


# ── Seeded violations: each rule must fail on a real object in this database (S241 Done-when: a seeded violation
# per rule fails the guard). Every seed runs in a transaction that is rolled back. ────────────────────────────────
SEEDS = {
    "a": ("CREATE TABLE shared.zz848_a (id int PRIMARY KEY, tenant_id uuid NOT NULL)", "shared.zz848_a"),
    "b": ("CREATE TABLE shared.zz848_b (id int PRIMARY KEY); ALTER TABLE shared.zz848_b ENABLE ROW LEVEL SECURITY; "
          "CREATE POLICY zz848_open ON shared.zz848_b USING (id > 0)", "shared.zz848_b:zz848_open"),
    "c": ("CREATE TABLE shared.zz848_c (id int PRIMARY KEY, tenant_id uuid); "
          "ALTER TABLE shared.zz848_c ENABLE ROW LEVEL SECURITY; "
          "CREATE POLICY zz848_old ON shared.zz848_c TO aa_app_user "
          "USING (tenant_id::text = current_setting('app.current_tenant', true))", "shared.zz848_c:zz848_old"),
    "d": ("CREATE TABLE shared.zz848_d (id int PRIMARY KEY, tenant_id uuid); "
          "ALTER TABLE shared.zz848_d ENABLE ROW LEVEL SECURITY; "
          "CREATE POLICY zz848_cast ON shared.zz848_d TO aa_app_user "
          "USING (tenant_id = current_setting('app.tenant_id', true)::uuid)", "shared.zz848_d:zz848_cast"),
    "e": ("CREATE SCHEMA IF NOT EXISTS booking; CREATE TABLE booking.zz848_e (id int PRIMARY KEY, "
          "tenant_id uuid REFERENCES shared.tenants(tenant_id))",
          "booking.zz848_e:zz848_e_tenant_id_fkey->shared.tenants"),
    "p": ("CREATE TABLE shared.zz848_p (id int PRIMARY KEY); GRANT SELECT ON shared.zz848_p TO PUBLIC",
          "shared.zz848_p"),
    "g": ("CREATE TABLE shared.zz848_g (id int PRIMARY KEY); GRANT SELECT ON shared.zz848_g TO booking",
          "shared.zz848_g:booking"),
    "t": ("CREATE TABLE shared.zz848_t (id int PRIMARY KEY); ALTER TABLE shared.zz848_t ENABLE ROW LEVEL SECURITY; "
          "CREATE POLICY zz848_all ON shared.zz848_t TO aa_app_user USING (true)", "shared.zz848_t:zz848_all"),
}


@pytest.mark.parametrize("rule", sorted(SEEDS))
def test_seeded_violation_fails_the_guard(rule):
    sql, key = SEEDS[rule]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)                       # an error here fails the test: the seed must reach the DB
        result = rules.check(read_catalog(conn), rules.merge_allowlists(rules.ALLOWLIST, CI_ONLY_ALLOWLIST))
        hits = [f for f in result.violations if f.rule == rule and f.key == key]
        assert hits, f"rule {rule} did not catch {key}:\n" + rules.format_findings(result.violations)
    finally:
        conn.rollback()
        conn.close()


def test_seeded_tenant_pool_table_cannot_be_allowlisted():
    """Rule a: an allowlist entry does not cover a tenant_id table the tenant pool (aa_app_user) can read."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE shared.zz848_pool (id int PRIMARY KEY, tenant_id uuid NOT NULL); "
                        "GRANT SELECT ON shared.zz848_pool TO aa_app_user")
        allow = rules.merge_allowlists(rules.ALLOWLIST, CI_ONLY_ALLOWLIST, {"a": {"shared.zz848_pool": "test"}})
        result = rules.check(read_catalog(conn), allow)
        assert [f for f in result.violations if f.rule == "a" and f.key == "shared.zz848_pool" and f.hard]
    finally:
        conn.rollback()
        conn.close()
