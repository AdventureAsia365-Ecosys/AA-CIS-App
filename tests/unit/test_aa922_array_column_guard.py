"""AA-922 — no SQL in api/, services/ or shared/ unnests a content_piece JSON-array column without the array guard.

``acp_shared.content_piece.gate_ledger``, ``flags``, ``repair_log`` and ``discarded_attempts`` are JSON arrays by
contract, but no CHECK enforces it. One object-shaped row (a Dev fixture wrote ``gate_ledger = '{}'``) failed the
weekly writing-memory KPI job twice. Every ``jsonb_array_elements`` / ``jsonb_array_length`` / ``||`` on those columns
must go through ``shared.repository.jsonb_sql.json_array`` (or its own ``CASE WHEN jsonb_typeof(...) = 'array'``): a
``WHERE jsonb_typeof(x) = 'array' AND jsonb_array_length(x) > 0`` is not enough, Postgres does not order ``AND``.
The SQL itself runs on real Postgres in tests/integration/test_aa922_array_column_readers.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCOPE = ("api", "services", "shared")
COLUMNS = r"(?:gate_ledger|flags|repair_log|discarded_attempts)"
# a raw column (optionally aliased, optionally inside COALESCE) as the first argument of an array function, or a
# raw column on either side of a jsonb concatenation
UNGUARDED = re.compile(
    rf"jsonb_array_(?:elements(?:_text)?|length)\(\s*(?:COALESCE\(\s*)?(?:\w+\.)?{COLUMNS}\b"
    rf"|(?<![\w'])(?:\w+\.)?{COLUMNS}\s*\|\|"
    rf"|\|\|\s*(?:COALESCE\(\s*)?(?:\w+\.)?{COLUMNS}\b",
    re.IGNORECASE)


def _hits(text: str) -> list[str]:
    return [m.group(0) for m in UNGUARDED.finditer(text)]


@pytest.mark.parametrize("sql", [
    "FROM acp_shared.content_piece cp, jsonb_array_elements(cp.gate_ledger) g",
    "jsonb_array_elements(COALESCE(cp.flags, '[]'::jsonb)) g",
    "jsonb_array_elements(cp.gate_ledger || COALESCE(cp.flags, '[]'::jsonb)) g",
    "WHERE jsonb_typeof(repair_log) = 'array' AND jsonb_array_length(repair_log) > 0",
    "AND jsonb_array_length(cp.flags) > 0",
    "jsonb_array_elements_text(discarded_attempts)",
])
def test_scanner_catches_the_pre_fix_shapes(sql):
    assert _hits(sql), sql


@pytest.mark.parametrize("sql", [
    "jsonb_array_elements((CASE WHEN jsonb_typeof(cp.gate_ledger) = 'array' THEN cp.gate_ledger "
    "ELSE '[]'::jsonb END)) g",
    "jsonb_array_elements({_LEDGER} || {_FLAGS}) g",
    "jsonb_array_length(discarded) = 0",
    "p.flags @> '[{\"gate\": \"degraded\"}]'::jsonb",
    "jsonb_array_length(qs.lessons_extracted) > 0",
])
def test_scanner_passes_guarded_shapes(sql):
    assert not _hits(sql), sql


def test_no_unguarded_array_column_reader_in_app_code():
    found = []
    for top in SCOPE:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "migrations" in path.parts:
                continue
            for hit in _hits(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not found, "unnest these through shared.repository.jsonb_sql.json_array:\n" + "\n".join(found)


def test_fixed_readers_use_the_fragments():
    from api.routers import admin_content_quality
    from services.writing_memory import backfill
    from shared.repository.jsonb_sql import FLAGS, GATE_LEDGER, json_array

    assert FLAGS == json_array("cp.flags")
    assert f"jsonb_array_elements({FLAGS})" in admin_content_quality._FAILED_GATES_SQL
    assert f"jsonb_array_elements({GATE_LEDGER})" in admin_content_quality._FAILED_GATES_SQL
    where, _ = admin_content_quality._piece_filters(None, None, None, "F2_lexicon", None)
    assert f"jsonb_array_elements({GATE_LEDGER} || {FLAGS})" in where
    assert f"jsonb_array_length({json_array('repair_log')}) > 0" in backfill._T9_SQL
