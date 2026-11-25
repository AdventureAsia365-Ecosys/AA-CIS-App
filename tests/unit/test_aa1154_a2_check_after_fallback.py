"""AA-1154 (A2, CIS 491) — an empty cis.ops.assist_check_after_min never reaches the run record.

The settings list type has no minimum length, so an admin may save []; the run record needs 1..10 checkpoints (CIS 490
ops_assist_run_check_after_chk). ``ops_assist_settings.snapshot()`` reads an empty value as the compiled default, so
"I ran this" never fails on a legal edit. No database: the settings reader is patched."""
from __future__ import annotations

import pytest

from shared import ops_assist_settings, settings

_real_get = settings.get


@pytest.mark.parametrize("value", [[], None])
def test_an_empty_checkpoint_list_reads_as_the_compiled_default(monkeypatch, value):
    def get(key, tenant_id=None):
        return value if key == ops_assist_settings.CHECK_AFTER_MIN else _real_get(key, tenant_id)

    monkeypatch.setattr(settings, "get", get)
    default = settings.definition(ops_assist_settings.CHECK_AFTER_MIN).default_value
    assert default, "the compiled default has at least one checkpoint"
    assert ops_assist_settings.snapshot()["check_after_min"] == sorted({int(x) for x in default})


def test_a_set_checkpoint_list_is_kept_ascending_and_once(monkeypatch):
    def get(key, tenant_id=None):
        return [45, 5, 45] if key == ops_assist_settings.CHECK_AFTER_MIN else _real_get(key, tenant_id)

    monkeypatch.setattr(settings, "get", get)
    assert ops_assist_settings.snapshot()["check_after_min"] == [5, 45]
