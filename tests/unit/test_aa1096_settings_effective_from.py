"""AA-1096 (S280 UX fix round) — a settings change may not be dated in the past (api/services/settings_registry.py
effective_start): the reader ranks values by effective_from, so a backdated approval would sit under the value in force
and never take effect while the page said it changed. No DB."""
from datetime import datetime, timedelta, timezone

import pytest

from api.services import settings_registry as reg

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def test_empty_means_when_approved():
    assert reg.effective_start(None, now=NOW) is None


def test_a_future_time_is_kept():
    later = NOW + timedelta(days=2)
    assert reg.effective_start(later, now=NOW) == later


def test_a_time_inside_the_grace_counts_as_now():
    assert reg.effective_start(NOW - timedelta(seconds=reg.PAST_GRACE_S - 1), now=NOW) is None
    assert reg.effective_start(NOW, now=NOW) is None


def test_an_older_time_is_refused():
    with pytest.raises(reg.SettingsError) as e:
        reg.effective_start(NOW - timedelta(hours=1), now=NOW)
    assert (e.value.status, e.value.code) == (422, "effective_in_past")


def test_a_time_without_zone_is_utc():
    naive = (NOW + timedelta(hours=1)).replace(tzinfo=None)
    assert reg.effective_start(naive, now=NOW) == NOW + timedelta(hours=1)
    with pytest.raises(reg.SettingsError):
        reg.effective_start((NOW - timedelta(hours=1)).replace(tzinfo=None), now=NOW)
