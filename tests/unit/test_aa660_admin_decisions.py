"""AA-660 — admin decisions API: the question-update rules (same as the DB CHECKs, as readable 422s).

AA-601: the summary aggregation also guards against the "Acted on = NaN" bug — every stage / question
/ day aggregate must carry each numeric field as a real number, even for a stage that only shows up in
the cache-hit rollup or in the billed-calls read."""
import asyncio
from datetime import datetime, date, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from api.routers import admin_decisions as m
from api.routers.admin_decisions import QuestionUpdate, validate_update

_NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
_NUM = (int, float)


class _FakePool:
    """Returns per-SQL rows; a stage/day/question row carries ONLY the keys the test sets, mirroring
    what asyncpg gives (a FILTERed count is 0, but sum()/avg() can be NULL on an empty group)."""

    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows.get(sql, [])


def _summary(rows):
    pool = _FakePool(rows)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    return asyncio.run(m.summary(req, days=7, x_admin_secret="x"))


def _no_none_nums(d, fields):
    for k in fields:
        assert d.get(k) is not None, f"{k} is None/missing in {d}"
        assert isinstance(d[k], _NUM), f"{k} is not numeric in {d}"


def test_hits_only_stage_has_acted_zero_not_null(monkeypatch):
    """A stage present only in the cache-hit rollup, with a grey hit (never acted on), must report
    acted == 0 (not null/missing) — reproduces the live s1_judge_tiebreak NaN."""
    monkeypatch.setattr(m, "verify_admin_secret", lambda _s: None)
    out = _summary({
        m._SUMMARY_SQL: [],
        m._ORPHAN_SQL: [],
        m._CALLS_SQL: [],
        m._DAILY_SQL: [],
        m._STAGE_SQL: [],  # no ledger rows at all
        m._ALLOWLIST_SQL: [],
        m._HITS_SQL: [{"day": date(2026, 10, 9), "stage": "s1_judge_tiebreak",
                       "question_key": "q", "mode": "shadow", "zone": "grey", "hits": 3,
                       "last_used_at": _NOW}],
    })
    stage = next(s for s in out["stages"] if s["stage"] == "s1_judge_tiebreak")
    assert stage["acted"] == 0
    _no_none_nums(stage, m._STAGE_NUM_FIELDS)
    # The FE sums s.acted across stages — this must be a number, never NaN.
    acted_sum = sum(s["acted"] for s in out["stages"])
    assert acted_sum == 0


def test_stage_row_with_null_sums_is_zeroed(monkeypatch):
    """A ledger stage row whose sum()/avg() came back NULL (empty window) is coalesced to 0 so the
    FE never sums a null."""
    monkeypatch.setattr(m, "verify_admin_secret", lambda _s: None)
    out = _summary({
        m._SUMMARY_SQL: [],
        m._ORPHAN_SQL: [],
        m._CALLS_SQL: [],
        m._DAILY_SQL: [],
        m._STAGE_SQL: [{"stage": "s", "questions": 1, "verdicts": 0, "acted": 0, "errors": 0,
                        "skipped": 0, "cached": 0, "cost_usd": None, "avg_latency_ms": None,
                        "last_used_at": None}],
        m._ALLOWLIST_SQL: [],
        m._HITS_SQL: [],
    })
    stage = out["stages"][0]
    _no_none_nums(stage, m._STAGE_NUM_FIELDS)
    assert stage["cost_usd"] == 0


def test_calls_only_stage_appears_with_all_fields(monkeypatch):
    """A stage that billed Jev calls but has no ledger/hit rows still shows up in `stages` with every
    numeric field present."""
    monkeypatch.setattr(m, "verify_admin_secret", lambda _s: None)
    out = _summary({
        m._SUMMARY_SQL: [],
        m._ORPHAN_SQL: [],
        m._CALLS_SQL: [{"stage": "s1_judge_tiebreak", "calls": 4, "cost_usd": 0.2, "tokens_in": 9}],
        m._DAILY_SQL: [],
        m._STAGE_SQL: [],
        m._ALLOWLIST_SQL: [],
        m._HITS_SQL: [],
    })
    stage = next(s for s in out["stages"] if s["stage"] == "s1_judge_tiebreak")
    _no_none_nums(stage, m._STAGE_NUM_FIELDS)
    assert stage["acted"] == 0


def test_shadow_without_floors_is_fine():
    validate_update(QuestionUpdate(mode="shadow"))


def test_enforce_needs_a_calibration_record():
    with pytest.raises(HTTPException) as e:
        validate_update(QuestionUpdate(mode="enforce", accept_floor=0.9, reject_ceiling=0.1))
    assert e.value.status_code == 422 and "calibration" in e.value.detail


def test_enforce_needs_a_floor():
    with pytest.raises(HTTPException):
        validate_update(QuestionUpdate(mode="enforce", calibration_ref="docs/calibration/x.md"))


def test_reject_ceiling_must_be_below_accept_floor():
    with pytest.raises(HTTPException):
        validate_update(QuestionUpdate(mode="shadow", accept_floor=0.4, reject_ceiling=0.6))


def test_calibrated_enforce_passes():
    validate_update(QuestionUpdate(mode="enforce", accept_floor=0.85, reject_ceiling=0.15,
                                   calibration_ref="docs/calibration/a3_keyword_belongs.md"))


def test_floors_are_bounded():
    with pytest.raises(ValidationError):
        QuestionUpdate(mode="shadow", accept_floor=1.5)
