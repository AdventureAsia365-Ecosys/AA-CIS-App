"""AA-969 part 2 (root ADR 0019 d16): the CIS clean-up kinds that touch tenant rows skip the pilot tenant
(``shared.tenants.is_pilot``, CIS 354). On Postgres: tests/integration/test_aa969_pilot_skip.py."""
from api.core import e2e_staff
from services.retention import policy

TENANT_TABLES = ("retention_llm_call_log", "retention_tenant_api_usage")


def test_retention_of_tenant_rows_leaves_the_pilot_out():
    assert "p.is_pilot" in policy.NOT_PILOT and "p.tenant_id = t.tenant_id" in policy.NOT_PILOT
    for kind in TENANT_TABLES:
        spec = policy.SPECS[kind]
        assert spec.parts and all(p.where == policy.NOT_PILOT for p in spec.parts), kind
    # the windows (DM-7.5 decisions) are unchanged
    assert policy.SPECS["retention_llm_call_log"].windows() == {"raw": 180}
    assert policy.SPECS["retention_tenant_api_usage"].windows() == {"raw": 90}


def test_platform_tables_have_no_pilot_guard():
    # shared.job, shared.decision_log and the cost snapshot hold no tenant data
    for kind in ("retention_job", "retention_decision_log", "retention_cost_explorer_snapshot"):
        assert all("is_pilot" not in p.where for p in policy.SPECS[kind].parts), kind


def test_the_e2e_identity_never_treats_the_pilot_as_synthetic():
    assert "is_pilot IS NOT TRUE" in e2e_staff.SYNTHETIC_TENANT_SQL
