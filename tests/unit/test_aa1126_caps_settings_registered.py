"""AA-1126 (S283) — every spend-control key of the gateway (shared/llm_client/caps_settings.py, CIS 446) is registered in
the compiled settings defaults and is rendered on Platform › Settings (area Spend of the CIS tab): the registry list the
admin page reads (``api.services.settings_registry``) carries each key with its label, guide, unit, bounds or allowed
values, so a tech lead can edit it there. One source of truth: the code reads exactly these keys. No database: the
compiled defaults only."""
from __future__ import annotations

from api.services import settings_registry as reg
from shared import settings
from shared.llm_client import caps_settings as cs

MODES = {cs.CONNECTION_CAP_MODE: list(cs.CAP_MODES), cs.ANOMALY_MODE: list(cs.ANOMALY_MODES)}


def test_every_key_is_registered_in_area_spend_of_cis():
    shown = {d.key for d in reg._app_definitions("cis", "spend")}
    missing = [k for k in cs.ALL if k not in shown]
    assert not missing, f"not on Platform › Settings › CIS › Spend: {missing}"
    assert set(cs.RUN_CAP_KEYS.values()) <= set(cs.ALL)


def test_every_key_renders_with_what_the_page_needs():
    for key in cs.ALL:
        out = reg._definition_out(settings.definition(key))
        assert out["app"] == "cis" and out["area"] == "spend" and out["scope_kind"] == "platform", key
        assert out["label"] and out["description"] and out["enforced"] is True, key
        assert out["env_override"] is None, key                 # a setting, never an env var
        guide = out["guide"] or {}
        assert {"en", "vi"} <= set(guide), key
        for lang in ("en", "vi"):
            assert all(guide[lang].get(f) for f in ("what", "raise", "lower", "when", "example")), (key, lang)
        if key in MODES:
            assert out["value_type"] == "enum" and out["allowed_values"] == MODES[key], key
        else:
            assert out["value_type"] in ("usd", "numeric", "int"), key
            assert out["min_value"] is not None and out["max_value"] is not None and out["unit"], key
            assert out["min_value"] <= out["default_value"] <= out["max_value"], key


def test_the_defaults_are_todays_dev_behaviour():
    assert settings.get(cs.CONNECTION_CAP_MODE) == cs.HARD_STOP
    assert settings.get(cs.ANOMALY_MODE) == cs.ALERT        # never refuses by default
    assert cs.connection_cap_mode() == cs.HARD_STOP and cs.anomaly_mode() == cs.ALERT
