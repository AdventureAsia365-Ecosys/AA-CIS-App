"""S282 Sentry triage (AA-CIS-API-1A) — the label set browser reads every version in a fixed number of queries.

`list_sets` used to read the rows and the corrections of each version one by one (2 + 2N queries, an N+1 Sentry
flagged). It now reads them with one query each, grouped in Python; the per-set result is unchanged. Fake connection,
invented names only; the SQL runs on real Postgres in tests/integration/test_aa1063_claim_review_db.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from services.ground_truth import review as rv
from tests.unit.test_aa1063_claim_review import _relabel, _rows


class _Rec(dict):
    """An asyncpg Record stand-in: dict(r) and r["col"] both work."""


def _versioned(rows, set_id, version):
    return [{**r, "set_id": set_id, "set_version": version} for r in rows]


class _Conn:
    def __init__(self, label_rows, relabels):
        self.label_rows = label_rows
        self.relabels = relabels
        self.fetches = 0

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        self.fetches += 1
        if sql.startswith("SELECT DISTINCT set_id, set_version"):
            keys = sorted({(r["set_id"], r["set_version"]) for r in self.label_rows})
            return [_Rec(set_id=a, set_version=b) for a, b in keys]
        if "writing_memory_disagreement" in sql:
            return []
        if "FROM shared.eval_claim_relabel" in sql:
            wanted = set(zip(*args)) if len(args) == 2 and isinstance(args[0], list) else {tuple(args[:2])}
            out = [r for r in self.relabels if (r["set_id"], r["set_version"]) in wanted]
            return [_Rec(r) for r in sorted(out, key=lambda r: (r["created_at"], r["relabel_id"]))]
        if "FROM shared.eval_claim_label" in sql:
            wanted = set(zip(*args)) if isinstance(args[0], list) else {tuple(args[:2])}
            out = [r for r in self.label_rows if (r["set_id"], r["set_version"]) in wanted]
            key = (lambda r: (r["set_id"], r["set_version"], r["claim_hash"], r["labeller_kind"], r["lens"] or "~"))
            return [_Rec(r) for r in sorted(out, key=key)]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


def _data(n_sets):
    label_rows, relabels = [], []
    for i in range(n_sets):
        set_id, version = f"ZZ-S282-{i}", "1.0.0"
        rows = _versioned(_rows(lead="ok" if i % 2 else None), set_id, version)
        label_rows += rows
        if i % 3 == 0:
            relabels.append({**_relabel(rows, "violation"), "set_id": set_id, "set_version": version})
    return label_rows, relabels


def _expected(label_rows, relabels):
    keys = sorted({(r["set_id"], r["set_version"]) for r in label_rows})
    out = []
    for set_id, version in keys:
        rows = sorted((r for r in label_rows if (r["set_id"], r["set_version"]) == (set_id, version)),
                      key=lambda r: (r["claim_hash"], r["labeller_kind"], r["lens"] or "~"))
        rel = [r for r in relabels if (r["set_id"], r["set_version"]) == (set_id, version)]
        out.append({"set_id": set_id, "set_version": version, **rv.set_summary(rows, rel),
                    "queue": {"open": 0, "resolved": 0}})
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("n_sets", [1, 5])
async def test_list_sets_makes_four_queries_whatever_the_number_of_sets(n_sets):
    conn = _Conn(*_data(n_sets))
    out = await rv.list_sets(conn)
    assert len(out) == n_sets
    assert conn.fetches == 4


@pytest.mark.asyncio
async def test_list_sets_result_is_the_per_set_summary():
    label_rows, relabels = _data(5)
    out = await rv.list_sets(_Conn(label_rows, relabels))
    assert out == _expected(label_rows, relabels)
    assert any(s["relabels"] for s in out)


@pytest.mark.asyncio
async def test_list_sets_with_no_sets_reads_nothing_more():
    conn = _Conn([], [])
    assert await rv.list_sets(conn) == []
    assert conn.fetches == 2
