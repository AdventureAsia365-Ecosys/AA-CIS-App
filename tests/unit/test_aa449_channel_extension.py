"""AA-449 — the planning Channel value set (Slot.channel 4 -> 8 values at the time).

AA-840 deleted the N6 allocator (`compute_slot_grid()`) and the `Slot` model these tests used to
exercise; what is left pins the `Channel` Literal, which `api/routers/admin.py::_VALID_CHANNELS`,
`services/acp_shared/slate.py::CHANNEL_BARS` and migration 236's `tenant_config` CHECK mirror.
"""
import typing

from services.acp_planning.models import Channel
from services.acp_shared.slate import CHANNEL_BARS

_ALL_CHANNELS = ["blog", "facebook", "tiktok", "email", "linkedin", "instagram", "landing_page", "ads"]


def test_channel_literal_has_exactly_eight_values():
    """Pins the exact value set — a future edit that adds/removes a channel here should also
    touch api/routers/admin.py::_VALID_CHANNELS and frontend/app/admin/tenants/page.tsx::
    ALL_CHANNELS (this test doesn't check those files, just documents the expected set)."""
    assert set(typing.get_args(Channel)) == set(_ALL_CHANNELS)


def test_channel_literal_matches_slate_channels():
    assert set(typing.get_args(Channel)) == set(CHANNEL_BARS)
