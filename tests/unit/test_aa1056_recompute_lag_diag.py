"""AA-1056: the read-only recompute lag diagnostic (scripts/aa1056/recompute_lag_diag.py).

The script runs in the api container through ecsx.sh; these tests pin the parts that need no database: the verdict
line and the helpers that keep personal data and free text out of the output.
"""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1056", "recompute_lag_diag.py")
AT = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def diag():
    spec = importlib.util.spec_from_file_location("aa1056_recompute_lag_diag", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_is_tracked_next_to_its_usage_line():
    # scripts/ is git-ignored in this repo: the file must be force-added or the ecsx.sh live step has nothing to run
    assert os.path.isfile(_PATH)


def test_the_script_opens_a_read_only_ssl_session(diag):
    src = open(_PATH, encoding="utf-8").read()
    assert '"default_transaction_read_only": "on"' in src
    assert 'ssl="require"' in src
    assert 'os.environ["DATABASE_URL"]' in src
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
        assert verb not in src.upper().replace("DELETE OR UPDATE", "")


def test_helpers_never_print_personal_data_or_free_text(diag):
    assert diag.who("someone@example.test") == "staff"
    assert diag.who("ops_sampler") == "ops_sampler"
    assert diag.who(None) == "-"
    assert diag.actor_kind("someone@example.test") == "staff"
    assert diag.actor_kind("pipeline") == "pipeline"
    assert diag.actor_kind("script:zz1056_fixture") == "script"
    assert diag.reason_kind("master_status=inactive:zz1056-tour") == "master_status=inactive"
    assert diag.reason_kind(None) == "-"
    assert diag.short("0123456789abcdef") == "01234567"
    assert diag.short(None) == "-"


def test_secs_and_parse_ts(diag, monkeypatch):
    assert diag.secs(AT + timedelta(seconds=90), AT) == "90.0"
    assert diag.secs(None, AT) == "null"
    monkeypatch.setenv("SINCE", "2026-11-05T00:00:00")
    assert diag.parse_ts("SINCE", None) == datetime(2026, 11, 5, tzinfo=timezone.utc)
    monkeypatch.delenv("SINCE")
    assert diag.parse_ts("SINCE", AT) == AT


def _job(created_s, finished_s=None, reason="master_status=inactive:zz1056-tour"):
    return {"reason": reason, "created_at": AT + timedelta(seconds=created_s),
            "finished_at": None if finished_s is None else AT + timedelta(seconds=finished_s)}


def test_verdict_effects_ran_but_no_recompute_was_enqueued(diag):
    v = diag.verdict({"trig": "after_commit"}, [], None, AT)
    assert "effects=ran" in v
    assert "enqueue=no recompute row created near the event" in v
    assert "lag_to_finished=never" in v
    assert "aa713_recompute_enqueue_failed" in v


def test_verdict_request_skipped_its_effects(diag):
    first = _job(7200, 7300, reason="sweep")
    v = diag.verdict({"trig": "sweep"}, [], first, AT)
    assert "did not run in the request" in v
    assert "lag_to_finished=7300s" in v
    assert "never ran run_after_commit" in v


def test_verdict_status_change_recompute_that_did_not_finish(diag):
    near = [_job(5)]
    first = _job(5 + 1800 + 60, 5 + 1800 + 120, reason="sweep")
    v = diag.verdict(None, near, first, AT)
    assert v.startswith("effects=unknown")
    assert "enqueue=status-change recompute created" in v
    assert "did not finish" in v


def test_verdict_only_other_rows_near_the_event(diag):
    near = [_job(10, 40, reason="ingest")]
    v = diag.verdict({"trig": "after_commit"}, near, _job(10, 40, reason="ingest"), AT)
    assert "a fold into one of them is possible" in v
    assert "lag_to_finished=40s" in v
    assert v.endswith("likely_cause=see the rows above")
