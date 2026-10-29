"""AA-1003 R1 — the vendored report registry and the invariants between it and the engine.

- services/reports/registry/*.yaml parse, every report's KPIs exist, the copy equals the root source when the root
  checkout is next to this repo (workspace layout; skipped in CI, where the root lint --copy covers it).
- The KPIs marked `snapshot: kpi_snapshot_daily` are exactly the ones services/reports/kpis.py computes.
- The reports marked `generator: cis` are exactly the ones services/reports/generate.py renders.
- User-visible registry text never shows an internal issue ID.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.reports import generate, kpis, registry

ROOT_REPORTS = Path(__file__).resolve().parents[2].parent.parent / "docs" / "reports"


def test_registry_loads_and_is_consistent():
    reg = registry.load()
    assert reg.version == 1
    assert len(reg.reports) >= 60
    ids = [r.id for r in reg.reports]
    assert len(ids) == len(set(ids))
    for r in reg.reports:
        for kid in r.kpis:
            assert reg.kpi(kid) is not None, (r.id, kid)
            assert r.id in reg.kpi(kid).used_by


def test_snapshot_kpis_equal_the_computers():
    reg = registry.load()
    assert {k.id for k in reg.snapshot_kpis()} == kpis.computed_kpi_ids()


def test_cis_generated_reports_equal_the_renderers():
    reg = registry.load()
    assert {r.id for r in reg.runnable()} == set(generate.RENDERERS)


def test_snapshot_kpis_have_the_units_the_table_accepts():
    allowed = {"count", "ratio", "usd", "seconds", "hours", "days", "score", "status", "text"}   # CIS 305 CHECK
    for k in registry.load().kpis.values():
        assert k.unit in allowed, k.id
        assert re.match(r"^K-[A-Z0-9]+(-[A-Z0-9]+)*$", k.id) and len(k.id) <= 64, k.id


def test_report_ids_fit_the_report_run_check():
    for r in registry.load().reports:
        assert re.match(r"^[A-Z]{2,4}-[0-9]{2}$", r.id), r.id


def test_no_linear_issue_id_in_names_shown_to_users():
    for r in registry.load().reports:
        for text in (r.name_en, r.name_vi):
            assert not re.search(r"\bAA-\d+\b", text), (r.id, text)
    for k in registry.load().kpis.values():
        assert not re.search(r"\bAA-\d+\b", k.name), k.id


@pytest.mark.skipif(not (ROOT_REPORTS / "catalog.yaml").exists(), reason="root repo not checked out next to this one")
def test_vendored_copy_matches_the_root_registry():
    for name in ("catalog.yaml", "kpis.yaml"):
        assert (registry.CATALOG_PATH.parent / name).read_bytes() == (ROOT_REPORTS / name).read_bytes(), name


def test_parse_refuses_a_report_with_an_unknown_kpi():
    cat = {"version": 1, "reports": [{
        "id": "EX-04", "family": "executive", "name_en": "n", "name_vi": "n", "audience": "a", "owner_role": "o",
        "decision": "d", "cadence": "daily", "schedule": "daily", "channel": "c", "automation": "auto",
        "status": "partial", "priority": "P0", "template": None, "generator": "cis", "kpis": ["K-NOPE"],
        "sections": []}]}
    with pytest.raises(ValueError):
        registry.parse(cat, {"version": 1, "kpis": []})


def test_parse_refuses_an_alert_bound_the_engine_does_not_read():
    kp = {"version": 1, "kpis": [{"id": "K-CAT-LIVE", "name": "n", "definition": "d", "unit": "count",
                                  "threshold": "t", "defined_in": "EX-04", "alert": {"red_lte": 100}}]}
    with pytest.raises(ValueError, match="alert"):
        registry.parse({"version": 1, "reports": []}, kp)
