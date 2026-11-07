"""AA-1049 W3 (eval item R0b) — the PieceStore seam (production input builder + dry store) and the free offline replay
harness with its declared-diff report. Invented places, brands and texts only (CI golden-set text guard)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from scripts.quality import replay_aa1049_t9 as rp
from services.acp_content_writing import piece_store, service
from services.quality import final_contract as fc

CTA = "Ask Velmora Trails for the harbour walk dates."
BLOG = ("Dunmere harbour wakes slowly. The sea wall runs past the net sheds to the old signal station.\n\n"
        "## FAQ\n\n**Q: Is the wall walk steep?**\nA: No, it follows the harbour at sea level.\n"
        "**Q: Where does it start?**\nA: At the net sheds by the slip.\n"
        "**Q: Can children walk it?**\nA: The path is flat and fenced along the water.\n\n" + CTA)
POST = "The Dunmere sea wall at low tide: nets, gulls and the old signal station.\n\n" + CTA
CONTEXT = {"atom_text": "Dunmere harbour: a stone sea wall past net sheds to an old signal station.",
           "facts_text": None, "brand_audience": {"brand_name": "Velmora Trails"}, "trip_name": "Dunmere Coast",
           "destination": "Velmora", "cta": CTA, "keyword": "dunmere harbour walk", "channel": "blog"}
F9_PASS = {"gate": "F9_cta_fact", "passed": True, "blocking": True, "violations": []}


def _sample(sid, channel, text, ledger, **out):
    ctx = {**CONTEXT, "channel": channel}
    return {"sample_id": sid, "kind": "t9", "channel": channel, "input": {"context": ctx},
            "output": {"content_text": text, "gate_ledger": ledger, "flags": [], **out},
            "pipeline_verdict": {"status": "approved"}}


def _fixture(tmp_path, samples):
    p = tmp_path / "fixture.json"
    p.write_text(json.dumps({"samples": samples}), encoding="utf-8")
    return str(p)


def _env(monkeypatch):
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)


def test_production_input_is_the_service_builder():
    got = piece_store.production_input(dict(CONTEXT))
    assert got["grounding_text"] == service._grounding_text(dict(CONTEXT))
    assert got["fs_sha"] == service._grounding_sheet(dict(CONTEXT)).sha() and len(got["grounding_sha"]) == 16


def test_dry_store_stamps_stored_sha_against_the_gated_sha():
    rec = {"workflow": "t9", "exit": "done", "verdict": "pass", "text_sha": fc.text_sha(POST), "findings": []}
    store = piece_store.DryStore()
    row = {"piece_id": "p-1", "attempt_number": 1, "status": "approved", "held_reason": None, "gate_ledger": [],
           "repair_log": [], "flags": [], "final_contract": rec}
    with piece_store.use_store(store):  # service._finalize_piece itself hands the row over: pool None is never used
        same = asyncio.run(service._finalize_piece(None, content_text=POST, **row))
        edited = asyncio.run(piece_store.finalize(None, content_text=POST + " ", **row))
    assert same["final_contract"]["sha_match"] is True and edited["final_contract"]["sha_match"] is False
    assert len(store.rows) == 2 and store.rows[0]["stored_sha"] == fc.text_sha(POST)


def test_without_a_dry_store_the_seam_is_the_production_store():
    with patch.object(service, "_finalize_piece", new=AsyncMock(return_value={"ok": 1})) as real:
        assert asyncio.run(piece_store.finalize("pool", content_text="x", status="held")) == {"ok": 1}
    real.assert_awaited_once_with("pool", content_text="x", status="held")


def test_replay_summary_counts_cassette_misses_as_needs_rejudge(tmp_path, monkeypatch):
    _env(monkeypatch)
    title = "Dunmere Harbour Walk Guide: The Sea Wall and the Net Sheds"
    meta = ("Walk the Dunmere harbour wall from the net sheds to the old signal station, a flat stone path along the "
            "water for every age.")
    fx = _fixture(tmp_path, [
        _sample("t9-001", "blog", BLOG, [F9_PASS], seo_title=title, meta_description=meta, slug="dunmere-harbour"),
        _sample("t9-002", "linkedin", POST, []),  # no F9 verdict stored: a cassette miss
    ])
    out = tmp_path / "cur.json"
    assert rp.main(["--golden", fx, "--out", str(out)]) == 0
    s = json.loads(out.read_text())["summary"]
    assert s["pieces"] == 2 and s["cassette_hits"] == 1 and s["cassette_misses_needs_rejudge"] == 1
    assert s["sha_match"] == 2 and s["sha_mismatch"] == 0 and s["input_errors"] == 0


def test_replay_against_itself_has_no_diff_and_a_changed_route_is_undeclared(tmp_path, monkeypatch):
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_sample("t9-003", "linkedin", POST, [F9_PASS])])
    base = tmp_path / "base.json"
    rp.main(["--golden", fx, "--out", str(base)])
    out = tmp_path / "cur.json"
    assert rp.main(["--golden", fx, "--base", str(base), "--out", str(out),
                    "--report", str(tmp_path / "r.md")]) == 0
    assert json.loads(out.read_text())["summary"]["undeclared"] == 0
    doc = json.loads(base.read_text())
    doc["rows"]["t9-003"]["route"]["hold_failure"] = "F1_grounding"
    base.write_text(json.dumps(doc))
    assert rp.main(["--golden", fx, "--base", str(base), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["diff"]["diffs"][0]["declared"] == "UNDECLARED"


@pytest.mark.parametrize("base, cur, want", [
    ({"fit_calls": 1, "seo_title": "a", "meta_description": "b", "slug": "s"},
     {"fit_calls": 0, "seo_title": "a", "meta_description": "c", "slug": "s"}, "D-seo-1"),
    ({"fit_calls": 2, "seo_title": "a", "meta_description": "b", "slug": "s"},
     {"fit_calls": 1, "seo_title": "a", "meta_description": "b", "slug": "s"}, "D-seo-2"),
    ({"fit_calls": 1, "seo_title": "a", "meta_description": "b", "slug": "s"},
     {"fit_calls": 1, "seo_title": "z", "meta_description": "b", "slug": "s"}, "UNDECLARED"),
])
def test_seo_diffs_are_classified(base, cur, want):
    assert rp._classify("seo", base, cur)[0] == want


def test_guard_diffs_are_classified():
    assert rp._classify("guard", [False], [True])[0] == "D-guard-1"
    assert rp._classify("guard", [True], [False])[0] == "D-guard-2"
    assert rp._classify("route", {"a": 1}, {"a": 2})[0] == "UNDECLARED"


def test_guard_section_reads_attempt_pairs_of_a_piece_export(tmp_path, monkeypatch):
    _env(monkeypatch)
    a = "The ferry to Brecca leaves at dawn every morning of the trip."
    b = "Every guest rides the old harbour tram along the Dunmere sea wall."

    def f9(q):
        return {"gate": "F9_cta_fact", "passed": False, "blocking": True,
                "violations": [f'audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "{q}" '
                               f'(claims: x; source: not stated)']}
    rows = [{"piece_id": "p-1", "channel": "linkedin", "content_text": POST, "status": "held",
             "gate_ledger": [f9(b)], "repair_log": [{"attempt": 1, "gate_ledger": [f9(a)]}],
             "writer_input": {"context": {**CONTEXT, "channel": "linkedin"}}}]
    p = tmp_path / "pieces.json"
    p.write_text(json.dumps(rows))
    out = tmp_path / "cur.json"
    rp.main(["--pieces", str(p), "--out", str(out)])
    assert json.loads(out.read_text())["rows"]["p-1"]["guard"] == [True]  # a same-gate swap reverts (D-guard-1)
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_GATE_NAMES)
    rp.main(["--pieces", str(p), "--out", str(out)])
    assert json.loads(out.read_text())["rows"]["p-1"]["guard"] == [False]
