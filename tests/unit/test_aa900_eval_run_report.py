"""AA-900 — the read-only eval run report (scripts/llm_ops/eval_run_report.py): argument checks and the JSON it builds
from a run row and its llm_call_log spend (fake connection, no DB)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from scripts.llm_ops import eval_run_report as rep

RUN = "a9000000-0000-4000-8000-000000000001"


class FakeConn:
    def __init__(self, run):
        self.run = run
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("row", args))
        return self.run

    async def fetch(self, sql, *args):
        self.calls.append(("rows", args))
        return [{"stage": "adhoc_aa900", "real_stage": "t9_write", "role": "writer", "calls": 4, "cost_usd": 0.12},
                {"stage": "adhoc_aa900", "real_stage": "writing_memory_eval", "role": "judge", "calls": 8,
                 "cost_usd": 0.016}]


def test_parse_refuses_a_non_uuid_and_needs_one_target():
    assert rep._parse([RUN]).run_id == RUN
    assert rep._parse(["--latest", "t9_blog"]).latest == "t9_blog"
    with pytest.raises(ValueError):
        rep._parse(["1; DROP"])
    with pytest.raises(SystemExit):
        rep._parse([])


def test_report_sums_the_logged_spend_of_both_eval_labels():
    run = {"run_id": RUN, "verdict": "pass", "cost_usd": 0.136,
           "created_at": datetime(2026, 10, 29, tzinfo=timezone.utc),
           "planned": '{"probe": 3, "tune": 4, "holdout": 0}', "log_stage": "adhoc_aa900"}
    conn = FakeConn(run)
    out = asyncio.run(rep.report(conn, RUN))
    assert out["logged_calls"] == 12 and out["logged_cost_usd"] == pytest.approx(0.136)
    assert out["run"]["planned"] == {"probe": 3, "tune": 4, "holdout": 0}
    assert conn.calls[1][1][1] == ["writing_memory_eval", "adhoc_aa900"]


def test_unknown_run_is_reported_not_raised():
    out = asyncio.run(rep.report(FakeConn(None), RUN))
    assert out["error"].startswith("no writing_memory eval run")
