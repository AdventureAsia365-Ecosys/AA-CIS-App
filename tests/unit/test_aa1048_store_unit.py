"""AA-1048 (Quality W2) — services/ground_truth/store.py without a DB: the queue items of a label set, the resolution
mapping, the append-only refusal and the DRY RUN paths (a fake connection). The SQL itself runs in
tests/integration/test_aa1048_ground_truth_store.py."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date

import pytest

from services.ground_truth import store


class FakeConn:
    def __init__(self, panel_rows=0, exists=False, resolved=(), rows=(), queued=None):
        self.panel_rows, self.exists, self.resolved, self.rows = panel_rows, exists, list(resolved), list(rows)
        self.queued = dict(queued or {})        # source_ref -> status of the items already in the queue
        self.inserted = []

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return self.panel_rows
        self.inserted.append(args)
        return "new-id"

    async def fetch(self, sql, *args):
        if "source_ref = ANY" in sql:
            refs = args[1]
            if self.exists:
                return [{"source_ref": r, "status": "open"} for r in refs]
            return [{"source_ref": r, "status": s} for r, s in self.queued.items() if r in refs]
        return self.resolved if "writing_memory_disagreement" in sql else self.rows

    @asynccontextmanager
    async def transaction(self):
        yield


def _row(h, lens, label, state, kind="agent_panel", lid=None, writer="t9_blog", split="train", sha="c1", span=0):
    return {"label_id": lid or f"{h}-{lens}", "claim_hash": h, "labeller_kind": kind, "lens": lens, "label": label,
            "adjudication": state, "writer": writer, "scope": "tenant", "tenant_id": "t-1",
            "claim_class": "fact.number", "set_id": "CL-1", "set_version": "1.0.0", "policy_sha": None,
            "split": split, "content_sha256": sha, "span_start": span}


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


def _batch_rows():
    """Six disputed claims: train / held-out, three writers, two objects; one agreed claim (never queued)."""
    spec = [("a", "t2", "held_out", "c3", 0), ("b", "t9_social", "held_out", "c2", 5),
            ("c", "t9_blog", "train", "c1", 0), ("d", "t9_blog", "held_out", "c9", 40),
            ("e", "t9_blog", "held_out", "c9", 10), ("f", "t9_blog", "held_out", "c4", 0)]
    rows = []
    for h, w, sp, sha, span in spec:
        rows += [_row(h, "promise", "violation", "disputed", writer=w, split=sp, sha=sha, span=span),
                 _row(h, "skeptic", "ok", "disputed", writer=w, split=sp, sha=sha, span=span),
                 _row(h, "source", "ok", "disputed", writer=w, split=sp, sha=sha, span=span)]
    rows += [_row("g", lens, "ok", "agreed", split="held_out") for lens in ("promise", "skeptic", "source")]
    return rows


def test_queue_order_is_held_out_first_then_t9_then_by_object_and_position():
    order = [it["claim_hash"] for it in store.queue_candidates(_batch_rows())]
    assert order == ["f", "e", "d", "b", "a", "c"]      # held-out blog (object c4, then c9 by span), social, t2, train
    held_t9 = store.queue_candidates(_batch_rows(), split="held_out", writers=["t9_blog", "t9_social"])
    assert [it["claim_hash"] for it in held_t9] == ["f", "e", "d", "b"]
    assert [it["claim_hash"] for it in store.queue_candidates(_batch_rows(), split="train")] == ["c"]
    with pytest.raises(ValueError, match="split must be"):
        store.queue_candidates(_batch_rows(), split="pooled")


def test_enqueue_batch_tops_the_queue_up_to_max_open():
    rows = _batch_rows()
    kw = {"today": date(2026, 11, 7), "split": "held_out", "writers": ["t9_blog", "t9_social"], "max_open": 2}
    conn = FakeConn()
    got = asyncio.run(store.enqueue(conn, rows, apply=True, **kw))
    assert got["added"] == 2 and got["candidates"] == 4 and got["remaining"] == 2 and got["batches_left"] == 1
    assert [a[1] for a in conn.inserted] == ["f-promise", "e-promise"]          # the first two in work order
    assert got["open_before"] == 0 and got["open_after"] == 2 and got["filter"]["max_open"] == 2
    # the batch is still open: nothing more is added
    conn = FakeConn(queued={"f-promise": "open", "e-promise": "open"})
    got = asyncio.run(store.enqueue(conn, rows, apply=True, **kw))
    assert got["added"] == 0 and got["already_queued"] == 2 and got["remaining"] == 2 and conn.inserted == []
    # one item resolved: the queue is topped up with the next one in order
    conn = FakeConn(queued={"f-promise": "resolved", "e-promise": "open"})
    got = asyncio.run(store.enqueue(conn, rows, apply=True, **kw))
    assert got["added"] == 1 and [a[1] for a in conn.inserted] == ["d-promise"] and got["remaining"] == 1
    # DRY RUN counts the same without writing
    conn = FakeConn(queued={"f-promise": "resolved", "e-promise": "resolved"})
    got = asyncio.run(store.enqueue(conn, rows, apply=False, **kw))
    assert got["added"] == 2 and got["remaining"] == 0 and got["batches_left"] == 0 and conn.inserted == []
    with pytest.raises(ValueError, match="max_open"):
        asyncio.run(store.enqueue(FakeConn(), rows, apply=False, today=date(2026, 11, 7), max_open=0))


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
