"""S279 lane L2 (AA-1096) — serve chosen settings values in a unit test, as an approved platform row would.

    from tests.unit.settings_serve import serve
    with serve({"cis.quality.pass_line": 7.5}):
        ...

Patches ``shared.settings.READER.get`` (every reader goes through it); a key not given keeps its value in force (the
compiled default in unit tests). No DB.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from shared import settings


@contextmanager
def serve(values: dict):
    real = settings.READER.get

    def fake(key, tenant_id=None):
        return values[key] if key in values else real(key, tenant_id)

    with patch.object(settings.READER, "get", side_effect=fake):
        yield values


def bar_with(channel: str, **numbers) -> dict:
    """``{setting key: Channel bar}``: the seeded bar of ``channel`` with ``numbers`` laid over it (for ``serve``)."""
    from services.quality import settings_keys

    key = settings_keys.CHANNEL_BARS[channel]
    return {key: {**settings.definition(key).default_value, **numbers}}


def serve_from_env(monkeypatch, key: str, env: str, default):
    """Serve ``key`` from the env var ``env`` read at call time (``default`` when unset): lets a test written for an
    env switch that became a setting (S279 L2: T2_JUDGE_GATE -> cis.quality_modes.t2_judge_gate) keep driving it with
    monkeypatch.setenv / delenv. ``default`` is the value the test was written for (main's code default)."""
    import os

    real = settings.READER.get

    def fake(k, tenant_id=None):
        if k == key:
            raw = os.environ.get(env)
            return default if raw is None or not raw.strip() else raw.strip().lower()
        return real(k, tenant_id)

    monkeypatch.setattr(settings.READER, "get", fake)
