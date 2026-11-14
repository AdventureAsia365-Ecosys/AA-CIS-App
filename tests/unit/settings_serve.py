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


def serve_retired_env(monkeypatch, keys=None):
    """AA-1096 l5 (CIS 424): serve each retired env-override key (``snapshots/aa1096_retired_env_overrides.json``) from
    its old env var, read at call time, as an approved platform value would be (invalid or unset: the real
    resolution). Lets a test written for the env switch keep driving the setting with monkeypatch.setenv / delenv
    after CIS 424 retired the override in the app. ``keys`` limits it to some keys (default every retired key)."""
    import json
    import os
    from pathlib import Path

    from shared.settings.reader import Resolution, coerce, parse_text

    retired = json.loads((Path(__file__).resolve().parent / "snapshots" / "aa1096_retired_env_overrides.json")
                         .read_text(encoding="utf-8"))
    if keys is not None:
        retired = {k: retired[k] for k in keys}
    real = settings.READER.resolve

    def fake(key, tenant_id=None):
        env = retired.get(key)
        raw = os.environ.get(env) if env else None
        if raw is not None and raw.strip():
            d = settings.definition(key)
            value, problem = parse_text(d, raw)
            if problem is None:
                return Resolution(key, coerce(d, value), "platform", version=1)
        return real(key, tenant_id)

    monkeypatch.setattr(settings.READER, "resolve", fake)
