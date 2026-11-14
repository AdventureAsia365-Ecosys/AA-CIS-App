"""AA-1096 — Platform › Infra (GET /admin/platform/infra, api/services/platform_infra.py): the limits that stay in
Terraform, read from the generated alarms page (OPS-07) of the docs reader bundle. The fixture is the live section of
root docs/generated/alarms.md on 14/11/2026 (AA-CIS-Infra output platform_limits). No S3, no DB."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.core import staff_authz
from api.services import docs_reader
from api.services import platform_infra as pi

FIXTURE = (Path(__file__).parent / "fixtures" / "aa1096_ops07_alarms.md").read_text(encoding="utf-8")


def _view(markdown: str = FIXTURE) -> dict:
    return {"page": {"id": "OPS-07", "path": "docs/generated/alarms.md", "title": "Monitoring and alarm catalog"},
            "markdown": markdown, "translations": [], "source": {"repo": "AA-Ecosys", "sha": "abc123"},
            "generated_at": "2026-11-14T00:00:00+00:00"}


def _fake(result=None, error=None):
    async def load(page_id, repo, client_factory=None):
        assert (page_id, repo) == (pi.PAGE_ID, pi.PAGE_REPO)
        if error is not None:
            raise error
        return result
    return load


# ── parse ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_section_parses_into_its_five_groups():
    parsed = pi.parse_section(FIXTURE)
    assert parsed is not None
    assert set(parsed) == {"alarms", "api_gateway_throttles", "budgets", "lambda_reserved_concurrency", "waf"}
    assert ("rds_connections_above", "108") in parsed["alarms"]
    assert ("acc2_per_app_monthly_usd.cis", "90") in parsed["budgets"]
    assert ("geo_allowed_countries", "US, GB, AU, SG, VN, IN, JP, TH, NZ, CA") in parsed["waf"]
    assert ("crm_slo_breaching_of_periods", "2 of 3") in parsed["alarms"]


def test_rows_outside_the_section_are_ignored_and_the_section_ends_at_the_next_h2():
    md = FIXTURE + "\n## Something else\n\n### waf\n\n| Setting | Value |\n|---|---|\n| `later_key` | `1` |\n"
    parsed = pi.parse_section(md)
    assert all(k != "later_key" for k, _ in parsed["waf"])
    assert all(k != "rds-cpu" for rows in parsed.values() for k, _ in rows)   # the Metric alarms table above


def test_a_page_without_the_section_parses_to_none():
    assert pi.parse_section("# Monitoring\n\n## Metric alarms\n\n| `a` | `b` |\n") is None


def test_the_terraform_commit_is_read_from_the_generated_marker():
    assert pi.terraform_sha(FIXTURE) == "09ea0c487275f0b4c702f4d67042c07edbf48015"
    assert pi.terraform_sha("# no marker") is None


# ── catalog ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_catalog_covers_every_value_of_the_live_output_and_nothing_else():
    groups, missing = pi.build_groups(pi.parse_section(FIXTURE))
    assert missing == []
    unknown = [f"{g['group']}.{i['key']}" for g in groups for i in g["items"] if not i["known"]]
    assert unknown == []
    assert [g["group"] for g in groups] == list(pi.GROUPS)


@pytest.mark.parametrize("key", sorted(pi.CATALOG))
def test_each_catalog_entry_says_where_it_is_changed_in_both_languages(key):
    limit = pi.CATALOG[key]
    assert limit.unit in pi.UNITS
    assert limit.source.startswith(("accounts/aa365 ", "modules/")), limit.source
    assert limit.en.strip() and limit.vi.strip() and limit.en != limit.vi
    assert key.split(".", 1)[0] in pi.GROUPS


def test_every_group_explains_itself_in_both_languages():
    for name, meta in pi.GROUPS.items():
        for part in ("title", "what", "why"):
            assert meta[part]["en"] and meta[part]["vi"], (name, part)
        assert (meta["editable"] is None) is (meta["editable_href"] is None), name
        if meta["editable_href"]:
            assert meta["editable_href"].startswith("/admin/"), name


def test_no_account_id_arn_or_host_in_the_catalog():
    text = repr(pi.CATALOG) + repr(pi.GROUPS)
    assert "arn:" not in text and ".amazonaws.com" not in text
    assert not docs_reader._ACCOUNT_ID.search(text)


def test_an_unknown_group_or_key_is_still_shown():
    groups, missing = pi.build_groups({"waf": [("new_rule_limit", "7")], "shield": [("enabled", "on")]})
    assert [g["group"] for g in groups] == ["waf", "shield"]
    new = groups[0]["items"][0]
    assert new == {"key": "new_rule_limit", "value": "7", "unit": "text", "source": None,
                   "label": pi._UNKNOWN_LABEL, "known": False}
    assert groups[1]["title"] == {"en": "shield", "vi": "shield"}
    assert "waf.alb_acl_rate_limit_per_5min_per_ip" in missing


_ACC_LABEL = re.compile(r"acc[0-9]", re.IGNORECASE)


@pytest.mark.parametrize("raw, shown", [
    ("acc2_total_monthly_usd", "total_monthly_usd"),
    ("acc2_per_app_monthly_usd.cis", "per_app_monthly_usd.cis"),
    ("cost_acc3_alert", "cost_alert"),
    ("cost_acc3", "cost"),
    ("myacc1x", "myaccountx"),
    ("rds_cpu_percent_above", "rds_cpu_percent_above"),
])
def test_the_display_key_drops_the_account_label(raw, shown):
    assert pi.display_key(raw) == shown


def test_the_live_answer_never_shows_an_account_label():
    """Admin rule: never print acc1 / acc2 / acc3. The live budgets rows are named acc2_* in Terraform."""
    groups, missing = pi.build_groups(pi.parse_section(FIXTURE))
    budgets = {i["key"]: i for g in groups if g["group"] == "budgets" for i in g["items"]}
    assert budgets["total_monthly_usd"]["known"] and budgets["per_app_monthly_usd.cis"]["value"] == "90"
    keys = [i["key"] for g in groups for i in g["items"]] + [g["group"] for g in groups]
    assert not [k for k in keys if _ACC_LABEL.search(k)]
    assert not _ACC_LABEL.search(json.dumps(groups, ensure_ascii=False))
    _, missing = pi.build_groups({"budgets": [("acc3_new_alert", "1")], "acc1_things": [("acc1_x", "2")]})
    assert missing and not [k for k in missing if _ACC_LABEL.search(k)]
    assert "budgets.total_monthly_usd" in missing


def test_an_unknown_group_or_key_with_an_account_label_is_shown_without_it():
    groups, _ = pi.build_groups({"acc1_things": [("acc1_x", "2"), ("acc3_x", "3")]})
    g = groups[0]
    assert g["group"] == "things" and g["title"] == {"en": "things", "vi": "things"}
    assert [i["key"] for i in g["items"]] == ["x", "x (2)"]


# ── read ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_answers_the_groups_with_the_doc_provenance(monkeypatch):
    monkeypatch.setattr(docs_reader, "load_staff_page_by_id", _fake(_view()))
    out = await pi.read_infra()
    assert out["doc"]["missing"] is False and out["doc"]["id"] == "OPS-07"
    assert out["doc"]["terraform_sha"].startswith("09ea0c48")
    assert out["doc"]["source"] == {"repo": "AA-Ecosys", "sha": "abc123"}
    waf = out["groups"][0]
    assert waf["group"] == "waf" and waf["editable_href"] == "/admin/platform/settings"
    first = {i["key"]: i for i in waf["items"]}["alb_acl_rate_limit_per_5min_per_ip"]
    assert first["value"] == "1000" and first["source"] == "accounts/aa365 variable waf_rate_limit"
    assert out["missing_keys"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error, reason", [
    (docs_reader.DocsMissing("x"), "not_published"),
    (docs_reader.DocsUnavailable("x"), "unavailable"),
    (docs_reader.DocsRestricted("x"), "owner_tier"),
])
async def test_a_page_that_cannot_be_read_is_a_reason_not_an_error(monkeypatch, error, reason):
    monkeypatch.setattr(docs_reader, "load_staff_page_by_id", _fake(error=error))
    assert await pi.read_infra() == {"doc": {"missing": True, "reason": reason}, "groups": [], "missing_keys": []}


@pytest.mark.asyncio
async def test_a_page_without_the_section_reads_as_not_published(monkeypatch):
    monkeypatch.setattr(docs_reader, "load_staff_page_by_id", _fake(_view("# Monitoring\n\n## Metric alarms\n")))
    assert (await pi.read_infra())["doc"] == {"missing": True, "reason": "not_published"}


def test_the_route_is_admin_ops_read():
    assert staff_authz.POLICY[("GET", "/admin/platform/infra")] == staff_authz.OPS_READ


def test_the_units_are_the_page_units():
    """The page (frontend/lib/platformInfra.ts InfraUnit, one message per unit) knows every unit the API sends."""
    import re

    lib = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "platformInfra.ts").read_text(encoding="utf-8")
    block = lib.split("export type InfraUnit =", 1)[1].split(";", 1)[0]
    assert re.findall(r'"([a-z_0-9]+)"', block) == list(pi.UNITS)
