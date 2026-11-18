"""AA-1113 (S284 redesign, CIS 453) — the split of a connection's provider budget between the CIS share and the CRM
share: the over-allocation flag, the states and the setting parser. Invented connection keys and amounts only."""
from decimal import Decimal

import pytest

from api.services import llm_connection_split as split
from shared import settings


def test_the_shares_above_the_provider_budget_are_flagged():
    row = split.split_row("zz-conn", provider_budget=100, cis_share=80, crm_share=30)
    assert row["over_allocated"] is True
    assert row["state"] == split.OVER_ALLOCATED
    assert row["allocated_usd"] == 110.0 and row["headroom_usd"] == -10.0


def test_shares_exactly_at_the_provider_budget_are_not_over():
    row = split.split_row("zz-conn", provider_budget=100, cis_share=70, crm_share=30)
    assert row["over_allocated"] is False and row["state"] == split.OK and row["headroom_usd"] == 0.0


def test_cis_alone_above_the_budget_is_flagged_even_when_the_crm_share_is_unknown():
    row = split.split_row("zz-conn", provider_budget=50, cis_share=60, crm_share=None, crm_known=False)
    assert row["over_allocated"] is True and row["state"] == split.OVER_ALLOCATED
    assert row["crm_share_usd"] is None and row["uncapped_apps"] == []


def test_an_unknown_crm_share_under_the_budget_is_not_called_ok():
    row = split.split_row("zz-conn", provider_budget=100, cis_share=40, crm_share=5, crm_known=False)
    assert row["state"] == split.CRM_UNKNOWN and row["crm_share_usd"] is None and row["allocated_usd"] == 40.0


@pytest.mark.parametrize("cis_share,crm_share,uncapped", [
    (None, 10, ["cis"]),
    (10, None, ["crm"]),
    (None, None, ["cis", "crm"]),
])
def test_an_app_with_no_cap_on_a_budgeted_connection_is_unbounded(cis_share, crm_share, uncapped):
    row = split.split_row("zz-conn", provider_budget=100, cis_share=cis_share, crm_share=crm_share)
    assert row["state"] == split.UNBOUNDED and row["uncapped_apps"] == uncapped and row["over_allocated"] is False


def test_no_provider_budget_checks_nothing():
    row = split.split_row("zz-conn", provider_budget=None, cis_share=500, crm_share=500)
    assert row["state"] == split.NO_PROVIDER_BUDGET and row["over_allocated"] is False
    assert row["headroom_usd"] is None


def test_split_rows_lists_every_connection_and_the_stray_keys():
    out = split.split_rows([("zz-b", 80), ("zz-a", None)], {"zz-b": Decimal(100), "zz-typo": Decimal(5)},
                           {"zz-b": Decimal(30), "zz-other": Decimal(1)})
    assert [r["connection_key"] for r in out["rows"]] == ["zz-a", "zz-b"]
    assert out["over_allocated"] == ["zz-b"]
    assert out["unknown_keys"] == ["zz-other", "zz-typo"]
    assert out["rows"][0]["state"] == split.NO_PROVIDER_BUDGET


def test_split_rows_with_the_crm_unreadable_marks_every_row():
    out = split.split_rows([("zz-a", 10)], {"zz-a": Decimal(100)}, None)
    assert out["rows"][0]["crm_known"] is False and out["rows"][0]["state"] == split.CRM_UNKNOWN


def test_budget_map_parses_numbers_and_refuses_anything_else():
    assert split.budget_map({"zz-a": 100, "zz-b": 2.5, "zz-c": "7"}) == {
        "zz-a": Decimal(100), "zz-b": Decimal("2.5"), "zz-c": Decimal(7)}
    assert split.budget_map({}) == {}
    for bad in ([], "x", {"zz-a": -1}, {"zz-a": True}, {"zz-a": None}, {"zz-a": "lots"}, {"": 1}, {"zz-a": "nan"}):
        with pytest.raises(ValueError):
            split.budget_map(bad)


def test_the_provider_budget_key_is_compiled_with_an_empty_default():
    d = settings.definition(split.PROVIDER_BUDGET_KEY)
    assert d.value_type == "json" and d.default_value == {} and d.enforced is True
    assert (d.min_value, d.max_value) == (0, 1_000_000)
