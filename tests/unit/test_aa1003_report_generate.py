"""AA-1003 R1 — report rendering (EX-04 scorecard, EX-06 register) and the due/overdue rule (no DB)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.reports import generate, registry

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
DAY = date(2026, 10, 28)


def _reg():
    return registry.load()


def _row(kpi_id, day, value, scope="all", source="shared.job"):
    return {"kpi_id": kpi_id, "day": day, "scope": scope, "value": Decimal(str(value)), "unit": "count",
            "source": source, "data_as_of": None, "computed_at": NOW}


def test_formatting_by_unit():
    assert generate.fmt(1234.5, "usd") == "1,234.50 USD"
    assert generate.fmt(0.0512, "ratio") == "5.1 %"
    assert generate.fmt(3.0, "count") == "3"
    assert generate.fmt(None, "count") == "—"


def test_alert_status_uses_the_registry_bounds():
    k = _reg().kpi("K-JOB-FAIL")                     # amber >= 1, red >= 5
    assert [generate.alert_status(k, v) for v in (0, 1, 5, None)] == ["ok", "amber", "red", "no_data"]
    assert generate.alert_status(_reg().kpi("K-LLM-COST-DAY"), 3.0) == "info"


def test_scorecard_lists_ex04_kpis_first_with_delta_and_median():
    reg = _reg()
    rows = [_row("K-JOB-FAIL", DAY - timedelta(days=i), v, scope="app:cis") for i, v in enumerate([6, 2, 1, 3])]
    rows.append(_row("K-LLM-FALLBACK-24H", DAY, "0.02", source="shared.llm_call_log"))
    out = generate.render_scorecard(reg, reg.report("EX-04"), DAY, rows, NOW)
    ids = [k["id"] for k in out.output["kpis"]]
    assert ids.index("K-JOB-FAIL") < ids.index("K-LLM-FALLBACK-24H")
    job = next(k for k in out.output["kpis"] if k["id"] == "K-JOB-FAIL")
    s = job["scopes"][0]
    assert (s["scope"], s["value"], s["previous"], s["delta"], s["median_7d"], s["status"]) == \
        ("app:cis", 6.0, 2.0, 4.0, 2.0, "red")
    assert job["listed"] is True
    missing = {m["id"] for m in out.output["not_measured"]}
    assert {"K-TP-TRIPS", "K-BK-CONF", "K-COST-FCST"} <= missing     # other apps' KPIs come in later slices
    assert "# EX-04" in out.markdown and "| K-JOB-FAIL" in out.markdown and "## Not measured yet" in out.markdown
    assert "1 red" in out.summary


def test_scorecard_without_snapshot_rows_still_renders():
    reg = _reg()
    out = generate.render_scorecard(reg, reg.report("EX-04"), DAY, [], NOW)
    assert out.output["status_counts"]["no_data"] == len(out.output["kpis"])


def _last(covered_to=None, status="succeeded", failed=0):
    return {"id": "r1", "status": status, "trigger": "schedule", "period_start": covered_to, "period_end": covered_to,
            "finished_at": NOW, "created_at": NOW, "summary": "s", "error": None, "covered_to": covered_to,
            "failed_7d": failed}


@pytest.mark.parametrize("now,covered,state", [
    (datetime(2026, 10, 29, 2, 0, tzinfo=UTC), date(2026, 10, 28), "ok"),
    (datetime(2026, 10, 29, 2, 0, tzinfo=UTC), date(2026, 10, 27), "due"),       # chain for 28/10 started 00:30
    (datetime(2026, 10, 29, 3, 31, tzinfo=UTC), date(2026, 10, 27), "overdue"),  # past 00:30 + 3 h
    (datetime(2026, 10, 29, 3, 31, tzinfo=UTC), None, "overdue"),
])
def test_due_state_of_a_daily_report(now, covered, state):
    ex04 = _reg().report("EX-04")
    assert generate.due_state(ex04, _last(covered) if covered else None, now)["state"] == state


@pytest.mark.parametrize("now,covered,state,expected", [
    # Deploy day: the newest weekly period (19-25/10) was chained on 25/10, before automated_from: nothing expected.
    (datetime(2026, 10, 30, 6, 0, tzinfo=UTC), None, "pending_first", None),
    (datetime(2026, 11, 1, 23, 0, tzinfo=UTC), None, "pending_first", None),
    # The 01/11 chain (00:30 UTC on 02/11) creates 26/10-01/11: due within the grace, overdue after it.
    (datetime(2026, 11, 2, 1, 0, tzinfo=UTC), None, "due", "2026-11-01"),
    (datetime(2026, 11, 2, 3, 31, tzinfo=UTC), None, "overdue", "2026-11-01"),
    (datetime(2026, 11, 2, 3, 31, tzinfo=UTC), date(2026, 11, 1), "ok", "2026-11-01"),
])
def test_due_state_waits_for_the_first_chain_after_automated_from(now, covered, state, expected):
    weekly = _reg().report("CIS-01")
    assert weekly.schedule == "weekly" and weekly.automated_from == date(2026, 10, 30)
    out = generate.due_state(weekly, _last(covered) if covered else None, now)
    assert out == {"state": state, "expected_period_end": expected}


def test_monthly_report_automated_late_is_not_overdue_for_the_month_before():
    data04 = _reg().report("DATA-04")
    assert data04.schedule == "monthly" and data04.automated_from == date(2026, 10, 30)
    # September was chained on 01/10 (business day 2 = 02/10), before the report existed.
    assert generate.due_state(data04, None, datetime(2026, 10, 30, 6, 0, tzinfo=UTC))["state"] == "pending_first"
    # October is chained on 02/11 (business day 2 = Tue 03/11) and is expected from then.
    late = generate.due_state(data04, None, datetime(2026, 11, 3, 4, 0, tzinfo=UTC))
    assert late == {"state": "overdue", "expected_period_end": "2026-10-31"}


def test_automated_from_parses_and_refuses_a_bad_date():
    reg = _reg()
    assert reg.report("EX-04").automated_from is None
    assert reg.report("OPS-13").summary()["automated_from"] == "2026-10-30"
    assert registry._date("X", "2026-10-30") == date(2026, 10, 30)
    with pytest.raises(ValueError):
        registry._date("X", "30/10/2026")


def test_due_state_of_reports_not_generated_here():
    assert generate.due_state(_reg().report("FI-07"), None, NOW)["state"] == "not_automated"   # R3 generates FI-01


def test_register_lists_every_report_and_flags_failures():
    reg = _reg()
    rows = generate.register_rows(reg, {"EX-04": _last(DAY, failed=2)}, NOW)
    assert len(rows) == len(reg.reports)
    out = generate.render_register(reg, reg.report("EX-06"), DAY, rows, NOW)
    assert "EX-04" in out.output["attention"]
    assert out.output["registry"]["automated"] == 20         # EX-04, EX-06; R2: OPS-12, TP-03; R3: 8 money; R4: 8
    assert "## Generated here" in out.markdown and "| P0 |" in out.markdown


def test_register_names_its_own_run_instead_of_running():
    reg = _reg()
    rows = generate.register_rows(reg, {"EX-06": _last(DAY, status="running")}, NOW)
    out = generate.render_register(reg, reg.report("EX-06"), DAY, rows, NOW)
    entry = next(e for e in out.output["reports"] if e["id"] == "EX-06")
    assert entry["last_status"] == "this_run"
    assert "| this run |" in out.markdown and "| running |" not in out.markdown


def test_render_refuses_a_report_without_renderer():
    import asyncio

    reg = _reg()
    with pytest.raises(LookupError):
        asyncio.run(generate.render(None, reg, reg.report("FI-07"), DAY, DAY, NOW))
