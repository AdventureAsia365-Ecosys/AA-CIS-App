"""AA-1048 (Quality W2) — services/ground_truth/store.py without a DB: the queue items of a label set, the resolution
mapping, the append-only refusal and the DRY RUN paths (a fake connection). The SQL itself runs in
tests/integration/test_aa1048_ground_truth_store.py."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date

import pytest

from services.ground_truth import store


class FakeConn:
    def __init__(self, panel_rows=0, exists=False, resolved=(), rows=()):
        self.panel_rows, self.exists, self.resolved, self.rows = panel_rows, exists, list(resolved), list(rows)
        self.inserted = []

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return self.panel_rows
        if "EXISTS" in sql:
            return self.exists
        self.inserted.append(args)
        return "new-id"

    async def fetch(self, sql, *args):
        return self.resolved if "writing_memory_disagreement" in sql else self.rows

    @asynccontextmanager
    async def transaction(self):
        yield


def _row(h, lens, label, state, kind="agent_panel", lid=None):
    return {"label_id": lid or f"{h}-{lens}", "claim_hash": h, "labeller_kind": kind, "lens": lens, "label": label,
            "adjudication": state, "writer": "t9_blog", "scope": "tenant", "tenant_id": "t-1",
            "claim_class": "fact.number", "set_id": "CL-1", "set_version": "1.0.0", "policy_sha": None}


ROWS = ([_row("h1", "promise", "violation", "disputed"), _row("h1", "skeptic", "ok", "disputed"),
         _row("h1", "source", "violation", "disputed")]
        + [_row("h2", lens, "ok", "audit") for lens in ("promise", "skeptic", "source")]
        + [_row("h3", lens, "ok", "agreed") for lens in ("promise", "skeptic", "source")]
        + [_row("h4", "promise", "ok", "disputed"), _row("h4", "skeptic", "violation", "disputed"),
           _row("h4", "source", "unsure", "disputed")]
        + [_row("h5", lens, "ok", "disputed") for lens in ("promise", "source")]
        + [_row("h5", None, "ok", "final", kind="tech_lead")])


def test_queue_candidates():
    items = {it["source_ref"]: it for it in store.queue_candidates(ROWS)}
    assert set(items) == {"h1-promise", "h2-promise", "h4-promise"}          # agreed and adjudicated left out
    assert items["h1-promise"]["machine_verdict"] == "violation" and items["h1-promise"]["human_label"] == "disputed"
    assert items["h2-promise"]["human_label"] == "audit"
    assert items["h4-promise"]["machine_verdict"] == "split"


def test_resolution_label_and_week():
    assert store.resolution_label("violation", "judge_right") == "violation"
    assert store.resolution_label("violation", "human_right") == "ok"
    assert store.resolution_label("ok", "human_right") == "violation"
    assert store.resolution_label("split", "human_right") == "unsure"
    assert store.resolution_label("ok", "unclear") == "unsure"
    assert store.week_start(date(2026, 11, 7)) == date(2026, 11, 2)


def test_insert_rows_dry_run_and_the_append_only_refusal():
    row = {c: None for c in store.COLUMNS}
    row.update(set_id="CL-1", set_version="1.0.0", labeller_kind="agent_panel")
    dry = asyncio.run(store.insert_rows(FakeConn(), [row], apply=False))
    assert dry["dry_run"] and dry["panel"] == 1 and dry["inserted"] == 0 and dry["sets"] == ["CL-1@1.0.0"]
    with pytest.raises(store.StoreRefused, match="new set version"):
        asyncio.run(store.insert_rows(FakeConn(panel_rows=3), [row], apply=True))
    lead = dict(row, labeller_kind="tech_lead")
    conn = FakeConn(panel_rows=3)
    done = asyncio.run(store.insert_rows(conn, [lead], apply=True))           # a tech-lead row may be added
    assert done["inserted"] == 1 and len(conn.inserted[0]) == len(store.COLUMNS)


def test_enqueue_dry_run_counts_new_items_only():
    got = asyncio.run(store.enqueue(FakeConn(exists=False), ROWS, today=date(2026, 11, 7), apply=False))
    assert got == {"candidates": 3, "added": 3, "week_start": "2026-11-02", "dry_run": True}
    got = asyncio.run(store.enqueue(FakeConn(exists=True), ROWS, today=date(2026, 11, 7), apply=False))
    assert got["added"] == 0
    conn = FakeConn()
    got = asyncio.run(store.enqueue(conn, ROWS, today=date(2026, 11, 7), apply=True))
    assert got["added"] == 3 and conn.inserted[0][0] == store.QUEUE_SOURCE


def test_pull_resolutions_builds_tech_lead_rows():
    resolved = [{"label_id": "h1-promise", "machine_verdict": "violation", "resolution": "human_right",
                 "resolved_by": "tech_lead"},
                {"label_id": "h5-promise", "machine_verdict": "ok", "resolution": "judge_right",
                 "resolved_by": "tech_lead"}]
    rows = [dict(r, **{c: r.get(c) for c in store.COLUMNS}) for r in ROWS]
    conn = FakeConn(resolved=resolved, rows=rows)
    got = asyncio.run(store.pull_resolutions(conn, "CL-1", "1.0.0", apply=False))
    assert got["resolved_items"] == 2 and got["tech_lead"] == 1 and got["dry_run"]   # h5 already has its lead row
    conn = FakeConn(resolved=[], rows=rows)
    assert asyncio.run(store.pull_resolutions(conn, "CL-1", "1.0.0", apply=True))["rows"] == 0
