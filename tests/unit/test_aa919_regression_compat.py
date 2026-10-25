"""AA-919 (WM-17) — CIS 284 widens shared.prompt_eval_runs; the legacy writer services/eval/regression.py
(_write_eval_run, pipeline s1_old) must keep working unchanged. The real insert runs in 284's guard 3 and in
tests/integration/test_aa919_migration_284.py; this checks the shapes without a DB:

- the legacy INSERT names none of the new columns, and every new NOT NULL column has a DEFAULT;
- the defaults satisfy the legacy CHECK (run_kind 'regression', writer NULL), and 's1_old' stays in the pipeline CHECK.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from services.eval import regression

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "api" / "migrations" / "284_eval_golden_set.sql").read_text(encoding="utf-8")


class _Conn:
    def __init__(self):
        self.sql = None

    async def execute(self, sql, *args):
        self.sql = sql


def _legacy_columns() -> list[str]:
    conn = _Conn()
    result = {"pipeline": "s1_old", "prompt_version": "abc", "tour_count": 1, "details": {}}
    asyncio.run(regression._write_eval_run(conn, result, None, False, "manual"))
    cols = re.search(r"INSERT INTO shared\.prompt_eval_runs \((.*?)\)\s*VALUES", conn.sql, re.S).group(1)
    return [c.strip() for c in cols.split(",")]


def _added_columns() -> dict[str, str]:
    block = MIGRATION.split("ALTER TABLE shared.prompt_eval_runs\n    ADD COLUMN", 1)[1].split(";", 1)[0]
    out = {}
    for line in ("ADD COLUMN" + block).splitlines():
        m = re.match(r"\s*ADD COLUMN IF NOT EXISTS (\w+)\s+(.*?),?$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_legacy_insert_names_no_new_column_and_new_not_nulls_have_defaults():
    added = _added_columns()
    assert set(added) == {"run_kind", "writer", "release_id", "golden_set_id", "tiers", "stop_reason", "verdict",
                          "cost_cap_usd", "judge_kappa", "judge_counts", "fingerprint", "finished_at"}
    assert not set(_legacy_columns()) & set(added)
    for col, spec in added.items():
        if "NOT NULL" in spec:
            assert "DEFAULT" in spec, col
    assert "DEFAULT 'regression'" in added["run_kind"]


def test_legacy_defaults_fit_the_new_checks():
    assert re.search(r"CHECK \(pipeline IN \('s1_old', 's1_from_atom', 'writing_memory'\)\)", MIGRATION)
    assert "CHECK (pipeline = 'writing_memory' OR (run_kind = 'regression' AND writer IS NULL))" in MIGRATION
