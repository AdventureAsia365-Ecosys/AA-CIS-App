"""AA-756 — write decision_log.outcome from human actions (item 5).

Covers:
  - record_outcome(): the JSON object it writes, that it never raises, returns the row count, and
    works with both a pool-like (has .acquire) and a bare connection.
  - grounding.record_approved_outcome(): reuses source_text + sentence_units + subject_key so an
    approved sentence stamps exactly the subject_key the S1 run judged.
  - tenant_pipeline.atom_subject_key(): the single builder for the a3_atom_in_text key format
    (atom:<md5(day_text)[:10]>:<label[:200]>), identical to what ground_day_atoms uses.
Mock-only: no TypeSafe call, no DB."""
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.llm_client import decide as d


class _FakeConn:
    """Records the execute() calls and returns an asyncpg-style command tag."""

    def __init__(self, tag="UPDATE 3", raise_on_execute=False):
        self._tag = tag
        self._raise = raise_on_execute
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self._raise:
            raise RuntimeError("boom")
        return self._tag


class _FakePool:
    """A pool-like object: `acquire()` yields the same connection (same shape as _SingleConn)."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


# ── record_outcome ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_outcome_writes_json_object_and_returns_rowcount():
    conn = _FakeConn("UPDATE 5")
    n = await d.record_outcome(conn, "a1_claim_supported", "claim:abc:def",
                               truth=True, source="review_approve", ref="rq-1")
    assert n == 5
    sql, args = conn.calls[0]
    assert "UPDATE shared.decision_log" in sql
    assert args[0] == "a1_claim_supported" and args[1] == "claim:abc:def"
    payload = json.loads(args[2])
    assert payload["truth"] is True
    assert payload["source"] == "review_approve"
    assert payload["ref"] == "rq-1"
    assert "at" in payload and payload["at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_record_outcome_defaults_ref_to_null_and_coerces_truth():
    conn = _FakeConn("UPDATE 1")
    await d.record_outcome(conn, "a3_atom_in_text", "atom:x:y", truth=0, source="admin_atom_delete")
    payload = json.loads(conn.calls[0][1][2])
    assert payload["truth"] is False          # 0 coerced to a real bool
    assert payload["ref"] is None


@pytest.mark.asyncio
async def test_record_outcome_accepts_a_pool_like_object():
    conn = _FakeConn("UPDATE 2")
    n = await d.record_outcome(_FakePool(conn), "q", "s", truth=True, source="src")
    assert n == 2 and len(conn.calls) == 1


@pytest.mark.asyncio
async def test_record_outcome_never_raises_returns_zero_on_error():
    conn = _FakeConn(raise_on_execute=True)
    n = await d.record_outcome(conn, "q", "s", truth=True, source="src")
    assert n == 0                              # logged + swallowed, caller is never broken


@pytest.mark.asyncio
async def test_record_outcome_zero_when_no_rows_match():
    conn = _FakeConn("UPDATE 0")
    assert await d.record_outcome(conn, "q", "no-such-subject", truth=False, source="src") == 0


# ── grounding.record_approved_outcome ────────────────────────────────────────────────────────────

TOUR = {
    "name": "Bhutan Cultural Journey",
    "summary": "Seven days across Paro and Thimphu.",
    "itineraries": "Day 1\nArrive in Paro. Visit Rinpung Dzong.",
    "duration": "7 days",
    "highlights": ["Taktsang Monastery"],
}
GEN = {
    "subtitle": "Seven days of dzongs and monasteries",
    "summary": "A week across Paro and Thimphu. The grounds cover a wide valley floor.",
    "highlights": ["Rinpung Dzong at dawn in the Paro valley"],
    "itineraries": "Day 1 — Arrival in Paro\nArrive in Paro and visit the fortress on the hill.",
}


@pytest.mark.asyncio
async def test_record_approved_outcome_stamps_each_unit_with_the_run_subject_key():
    from services.content_generation import grounding as gr

    units = gr.sentence_units(GEN)
    assert units                                                  # the fixture produces real units
    # AA-756: only the units the run would ask (not skipped) are stamped, each keyed by the SAME
    # per-unit source (unit_source) the run used — not the whole-tour source.
    asked = [u for u in units if not gr.should_skip(u["sentence"])]
    assert asked and len(asked) < len(units)                      # the fixture has both skipped+asked

    seen = []

    async def fake_record_outcome(conn, qkey, subject_key, *, truth, source, ref=None):
        seen.append((qkey, subject_key, truth, source))
        return 1

    import shared.llm_client.decide as dd
    orig = dd.record_outcome
    dd.record_outcome = fake_record_outcome
    try:
        n = await gr.record_approved_outcome(MagicMock(), TOUR, GEN)
    finally:
        dd.record_outcome = orig

    assert n == len(asked)
    # Every stamped key is subject_key(unit_source(tour, unit), sentence) for a non-skipped unit.
    expected = {gr.subject_key(gr.unit_source(TOUR, u), u["sentence"]) for u in asked}
    assert {s[1] for s in seen} == expected
    assert all(s[0] == "a1_claim_supported" and s[2] is True and s[3] == "review_approve" for s in seen)


@pytest.mark.asyncio
async def test_record_approved_outcome_noop_on_empty_input():
    from services.content_generation import grounding as gr
    assert await gr.record_approved_outcome(MagicMock(), {}, {}) == 0


# ── atom_subject_key — the single a3_atom_in_text key builder ─────────────────────────────────────

def test_atom_subject_key_matches_the_a3_run_format():
    from services.acp_produce.tenant_pipeline import atom_subject_key
    from services.acp_shared.atom_extraction import derive_atom_text

    day = {"title": "Day 2 — Tiger's Nest", "body": "Hike to Taktsang Monastery through pine forest."}
    day_text = f"{day['title']}\n{day['body']}".strip()
    place, action = "Taktsang Monastery", "hike"

    key = atom_subject_key(day_text, place, action)

    text_key = hashlib.md5(day_text.encode("utf-8")).hexdigest()[:10]
    label = derive_atom_text(place, action)
    assert key == f"atom:{text_key}:{label[:200]}"


def test_atom_subject_key_strips_day_text_before_hashing():
    from services.acp_produce.tenant_pipeline import atom_subject_key
    a = atom_subject_key("  Day 1\nBody text  ", "P", "a")
    b = atom_subject_key("Day 1\nBody text", "P", "a")
    assert a == b
