"""AA-1055 PR 1 (S273 design section 3) — why a Slate candidate misses a Channel's Bar.

`bar_misses` / `bar_status` split a miss into no_data (no market has a demand number), below_bar (a number under the
Bar), few_questions and few_said, using the same `>=` tests as `_clears_bar`; `bar_status_counts` counts them per
Channel with a Bar for the t7_slate job result. The tenant-visible `cleared_bar_reason` is unchanged.
(The MAX-demand SQL is covered by tests/integration/test_aa741_s271_blog_slate_sql.py.)

Invented places only. Pure: no DB.
"""
import pytest

from services.acp_shared import slate
from services.acp_shared.slate import (CHANNEL_BARS, Candidate, _clears_bar, bar_misses, bar_status, bar_status_counts,
                                       bars)


def _seg(sid, demand, questions=5, said=200, market="UK"):
    return Candidate(segment_id=sid, route_id=None, score=1.0, demand=demand, questions=questions, said=said,
                     place=f"Place {sid}", demand_market=market if demand is not None else None)


@pytest.mark.parametrize("demand,questions,expected", [
    (2400, 5, []),
    (1000, 3, []),                                    # the Bar is >=
    (None, 5, ["no_data"]),
    (0, 5, ["below_bar"]),                            # a real zero is a number, not missing data
    (999, 5, ["below_bar"]),
    (5000, 2, ["few_questions"]),
    (None, 0, ["no_data", "few_questions"]),
    (40, 1, ["below_bar", "few_questions"]),
])
def test_blog_bar_misses(demand, questions, expected):
    c = _seg("a", demand, questions)
    assert bar_misses("blog", c) == expected
    assert bar_status("blog", c) == (expected[0] if expected else "cleared")
    assert (not expected) == _clears_bar("blog", c)[0]   # same tests as the Bar itself


def test_social_bar_reads_said_only():
    assert bar_misses("linkedin", _seg("a", None, 0, said=150)) == []
    assert bar_misses("linkedin", _seg("a", 9000, 9, said=149)) == ["few_said"]


def test_the_tenant_visible_reason_is_unchanged():
    _cleared, reason = _clears_bar("blog", _seg("a", 2400))
    assert set(reason) == {"channel", "on_demand", "needs_demand", "demand", "demand_ok", "needs_questions",
                           "questions", "questions_ok", "needs_said", "said", "said_ok"}


def test_bar_status_counts_per_channel_with_a_bar():
    segments = [_seg("a", 2400), _seg("b", None), _seg("c", 300), _seg("d", 3000, questions=1),
                _seg("e", None, questions=0, said=10)]
    counts = bar_status_counts(["blog", "linkedin", "email"], segments, [])
    assert set(counts) == {"blog", "linkedin"}                  # on-demand Channels have no Bar
    assert counts["blog"] == {"cleared": 1, "no_data": 2, "below_bar": 1, "few_questions": 1, "few_said": 0}
    assert counts["linkedin"] == {"cleared": 4, "no_data": 0, "below_bar": 0, "few_questions": 0, "few_said": 1}
    assert sum(counts["blog"].values()) == len(segments)


def test_every_reason_is_exported():
    assert slate.BAR_MISS_REASONS == ("no_data", "below_bar", "few_questions", "few_said")
    assert {"bar_misses", "bar_status", "bar_status_counts"} <= set(slate.__all__)
    assert all(not CHANNEL_BARS[c]["on_demand"] for c in bar_status_counts(list(CHANNEL_BARS), [], []))


@pytest.mark.asyncio
async def test_the_t7_slate_job_result_carries_the_bar_counts(monkeypatch):
    from tests.unit.test_aa741_s271_blog_segment_grain import _HeldConn, _propose
    from tests.unit.test_aa1081_blog_topic_coverage import set_min_seed
    set_min_seed(monkeypatch, 0)        # S279: the Blog minimum seed setting is 600; this test runs with it off
    segments = [_seg("a", 2400), _seg("b", None), _seg("c", 300), _seg("d", 3000, questions=1)]
    result, _routes = await _propose(_HeldConn(), segments)      # tenant Channels: blog + linkedin
    assert result["bar_status"]["blog"] == {"cleared": 1, "no_data": 1, "below_bar": 1, "few_questions": 1,
                                            "few_said": 0}
    assert result["bar_status"]["linkedin"]["cleared"] == 4
    assert result["proposed_per_channel"]["blog"] == 1


# ── the before / after script (scripts/aa1055/slate_bar_compare.py) agrees with the module ─────────────────────────

def _compare_script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "aa1055" / "slate_bar_compare.py"
    spec = importlib.util.spec_from_file_location("aa1055_slate_bar_compare", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sql_core(sql: str) -> str:
    """Whitespace-normalised SQL without `--` comments."""
    return " ".join(" ".join(line.split("--", 1)[0] for line in sql.splitlines()).split())


@pytest.mark.parametrize("demand", [None, 0, 999, 1000, 4000])
@pytest.mark.parametrize("questions", [0, 2, 3])
@pytest.mark.parametrize("said", [0, 149, 150])
def test_the_script_bar_status_equals_the_module(demand, questions, said):
    script = _compare_script()
    c = Candidate(segment_id="a", route_id=None, score=1.0, demand=demand, questions=questions, said=said)
    for channel, spec in bars().items():
        assert script.bar_status(spec, demand, questions, said) == bar_status(channel, c), channel


def test_the_script_after_rule_is_the_module_sql():
    script = _compare_script()
    module = _sql_core(slate._SEGMENT_CANDIDATES_SQL)
    after = _sql_core(script.AFTER_SQL)
    # same per-market CTE and the same MAX demand / MAX questions (the module also names the demand market)
    assert after.split(" ) SELECT")[0] == module.split(" ) SELECT")[0]
    for part in ("MAX(pm.demand_volume) AS demand_volume", "MAX(pm.questions) AS questions", "MIN(pm.said) AS said"):
        assert part in after and part in module
    assert "DISTINCT ON" in script.BEFORE_SQL and "DISTINCT ON" not in module


def test_the_script_counts_per_channel_and_picks_bar_channels():
    script = _compare_script()
    rows = [{"demand_volume": 2400, "questions": 5, "said": 200}, {"demand_volume": None, "questions": 5, "said": 0}]
    counts = script.count_statuses(rows, ["blog", "linkedin"], bars())
    assert counts["blog"] == {"cleared": 1, "no_data": 1, "below_bar": 0, "few_questions": 0, "few_said": 0}
    assert counts["linkedin"] == {"cleared": 1, "no_data": 0, "below_bar": 0, "few_questions": 0, "few_said": 1}
    assert script.bar_channels(CHANNEL_BARS, "") == ["blog", "linkedin", "facebook", "instagram", "tiktok"]
    assert script.bar_channels(CHANNEL_BARS, "blog") == ["blog"]
    with pytest.raises(SystemExit):
        script.bar_channels(CHANNEL_BARS, "email")
