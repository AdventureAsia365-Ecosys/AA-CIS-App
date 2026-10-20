"""X2 RLS catalogue guard rules (tests/integration/rls_catalogue_rules.py) on synthetic catalog rows: each rule
flags its violation, an allowlist entry with a reason covers it, a bad allowlist is refused. No database; the same
rules run on the replayed DB (tests/integration/test_rls_catalogue_guard.py) and on the nightly Dev dump (Infra)."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "integration", "rls_catalogue_rules.py")
_spec = importlib.util.spec_from_file_location("rls_catalogue_rules", _PATH)
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)

NULLIF_QUAL = "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
EMPTY = {r: {} for r in rules.RULES}


def table(columns=("id",), rls=False, policies=(), constraints=(), grants=(), column_grants=(), kind="table"):
    return {"kind": kind, "rls": rls, "columns": [{"name": c} for c in columns], "policies": list(policies),
            "constraints": list(constraints), "grants": list(grants), "column_grants": list(column_grants)}


def policy(name, qual=None, with_check=None, roles=("aa_app_user",), permissive="PERMISSIVE"):
    return {"policy": name, "permissive": permissive, "roles": list(roles), "cmd": "ALL", "qual": qual,
            "with_check": with_check}


def dump(relations, schema_grants=None):
    schemas = sorted(set(relations) | set(schema_grants or {}))
    return {"schemas": [{"schema": s, "grants": (schema_grants or {}).get(s, [])} for s in schemas],
            "relations": relations}


def clean_tenant_table():
    return table(("id", "tenant_id"), rls=True, policies=[policy("tenant_isolation", NULLIF_QUAL)])


# One violating catalog per rule and the key the rule reports.
CASES = {
    "a": (dump({"shared": {"x": table(("id", "tenant_id"))}}), "shared.x"),
    "b": (dump({"shared": {"x": table(rls=True, policies=[policy("p", "(id > 0)", roles=("public",))])}}),
          "shared.x:p"),
    "c": (dump({"shared": {"x": table(rls=True, policies=[
        policy("p", "((tenant_id)::text = current_setting('app.current_tenant'::text, true))")])}}), "shared.x:p"),
    "d": (dump({"shared": {"x": table(rls=True, policies=[
        policy("p", "(tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)")])}}), "shared.x:p"),
    "e": (dump({"booking": {"x": table(constraints=[{"constraint": "x_fk", "type": "f", "ref_schema": "shared",
                                                      "ref_name": "tenants"}])}}),
          "booking.x:x_fk->shared.tenants"),
    "p": (dump({"shared": {"x": table(grants=[{"grantee": "PUBLIC", "privileges": "SELECT"}])}}), "shared.x"),
    "g": (dump({"shared": {"x": table(grants=[{"grantee": "booking", "privileges": "SELECT"}])}}),
          "shared.x:booking"),
    "t": (dump({"shared": {"x": table(rls=True, policies=[policy("p", "true")])}}), "shared.x:p"),
}


@pytest.mark.parametrize("rule", sorted(rules.RULES))
def test_each_rule_flags_its_violation(rule):
    d, key = CASES[rule]
    result = rules.check(d, EMPTY)
    assert [(f.rule, f.key) for f in result.violations] == [(rule, key)]


@pytest.mark.parametrize("rule", sorted(rules.RULES))
def test_an_allowlist_entry_covers_it(rule):
    d, key = CASES[rule]
    result = rules.check(d, rules.merge_allowlists({rule: {key: "reason"}}))
    assert result.violations == [] and [f.key for f, _ in result.allowlisted] == [key] and result.stale == []


def test_clean_catalog_passes():
    d = dump({"shared": {"x": clean_tenant_table(), "v": table(("tenant_id",), kind="view")}},
             {"public": [{"grantee": "PUBLIC", "privileges": "USAGE"}]})
    assert rules.findings(d) == [rules.Finding("p", "public", "PUBLIC: USAGE")]
    assert rules.check(d).violations == []


def test_rule_a_needs_a_policy_on_app_tenant_id():
    d = dump({"shared": {"x": table(("id", "tenant_id"), rls=True, policies=[policy("p", "(id > 0)")])}})
    assert [f.detail for f in rules.check(d, EMPTY).violations] == ["no policy reads app.tenant_id"]


def test_rule_a_tenant_pool_grant_cannot_be_allowlisted():
    d = dump({"shared": {"x": table(("id", "tenant_id"), grants=[{"grantee": "aa_app_user", "privileges": "SELECT"}])}})
    result = rules.check(d, rules.merge_allowlists({"a": {"shared.x": "reason"}}))
    assert [(f.key, f.hard) for f in result.violations] == [("shared.x", True)]
    assert result.stale == [("a", "shared.x")]


@pytest.mark.parametrize("expr,gaps", [
    (NULLIF_QUAL, 0),
    ("(tenant_id = (current_setting('app.tenant_id'::text, true))::uuid)", 1),
    ("(tenant_id = NULLIF( current_setting('app.tenant_id'), '')::uuid)", 0),
    ("((NULLIF(current_setting('app.tenant_id', true), '') IS NULL) OR "
     "(tenant_id = (current_setting('app.tenant_id', true))::uuid))", 1),
    ("(id > 0)", 0),
    (None, 0),
])
def test_nullif_gaps(expr, gaps):
    assert rules.nullif_gaps(expr) == gaps


def test_rule_t_skips_the_x1_parity_policies_only():
    rels = {"silver_aa_internal": {"raw_tours": table(rls=True, policies=[
        policy("raw_tours_cis_app_all", "true", "true", roles=("cis_app",)),
        policy("raw_tours_cis_owner_all", "true", "true", roles=("cis_owner",)),
        policy("other_cis_app_all", "true", roles=("cis_app",)),
        policy("restrictive", "true", permissive="RESTRICTIVE"),
    ])}, "booking": {"case": table(rls=True, policies=[policy("case_cis_app_all", "true", roles=("cis_app",))])}}
    keys = [f.key for f in rules.check(dump(rels), EMPTY).violations]
    assert keys == ["booking.case:case_cis_app_all", "silver_aa_internal.raw_tours:other_cis_app_all"]


def test_rule_e_same_owner_passes_and_unknown_schema_fails():
    fk = {"constraint": "fk", "type": "f", "ref_schema": "shared", "ref_name": "tenants"}
    assert rules.check(dump({"acp_shared": {"x": table(constraints=[fk])}}), EMPTY).violations == []
    found = rules.check(dump({"newschema": {"x": table(constraints=[fk])}}), EMPTY).violations
    assert [(f.rule, f.detail) for f in found] == [("e", "no owner -> cis")]


def test_rule_g_column_and_schema_grants_and_platform_roles():
    rels = {"shared": {"x": table(column_grants=[{"column": "c", "grantee": "tripplanner", "privileges": "SELECT"}],
                                  grants=[{"grantee": "db_migrator", "privileges": "SELECT"},
                                          {"grantee": "cis_app", "privileges": "SELECT"}])}}
    d = dump(rels, {"shared": [{"grantee": "bk_owner", "privileges": "USAGE"}]})
    assert [f.key for f in rules.check(d, EMPTY).violations] == ["shared.x.c:tripplanner", "shared:bk_owner"]


def test_wildcard_entries_and_stale_report():
    rels = {"catalog": {"v_a": table(grants=[{"grantee": "booking", "privileges": "SELECT"}]),
                        "v_b": table(grants=[{"grantee": "booking", "privileges": "SELECT"}])}}
    allow = rules.merge_allowlists({"g": {"catalog.*:booking": "contract", "catalog.*:tripplanner": "contract"}})
    result = rules.check(dump(rels), allow)
    assert result.violations == [] and len(result.allowlisted) == 2
    assert result.stale == [("g", "catalog.*:tripplanner")]


@pytest.mark.parametrize("bad", [{"a": {"shared.x": ""}}, {"a": {"shared.x": "two\nlines"}}, {"z": {}}])
def test_bad_allowlists_are_refused(bad):
    assert rules.check_allowlist(bad)


def test_shipped_allowlist_is_well_formed():
    assert rules.check_allowlist(rules.ALLOWLIST) == []
    assert set(rules.ALLOWLIST) == set(rules.RULES)


def test_dump_from_rows_matches_the_dump_shape():
    rows = {
        "schemas": [{"schema": "shared"}],
        "schema_grants": [{"schema": "shared", "grantee": "PUBLIC", "privileges": "USAGE"}],
        "relations": [{"schema": "shared", "name": "x", "kind": "r", "rls": True}],
        "columns": [{"schema": "shared", "name": "x", "column": "tenant_id"}],
        "constraints": [], "table_grants": [], "column_grants": [],
        "policies": [{"schema": "shared", "name": "x", "policy": "tenant_isolation", "permissive": "PERMISSIVE",
                      "roles": ["aa_app_user"], "cmd": "ALL", "qual": NULLIF_QUAL, "with_check": None}],
    }
    d = rules.dump_from_rows(rows)
    assert d["relations"]["shared"]["x"]["kind"] == "table"
    assert [f.key for f in rules.findings(d)] == ["shared"]   # PUBLIC USAGE on a CIS schema
