"""S282 Sentry triage (AA-CIS-API-1F) — gate_ledger is unnested only when it is a JSON array.

The SQL itself runs on real Postgres in tests/integration/test_s282_gate_ledger_object_row.py; this file pins the
fragment, that every gate_ledger reader that the KPI job and the admin views run uses it, and that the Dev fixture
writes the contract shape.
"""
from __future__ import annotations

import pytest

from shared.repository.jsonb_sql import json_array


def test_fragment_keeps_an_array_and_replaces_anything_else():
    assert json_array("cp.gate_ledger") == (
        "(CASE WHEN jsonb_typeof(cp.gate_ledger) = 'array' THEN cp.gate_ledger ELSE '[]'::jsonb END)")


@pytest.mark.parametrize("bad", ["", "cp.gate_ledger; DROP TABLE x", "1col", "a.b.c", "cp.Gate"])
def test_fragment_refuses_anything_but_a_column_name(bad):
    with pytest.raises(ValueError):
        json_array(bad)


def test_readers_unnest_through_the_fragment():
    from api.routers import admin_content_quality
    from services.writing_memory import disagreements, kpi, shadow

    guarded = json_array("cp.gate_ledger")
    for sql in (kpi._T9_SQL, disagreements._PIECE_SQL, shadow._T9_SQL, admin_content_quality._FAILED_GATES_SQL):
        assert "jsonb_array_elements(cp.gate_ledger)" not in sql
        assert f"jsonb_array_elements({guarded})" in sql
    assert kpi._T9_SQL.count(guarded) == 2


def test_e2e_fixture_writes_an_array_ledger():
    from services.tenant_account import e2e_fixtures

    assert "'approved', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb" in e2e_fixtures.INSERT_PIECE_SQL
    assert "'{}'::jsonb" not in e2e_fixtures.INSERT_PIECE_SQL
