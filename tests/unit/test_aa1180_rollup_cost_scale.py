"""AA-1180 — the usage rollup keeps the ledgers' cost precision (pure, no DB).

- The rollup SQL sums every ledger's per-row cost as unbounded numeric: nothing in the INSERT ... SELECT, the ledger
  totals or the rollup totals casts cost to a fixed scale (the column, numeric(20,8) since CIS 494, is the only bound).
- compare() stays exact: a Jev window whose ledger total has 8 decimals is equal only when the rollup has the same 8.
- The explorer reports cost with COST_SCALE (8) decimals, so a sub-micro-dollar cost survives to the response.
Invented figures only.
"""
from __future__ import annotations

import re
from decimal import Decimal

from api.services import usage_dims as ud
from api.services import usage_explorer as ue
from api.services import usage_rollup as ur

_FIXED_SCALE = re.compile(r"numeric\s*\(\s*\d+\s*,\s*\d+\s*\)|::decimal\s*\(|round\s*\(", re.IGNORECASE)


def test_cost_scale_is_the_widest_ledger_scale():
    # shared.decision_log.cost_usd is numeric(12,8), the widest of the four source ledgers.
    assert ur.COST_SCALE == 8


def test_rollup_sql_never_fixes_the_cost_scale():
    sqls = [ur.insert_sql(), ur.ROLLUP_TOTALS_SQL] + [ur.raw_totals_sql(s) for s in ud.SOURCES]
    for sql in sqls:
        assert not _FIXED_SCALE.search(sql), sql
    assert "sum(u.cost_usd)" in ur.insert_sql()
    assert ")::numeric AS cost_usd" in ur.insert_sql()


def test_compare_is_exact_at_eight_decimals():
    raw = {"jev": {"ledger_rows": 41, "requests": 41, "tokens_in": 0, "tokens_out": 0,
                   "cost_usd": Decimal("0.06664078")}}
    assert ur.compare(raw, {"jev": {**raw["jev"]}})["ok"]
    rounded = {"jev": {**raw["jev"], "cost_usd": Decimal("0.066641")}}      # what numeric(18,6) stored before 494
    out = ur.compare(raw, rounded)
    jev = next(s for s in out["sources"] if s["source"] == "jev")
    assert not out["ok"] and jev["diff"] == {"cost_usd": "2.2E-7"}
    assert jev["ledger"]["cost_usd"] == "0.06664078"


def test_a_sub_micro_dollar_cost_survives_the_explorer():
    acc = ue.new_acc()
    for c in ("0.00000012", "0.00000066"):
        ue.add_into(acc, {"requests": 1, "cost_usd": Decimal(c)})
    m = ue.finish(acc, {"tours": 3, "tour_cost": Decimal("0.00000078"), "pieces": 0, "piece_cost": 0})
    assert m["cost_usd"] == 0.00000078 and m["cost_usd"] != 0
    assert m["cost_per_tour"] == 0.00000026
