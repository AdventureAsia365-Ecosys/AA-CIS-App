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
    # in band: no SEO-fit call, so a full cassette hit
    meta = ("The Dunmere harbour walk follows the stone sea wall from the net sheds to the old signal station, "
            "a flat path at sea level along the calm water.")
    fx = _fixture(tmp_path, [
        _sample("t9-001", "blog", BLOG, [F9_PASS], seo_title=title, meta_description=meta, slug="dunmere-harbour"),
        _sample("t9-002", "linkedin", POST, []),  # no F9 verdict stored: a cassette miss
    ])
    out = tmp_path / "cur.json"
    assert rp.main(["--golden", fx, "--out", str(out)]) == 0
    s = json.loads(out.read_text())["summary"]
    assert s["pieces"] == 2 and s["cassette_hits"] == 1 and s["cassette_misses_f9_needs_rejudge"] == 1
    assert s["cassette_misses_seo_fit"] == 0 and s["seo_fit_calls"] == 0
    # golden samples carry no stored contract: the sha check is n/a, never a pass
    assert s["stored_sha_na"] == 2 and s["stored_sha_match"] == 0 and s["post_gate_edit"] == 0
    assert s["input_errors"] == 0


def test_an_seo_fit_call_is_a_cassette_miss_not_a_hit(tmp_path, monkeypatch):
    _env(monkeypatch)
    meta = "Too short for any band."  # out of band and no prefix fixes it: the replay would need the fit call
    fx = _fixture(tmp_path, [_sample("t9-004", "blog", BLOG, [F9_PASS], seo_title="Dunmere", meta_description=meta,
                                     slug="dunmere")])
    out = tmp_path / "cur.json"
    rp.main(["--golden", fx, "--out", str(out)])
    doc = json.loads(out.read_text())
    row, s = doc["rows"]["t9-004"], doc["summary"]
    assert row["f9_hit"] and row["seo_miss"] and not row["cassette_hit"]
    assert s["cassette_hits"] == 0 and s["cassette_misses_seo_fit"] == 1 and s["seo_fit_calls"] == 1
    base = tmp_path / "base.json"
    base.write_text(out.read_text())
    assert rp.main(["--golden", fx, "--base", str(base), "--out", str(out)]) == 0
    d = json.loads(out.read_text())
    assert [x["declared"] for x in d["diff"]["diffs"]] == ["MISS"]  # both sides missed: not compared, not a match
    assert d["summary"]["undeclared"] == 0 and d["summary"]["not_compared"] == {"MISS": 1}


def _piece_row(text, contract):
    return {"piece_id": "p-9", "channel": "linkedin", "content_text": text, "status": "approved",
            "gate_ledger": [F9_PASS], "repair_log": [],
            "writer_input": {"context": {**CONTEXT, "channel": "linkedin"}, "final_contract": contract}}


@pytest.mark.parametrize("stored, now, want, edited, code", [
    (POST, POST, "match", False, 0),
    (POST + " ", POST + " ", "post_gate_edit", False, 1),   # _finalize_piece stored a text the gates never saw
    (POST, POST + " (edited)", "match", True, 0),           # a later tenant edit: reported, not a post-gate edit
])
def test_stored_contract_of_a_piece_export_is_checked(tmp_path, monkeypatch, stored, now, want, edited, code):
    _env(monkeypatch)
    rec = fc.stamp_stored({"workflow": "t9", "exit": "done", "verdict": "pass", "text_sha": fc.text_sha(POST),
                           "findings": []}, fc.text_sha(stored))
    p = tmp_path / "pieces.json"
    p.write_text(json.dumps([_piece_row(now, rec)]))
    out = tmp_path / "cur.json"
    assert rp.main(["--pieces", str(p), "--out", str(out)]) == code
    row = json.loads(out.read_text())["rows"]["p-9"]["stored"]
    assert row == {"sha": want, "edited_since_store": edited}


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


def _seo_row(calls, title="Dunmere Walk: Sea Wall", meta="One. Two.", slug="s", writer=None):
    w = writer or {"seo_title": "Dunmere Walk: Sea Wall", "meta_description": "One. Two.", "slug": "s"}
    return {"fit_calls": calls, "miss": calls > 0, "seo_title": title, "meta_description": meta, "slug": slug,
            "writer": w}


@pytest.mark.parametrize("base, cur, want", [
    (_seo_row(1), _seo_row(0, meta="One."), "D-seo-1"),                        # whole-sentence prefix, no call
    (_seo_row(1), _seo_row(0, title="Dunmere Walk"), "D-seo-1"),               # separator prefix, no call
    (_seo_row(1), _seo_row(0, meta="Three."), "UNDECLARED"),                   # not a prefix of the writer's meta
    (_seo_row(1), _seo_row(0, meta="One.", slug="t"), "UNDECLARED"),           # the slug moved
    (_seo_row(0), _seo_row(1), "UNDECLARED"),                                  # a new fit call
    (_seo_row(1), _seo_row(1, meta="One."), "MISS"),                           # both sides missed
    (_seo_row(0), _seo_row(0, title="z"), "UNDECLARED"),
])
def test_seo_diffs_are_classified_by_shape(base, cur, want):
    assert rp._classify("seo", base, cur)[0] == want


def _keys(new, prev_gates, cur_gates, prev_n, cur_n):
    return {"new": new, "removed": [], "prev_keys": prev_n, "cur_keys": cur_n, "prev_gates": prev_gates,
            "cur_gates": cur_gates}


@pytest.mark.parametrize("base, cur, want", [
    # a same-gate swap the branch reverts
    ({"revert": False}, {"revert": True, "keys": _keys([["F9_cta_fact", "b"]], ["F9_cta_fact"], ["F9_cta_fact"],
                                                         1, 1)}, "D-guard-1"),
    # a revert whose new key is in a gate the previous attempt did not fail: not a swap
    ({"revert": False}, {"revert": True, "keys": _keys([["F17_trip_fact", "b"]], ["F9_cta_fact"],
                                                         ["F9_cta_fact", "F17_trip_fact"], 1, 2)}, "UNDECLARED"),
    # a revert after the gate set shrank (F1 + F9 -> F13_repeat): never declared
    ({"revert": False}, {"revert": True, "keys": _keys([["F13_repeat", "a"]], ["F1_grounding", "F9_cta_fact"],
                                                         ["F13_repeat"], 2, 3)}, "UNDECLARED"),
    # a keep with fewer keys
    ({"revert": True}, {"revert": False, "keys": _keys([["F17_trip_fact", "d"]], ["F9_cta_fact"],
                                                         ["F17_trip_fact"], 3, 1)}, "D-guard-2"),
    # a keep without fewer keys
    ({"revert": True}, {"revert": False, "keys": _keys([["F17_trip_fact", "d"]], ["F9_cta_fact"],
                                                         ["F17_trip_fact"], 1, 1)}, "UNDECLARED"),
    # a flip with no stable-key evidence (gate_names mode)
    ({"revert": False}, {"revert": True, "keys": None}, "UNDECLARED"),
])
def test_guard_flips_are_classified_by_shape(base, cur, want):
    assert rp._classify("guard", base, cur)[0] == want
    assert rp._classify("route", {"a": 1}, {"a": 2})[0] == "UNDECLARED"


def test_mixed_guard_flips_are_one_diff_per_pair(tmp_path):
    swap = {"revert": True, "keys": _keys([["F9_cta_fact", "b"]], ["F9_cta_fact"], ["F9_cta_fact"], 1, 1)}
    keep_bad = {"revert": False, "keys": _keys([["F17_trip_fact", "d"]], ["F9_cta_fact"], ["F17_trip_fact"], 1, 1)}
    row = {"input": {}, "route": {}, "seo": None, "contract": {}}
    base = {"rows": {"p": {**row, "guard": [{"revert": False, "keys": None}, {"revert": True, "keys": None}]}}}
    cur = {"rows": {"p": {**row, "guard": [swap, keep_bad]}}}
    got = [(x["section"], x["declared"]) for x in rp.diff(base, cur)["diffs"]]
    assert got == [("guard[0]", "D-guard-1"), ("guard[1]", "UNDECLARED")]
    s = rp.summary({"rows": {"p": {**cur["rows"]["p"], "channel": "linkedin", "f9_hit": True, "seo_miss": False,
                                   "cassette_hit": True, "stored": {"sha": "n/a"}}}}, rp.diff(base, cur))
    assert s["undeclared"] == 1 and s["declared"] == {"D-guard-1": 1}


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
    pair = json.loads(out.read_text())["rows"]["p-1"]["guard"][0]
    assert pair["revert"] is True  # a same-gate swap reverts (D-guard-1)
    assert pair["keys"]["prev_gates"] == pair["keys"]["cur_gates"] == ["F9_cta_fact"]
    base = tmp_path / "base.json"
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_GATE_NAMES)
    rp.main(["--pieces", str(p), "--out", str(base)])
    assert json.loads(base.read_text())["rows"]["p-1"]["guard"] == [{"revert": False, "keys": None}]
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV)
    assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["summary"]["declared"] == {"D-guard-1": 1}
