"""AA-1127 (C2 "spend truth"; tech lead decision S285 E2) — CIS 461: the three settings of the daily Cost Explorer check
are compiled with the seeded defaults, a plain label and a complete EN + VI guide (the reader lands in the code PR,
api/services/cost_explorer_daily.py). No DB."""
from api.services import cost_explorer_daily_settings as keys
from shared import settings


def test_the_three_keys_are_compiled_with_the_seeded_defaults():
    assert keys.ALL == (keys.ENABLED, keys.RUN_HOUR_UTC, keys.STALE_AFTER_HOURS)
    expected = {keys.ENABLED: (True, "medium"), keys.RUN_HOUR_UTC: (1, "low"), keys.STALE_AFTER_HOURS: (36, "low")}
    for key, (default, impact) in expected.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced) == ("cis", "spend", "platform", True)
        assert d.default_value == default and d.impact == impact and d.label
        guide = settings.guide(key)
        for lang in ("en", "vi"):
            assert all(guide[lang][part] for part in ("what", "raise", "lower", "when", "example")), (key, lang)
    assert settings.get(keys.ENABLED) is True and settings.get(keys.RUN_HOUR_UTC) == 1
    assert settings.get(keys.STALE_AFTER_HOURS) == 36
