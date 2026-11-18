"""AA-1127 (C1, S283 UI audit) — the AI gateway idle window is the setting cis.llm.idle_after_hours (CIS 453).

The status ("ok" while the newest answered call is at most that old, then "unknown" with no reason, the row badge's
"No recent calls") and the "Need a look" tile (frontend/lib/aiGateway.ts needsLook, which leaves an idle row out) read
one value an administrator can change. No DB."""
from datetime import datetime, timedelta, timezone

import pytest

from api.services import gateway_health_settings as gh
from api.services import llm_availability as la

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def served(monkeypatch):
    values: dict = {}

    def fake(key, tenant_id=None):
        if key in values:
            return values[key]
        raise KeyError(key)
    from shared import settings
    monkeypatch.setattr(settings, "get", fake)
    return values


def test_the_key_is_the_seeded_one():
    assert gh.IDLE_AFTER_HOURS == "cis.llm.idle_after_hours"


def test_the_setting_moves_the_idle_window(served):
    served[gh.IDLE_AFTER_HOURS] = 24
    assert la.idle_after_hours() == 24.0
    assert la.decide_status(NOW - timedelta(hours=7), [], NOW) == ("ok", None)
    served[gh.IDLE_AFTER_HOURS] = 2
    assert la.decide_status(NOW - timedelta(hours=3), [], NOW) == ("unknown", None)


def test_the_connection_status_reads_the_same_window(served):
    served[gh.IDLE_AFTER_HOURS] = 24
    model = {"state": la.CLOSED, "consecutive_failures": 0, "_last_ok": NOW - timedelta(hours=10)}
    assert la.connection_status(True, [model], NOW) == "ok"
    served[gh.IDLE_AFTER_HOURS] = 6
    assert la.connection_status(True, [model], NOW) == "unknown"


@pytest.mark.parametrize("bad", [None, True, "six", 0, -3])
def test_an_unusable_value_keeps_the_seeded_default(served, bad):
    served[gh.IDLE_AFTER_HOURS] = bad
    assert la.idle_after_hours() == float(la.STALE_OK_HOURS)


def test_a_key_not_seeded_yet_keeps_the_seeded_default(served):
    assert la.idle_after_hours() == float(la.STALE_OK_HOURS) == 6.0
