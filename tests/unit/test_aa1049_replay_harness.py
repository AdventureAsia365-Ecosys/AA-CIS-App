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
    """Every AA-1049 switch at its default (= main)."""
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)
    monkeypatch.delenv(service.SEO_BUILDER_ENV, raising=False)


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


W_TITLE, W_META = "Dunmere Walk: Sea Wall", "One. Two."
# main's acceptance tests in these rows: the writer's title and meta fail; "Dunmere Walk" and "One." pass
_PASS = {"Dunmere Walk", "One."}


def _seo_row(calls, title=W_TITLE, meta=W_META, slug="s", request=None):
    """A replay seo row: the delivered fields, the writer's own, the verdict of main's acceptance tests on both and,
    with a fit call, its request (by default: the fields delivered)."""
    req = request if request is not None else {"seo_title": title, "meta_description": meta, "keyword": "k",
                                               "body_sha": "b"}
    return {"fit_calls": calls, "miss": calls > 0, "seo_title": title, "meta_description": meta, "slug": slug,
            "writer": {"seo_title": W_TITLE, "meta_description": W_META, "slug": "s"},
            "fit_request": req if calls else None,
            "verdict": {"writer": {"seo_title": W_TITLE in _PASS, "meta_description": W_META in _PASS},
                        "out": {"seo_title": title in _PASS, "meta_description": meta in _PASS}}}


def _passing_writer(row):
    """The same row where main's acceptance tests already pass the writer's values."""
    return {**row, "verdict": {"writer": {"seo_title": True, "meta_description": True},
                               "out": row["verdict"]["out"]}}


@pytest.mark.parametrize("base, cur, want", [
    (_seo_row(1), _seo_row(0, title="Dunmere Walk", meta="One."), "D-seo-1"),    # both fixed by a prefix, no call
    (_seo_row(1), _seo_row(0, meta="One."), "UNDECLARED"),         # no call, but the title kept still fails
    (_seo_row(1), _seo_row(0, title="Dunmere Walk", meta="Three."), "UNDECLARED"),  # not a prefix of the writer's
    (_seo_row(1), _seo_row(0, title="Dunmere Walk", meta="One.", slug="t"), "UNDECLARED"),  # the slug moved
    (_seo_row(1), _seo_row(0), "UNDECLARED"),   # review S272 M1: 0 calls and NO field changed (gate skipped)
    (_seo_row(0), _seo_row(1), "UNDECLARED"),                                    # a new fit call
    (_seo_row(1), _seo_row(1), "MISS"),          # both missed: same request, same soft-fail fields
    (_seo_row(0), _seo_row(0, title="z"), "UNDECLARED"),
    # review S272 M2: both missed, the soft-fail path ships a cut that is no fix ("Dunmere" fails, "Cheap." no prefix)
    (_seo_row(1), _seo_row(1, title="Dunmere", meta="Cheap."), "UNDECLARED"),
    # both missed: the meta fixed by a prefix ships on a fit outage, the fit is asked only about the title
    (_seo_row(1), _seo_row(1, meta="One."), "D-seo-1"),
    # ... but the fit request must be the values the code kept
    (_seo_row(1), _seo_row(1, meta="One.", request={"seo_title": W_TITLE, "meta_description": W_META,
                                                    "keyword": "k", "body_sha": "b"}), "UNDECLARED"),
    # both missed, same soft-fail fields, a different request (forbidden words main never sent)
    (_seo_row(1), _seo_row(1, request={"seo_title": W_TITLE, "meta_description": W_META, "keyword": "k",
                                       "body_sha": "b", "forbidden_words": ["cheap"]}), "UNDECLARED"),
    # a predicate change: the writer's values are judged differently from main
    (_seo_row(1), _passing_writer(_seo_row(1)), "UNDECLARED"),
])
def test_seo_diffs_are_classified_by_shape(base, cur, want):
    assert rp._classify("seo", base, cur)[0] == want


def _keys(new, prev_gates, cur_gates, prev_n, cur_n):
    return {"new": new, "removed": [], "prev_keys": prev_n, "cur_keys": cur_n, "prev_gates": prev_gates,
            "cur_gates": cur_gates}


def _main(revert, prev_gates, cur_gates, hold=(None, None)):
    """A base (main) guard pair: its decision, its gate-name view and its hold gate of both attempts."""
    return {"revert": revert, "keys": None, "gates": {"prev": sorted(prev_gates), "cur": sorted(cur_gates)},
            "hold": {"prev": hold[0], "cur": hold[1]}}


F9, F17, F13, F1 = "F9_cta_fact", "F17_trip_fact", "F13_repeat", "F1_grounding"


@pytest.mark.parametrize("base, cur, want", [
    # a same-gate swap the branch reverts
    (_main(False, [F9], [F9]), {"revert": True, "keys": _keys([[F9, "b"]], [F9], [F9], 1, 1)}, "D-guard-1"),
    # a revert whose new key is in a gate the previous attempt did not fail: not a swap
    (_main(False, [F9], [F9, F17]), {"revert": True, "keys": _keys([[F17, "b"]], [F9], [F9, F17], 1, 2)},
     "UNDECLARED"),
    # a revert after the gate set shrank (F1 + F9 -> F13_repeat): never declared
    (_main(False, [F1, F9], [F13]), {"revert": True, "keys": _keys([[F13, "a"]], [F1, F9], [F13], 2, 3)},
     "UNDECLARED"),
    # review S272 M3: same gate, 3 findings -> 1 new one (clears 3, adds 1): a revert is never declared
    (_main(False, [F13], [F13]), {"revert": True, "keys": _keys([[F13, "d"]], [F13], [F13], 3, 1)}, "UNDECLARED"),
    # a keep with fewer keys
    (_main(True, [F9], [F17]), {"revert": False, "keys": _keys([[F17, "d"]], [F9], [F17], 3, 1)}, "D-guard-2"),
    # a keep without fewer keys
    (_main(True, [F9], [F17]), {"revert": False, "keys": _keys([[F17, "d"]], [F9], [F17], 1, 1)}, "UNDECLARED"),
    # review S272 M4: the branch's key evidence lost a gate main sees (F13 dropped by a key-extraction bug)
    (_main(True, ["F12_scope", "F2_superlatives"], ["F2_superlatives", F13]),
     {"revert": False, "keys": _keys([], ["F12_scope", "F2_superlatives"], ["F2_superlatives"], 2, 1)},
     "UNDECLARED"),
    # a base row without main's gate view
    ({"revert": False}, {"revert": True, "keys": _keys([[F9, "b"]], [F9], [F9], 1, 1)}, "UNDECLARED"),
    # a flip with no stable-key evidence (gate_names mode)
    (_main(False, [F9], [F9]), {"revert": True, "keys": None}, "UNDECLARED"),
])
def test_guard_flips_are_classified_by_shape(base, cur, want):
    assert rp._classify("guard", base, cur)[0] == want
    assert rp._classify("route", {"a": 1}, {"a": 2})[0] == "UNDECLARED"


@pytest.mark.parametrize("hold, base_revert, want", [
    ((F9, None), True, "held->approved"),     # main reverts to the held attempt, the branch keeps the clean rewrite
    ((None, F1), True, "approved->held"),     # main reverts to a style-only attempt, the branch keeps a new F1 hold
    ((None, F1), False, "held->approved"),    # main keeps a rewrite that holds, the branch reverts to a clean one
    ((F9, F1), True, None),                   # held either way
])
def test_a_guard_flip_reports_the_status_each_side_continues_with(hold, base_revert, want):
    base = _main(base_revert, [F9], [F9], hold=hold)
    cur = {"revert": not base_revert, "keys": _keys([], [F9], [F9], 1, 1)}
    assert rp._status_flip(base, cur) == want


def test_mixed_guard_flips_are_one_diff_per_pair(tmp_path):
    swap = {"revert": True, **{k: v for k, v in _main(False, [F9], [F9]).items() if k in ("gates", "hold")},
            "keys": _keys([[F9, "b"]], [F9], [F9], 1, 1)}
    keep_bad = {"revert": False, **{k: v for k, v in _main(True, [F9], [F17]).items() if k in ("gates", "hold")},
                "keys": _keys([[F17, "d"]], [F9], [F17], 1, 1)}
    row = {"input": {}, "route": {}, "seo": None, "contract": {}}
    base = {"rows": {"p": {**row, "guard": [_main(False, [F9], [F9]), _main(True, [F9], [F17])]}}}
    cur = {"flags": {"T9_REGRESSION_GUARD_MODE": "stable_keys", "T9_SEO_BUILDER": "off"},
           "rows": {"p": {**row, "guard": [swap, keep_bad]}}}
    got = [(x["section"], x["declared"]) for x in rp.diff(base, cur, opt_in=True)["diffs"]]
    assert got == [("guard[0]", "D-guard-1"), ("guard[1]", "UNDECLARED")]
    s = rp.summary({"rows": {"p": {**cur["rows"]["p"], "channel": "linkedin", "f9_hit": True, "seo_miss": False,
                                   "cassette_hit": True, "stored": {"sha": "n/a"}}}}, rp.diff(base, cur, opt_in=True))
    assert s["undeclared"] == 1 and s["declared"] == {"D-guard-1": 1}
    # without --opt-in, or with --opt-in but the switch off in this run, a declared shape is still UNDECLARED
    for d in (rp.diff(base, cur), rp.diff(base, {**cur, "flags": {}}, opt_in=True)):
        assert [x["declared"] for x in d["diffs"]] == ["UNDECLARED", "UNDECLARED"]
        assert "needs --opt-in" in d["diffs"][0]["note"]


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
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_STABLE_KEYS)  # opt-in
    rp.main(["--pieces", str(p), "--out", str(out)])
    pair = json.loads(out.read_text())["rows"]["p-1"]["guard"][0]
    assert pair["revert"] is True  # a same-gate swap reverts (D-guard-1)
    assert pair["keys"]["prev_gates"] == pair["keys"]["cur_gates"] == ["F9_cta_fact"]
    base = tmp_path / "base.json"
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV)  # the default: main's guard
    rp.main(["--pieces", str(p), "--out", str(base)])
    assert json.loads(base.read_text())["rows"]["p-1"]["guard"] == [
        {"revert": False, "keys": None, "gates": {"prev": [F9], "cur": [F9]}, "hold": {"prev": F9, "cur": F9}}]
    # the default run equals main: no diff at all
    assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out)]) == 0
    s = json.loads(out.read_text())["summary"]
    assert s["declared"] == {} and s["undeclared"] == 0 and s["flags"]["T9_REGRESSION_GUARD_MODE"] == "gate_names"
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_STABLE_KEYS)
    # an opt-in run without --opt-in is never labelled declared
    assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["summary"]["undeclared"] == 1
    assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out), "--opt-in"]) == 0
    s = json.loads(out.read_text())["summary"]
    assert s["declared"] == {"D-guard-1": 1} and s["opt_in"] == ["guard"]


# ----------------------------------------------------------------------------- review S272: mutations end to end
# Each mutation makes the branch more permissive than main (or wrongly declares a diff); the replay of the mutated
# code against the unmutated base must exit 1 with an UNDECLARED diff.

TENANT = "00000000-0000-0000-0000-00000000a1b2"
TITLE_OK = "Dunmere Harbour Walk Guide: The Sea Wall and the Net Sheds"
META_OK = ("The Dunmere harbour walk follows the stone sea wall from the net sheds to the old signal station, "
           "a flat path at sea level along the calm water.")


def _blog(sid, title, meta):
    s = _sample(sid, "blog", BLOG, [F9_PASS], seo_title=title, meta_description=meta, slug="dunmere-harbour")
    s["input"]["context"]["tenant_id"] = TENANT
    return s


def _base_then(tmp_path, fx, mutation=None, monkeypatch=None, seo_on=False):
    """(exit code, replay doc) of a run over `fx` against a base run of the unmutated code at the defaults (main);
    `seo_on`: the branch run opts in to T9_SEO_BUILDER=on with --opt-in."""
    from contextlib import nullcontext
    base, out = tmp_path / "base.json", tmp_path / "cur.json"
    assert rp.main(["--golden", fx, "--out", str(base)]) == 0
    extra = []
    if seo_on:
        monkeypatch.setenv(service.SEO_BUILDER_ENV, service.SEO_BUILDER_ON)
        extra = ["--opt-in"]
    with mutation or nullcontext():
        code = rp.main(["--golden", fx, "--base", str(base), "--out", str(out), *extra])
    if seo_on:
        monkeypatch.delenv(service.SEO_BUILDER_ENV)
    return code, json.loads(out.read_text())


def test_m1_a_skipped_seo_gate_is_undeclared(tmp_path, monkeypatch):
    """M1: _fit_seo_meta returns after build_seo without checking the fields: an out-of-band title ships unfitted."""
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_blog("t9-m1", "Dunmere", "Too short for any band.")])
    assert _base_then(tmp_path, fx)[0] == 0
    assert _base_then(tmp_path, fx, monkeypatch=monkeypatch, seo_on=True)[0] == 0
    code, doc = _base_then(tmp_path, fx, patch.object(service.seo_builder, "fields_ok", return_value=True),
                           monkeypatch=monkeypatch, seo_on=True)
    assert code == 1 and [x["declared"] for x in doc["diff"]["diffs"]] == ["UNDECLARED"]
    assert doc["rows"]["t9-m1"]["seo"]["verdict"]["out"] == {"seo_title": False, "meta_description": False}


def test_m2_a_soft_fail_cut_on_a_fit_outage_is_undeclared(tmp_path, monkeypatch):
    """M2: the fields sent to the fit (and shipped when it fails) are cut to junk: both sides miss, still compared."""
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_blog("t9-m2", "Dunmere", "Too short for any band.")])

    def junk(seo, **_kw):
        return {**seo, "seo_title": (seo.get("seo_title") or "")[:4], "meta_description": "Cheap."}, []
    code, doc = _base_then(tmp_path, fx, patch.object(service.seo_builder, "build_seo", side_effect=junk),
                           monkeypatch=monkeypatch, seo_on=True)
    assert code == 1 and [x["declared"] for x in doc["diff"]["diffs"]] == ["UNDECLARED"]
    assert doc["rows"]["t9-m2"]["seo"]["miss"]


def test_m6_a_loosened_taken_title_check_is_undeclared(tmp_path, monkeypatch):
    """M6: the replay passes production's taken titles (the same tenant's other blog pieces), so a branch whose
    title_ok forgets them ships a title this brand already used, and the independent verdict sees it."""
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_blog("t9-m6a", TITLE_OK, META_OK), _blog("t9-m6b", TITLE_OK, META_OK)])
    code, doc = _base_then(tmp_path, fx)
    assert code == 0 and doc["rows"]["t9-m6a"]["seo"]["taken_titles"] == 1
    assert doc["rows"]["t9-m6a"]["seo"]["fit_calls"] == 1  # main: a taken title goes to the fit
    code, doc = _base_then(tmp_path, fx, patch.object(service, "title_taken", return_value=False))
    assert code == 1 and {x["declared"] for x in doc["diff"]["diffs"]} == {"UNDECLARED"}


def test_seo_replay_builds_the_production_hygiene_context(monkeypatch):
    seen = {}
    real = service._fit_seo_meta

    async def spy(seo, kw, **k):
        seen.update(k)
        return await real(seo, kw, **k)
    ctx = {**CONTEXT, "tenant_id": TENANT, "scope": None, "trip_places": ["Dunmere"]}
    piece = {"id": "x", "channel": "blog", "context": ctx, "content_text": BLOG, "seo_title": TITLE_OK,
             "meta_description": META_OK, "slug": "s"}
    with patch.object(service, "_fit_seo_meta", side_effect=spy):
        row = rp._seo(piece, ["Another Dunmere Title For The Same Brand Here"])
    assert seen["hyg"] is not None and seen["hyg"].lexicon is not None
    assert seen["taken_titles"] == ["Another Dunmere Title For The Same Brand Here"]
    assert row["context_error"] is None and row["verdict"]["writer"] == {"seo_title": True, "meta_description": True}


def _ledger(*entries):
    return [{"gate": g, "passed": False, "blocking": True, "violations": [f"repeats an earlier line: '{q}'"
                                                                          for q in qs]} for g, qs in entries]


def _guard_run(tmp_path, monkeypatch, prev, cur, mutation):
    rows = [{"piece_id": "p-g", "channel": "linkedin", "content_text": POST, "status": "approved",
             "gate_ledger": cur, "repair_log": [{"attempt": 1, "gate_ledger": prev}],
             "writer_input": {"context": {**CONTEXT, "channel": "linkedin"}}}]
    p, base, out = tmp_path / "pieces.json", tmp_path / "base.json", tmp_path / "cur.json"
    p.write_text(json.dumps(rows))
    monkeypatch.delenv(service.REGRESSION_GUARD_ENV, raising=False)  # the default: main's guard
    rp.main(["--pieces", str(p), "--out", str(base)])
    assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out)]) == 0  # default = main
    with mutation:  # the default path never reads stable_keys: a stable-key mutation changes nothing
        assert rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out)]) == 0
    monkeypatch.setenv(service.REGRESSION_GUARD_ENV, service.GUARD_STABLE_KEYS)  # opt-in
    clean = rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out), "--opt-in"])
    with mutation:
        code = rp.main(["--pieces", str(p), "--base", str(base), "--out", str(out), "--opt-in"])
    return clean, code, json.loads(out.read_text())


SX, SY = "The quay bell rings at noon.", "The net sheds are painted blue."


def test_m3_reverting_a_same_gate_rewrite_that_cleared_more_findings_is_undeclared(tmp_path, monkeypatch):
    """M3: stable_keys.compare without `len(cur) >= len(prev)`: F13_repeat x3 -> x1 (new sentence) is reverted."""
    from services.acp_content_writing import stable_keys
    real = stable_keys.compare

    def no_count(prev, cur):
        c = real(prev, cur)
        return {**c, "revert": bool(c["new"]) and len(c["cur_gates"]) >= len(c["prev_gates"])}
    prev = _ledger(("F13_repeat", ["Line A here.", "Line B here.", "Line C here."]))
    cur = _ledger(("F13_repeat", ["Line D here."]))
    clean, code, doc = _guard_run(tmp_path, monkeypatch, prev, cur, patch.object(stable_keys, "compare", no_count))
    assert clean == 0 and code == 1
    assert [x["declared"] for x in doc["diff"]["diffs"]] == ["UNDECLARED"]


def test_m4_key_evidence_that_lost_a_gate_cannot_certify_itself(tmp_path, monkeypatch):
    """M4: blocking_keys skips F13_repeat, so a same-count rewrite with a new gate looks like 'fewer keys'."""
    from services.acp_content_writing import stable_keys
    real = stable_keys.blocking_keys

    def skip_f13(ledger):
        return frozenset(k for k in real(ledger) if k[0] != "F13_repeat")
    prev = _ledger(("F12_scope", [SX]), ("F2_superlatives", ["The finest harbour anywhere."]))
    cur = _ledger(("F2_superlatives", ["The finest harbour anywhere."]), ("F13_repeat", [SY]))
    clean, code, doc = _guard_run(tmp_path, monkeypatch, prev, cur,
                                  patch.object(stable_keys, "blocking_keys", skip_f13))
    assert clean == 0 and code == 1
    d = doc["diff"]["diffs"]
    assert [x["declared"] for x in d] == ["UNDECLARED"] and "main's gate view" in d[0]["note"]


def test_a_default_run_equals_main_and_the_seo_prefix_is_opt_in_only(tmp_path, monkeypatch):
    """S272 decision: with the defaults the SEO section equals main (the fit call is made: a cassette miss on both
    sides); the code-first prefix shows only with T9_SEO_BUILDER=on, and is labelled D-seo-1 only with --opt-in."""
    _env(monkeypatch)
    long_meta = META_OK + " Then follow the slip down to the net sheds for tea with the harbour crews at dusk."
    fx = _fixture(tmp_path, [_blog("t9-p1", TITLE_OK, long_meta)])
    code, doc = _base_then(tmp_path, fx)
    assert code == 0 and doc["summary"]["declared"] == {} and doc["summary"]["undeclared"] == 0
    assert doc["rows"]["t9-p1"]["seo"]["fit_calls"] == 1                       # main's path
    code, doc = _base_then(tmp_path, fx, monkeypatch=monkeypatch, seo_on=True)
    assert code == 0 and doc["summary"]["declared"] == {"D-seo-1": 1} and doc["summary"]["opt_in"] == ["seo"]
    assert doc["rows"]["t9-p1"]["seo"]["meta_description"] == META_OK and doc["rows"]["t9-p1"]["seo"]["fit_calls"] == 0
    # the same opt-in run without --opt-in: UNDECLARED, exit 1
    base, out = tmp_path / "base.json", tmp_path / "cur.json"
    monkeypatch.setenv(service.SEO_BUILDER_ENV, service.SEO_BUILDER_ON)
    assert rp.main(["--golden", fx, "--base", str(base), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["summary"]["undeclared"] == 1


def test_live_piece_rows_use_the_flat_writer_input_as_context(tmp_path):
    """S273: stored pieces keep the T9 context flat in writer_input (trip, facts, cta, ...); there is no nested
    "context" key. The loader read only writer_input["context"], so every live row replayed with an empty context
    (the S273 export found 0 pieces with a "context" key among 169 recent T9 pieces)."""
    flat = {"channel": "blog", "trip": {"name": "Invented River Loop"}, "cta": "Ask us", "facts": []}
    path = tmp_path / "pieces.json"
    path.write_text(json.dumps([{"piece_id": "p-1", "channel": "blog", "writer_input": flat, "content_text": "x"}]))
    [p] = rp.load_pieces(str(path))
    assert p["context"]["trip"] == {"name": "Invented River Loop"}
    assert p["context"]["cta"] == "Ask us"


def test_a_nested_context_still_wins(tmp_path):
    nested = {"context": {"cta": "Nested"}, "cta": "Flat"}
    path = tmp_path / "pieces.json"
    path.write_text(json.dumps([{"piece_id": "p-2", "channel": "blog", "writer_input": nested}]))
    [p] = rp.load_pieces(str(path))
    assert p["context"] == {"cta": "Nested"}


# ----------------------------------------------------------------------------- S273: a meta under the band floor
# D-seo-4: the one fit call states the policy band (stored bodies: the only S273 diff a replay of real data can show).
# D-seo-3: a meta completed by one cited post sentence (needs a body that still carries its citation tags).

SHORT_META = "The Dunmere harbour walk follows the stone sea wall from the net sheds to the old signal station."  # 97
FLAT = "The path stays flat at sea level along the calm water."                                              # 54
TAGGED_BLOG = (f"Dunmere harbour wakes slowly. [R:atom_dun1]\n\n{FLAT} [R:atom_dun1]\n\n"
               "Some say it is the best walk on the coast.\n\n" + BLOG.split("\n\n", 1)[1])


def _tagged(sid, meta, body=TAGGED_BLOG, atom="atom_dun1"):
    s = _blog(sid, TITLE_OK, meta)
    s["output"]["content_text"] = body
    s["input"]["context"]["atom_id"] = atom
    return s


def test_s273_a_band_stated_fit_request_is_d_seo_4_only_with_opt_in(tmp_path, monkeypatch):
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_blog("t9-b4", TITLE_OK, SHORT_META)])
    code, doc = _base_then(tmp_path, fx, monkeypatch=monkeypatch, seo_on=True)
    assert code == 0 and [x["declared"] for x in doc["diff"]["diffs"]] == ["D-seo-4"]
    req = doc["rows"]["t9-b4"]["seo"]["fit_request"]
    assert list(req["meta_band"]) == list(rp_band()) and doc["rows"]["t9-b4"]["seo"]["fit_calls"] == 1
    # the same run without --opt-in is never declared
    base, out = tmp_path / "base.json", tmp_path / "cur.json"
    monkeypatch.setenv(service.SEO_BUILDER_ENV, service.SEO_BUILDER_ON)
    assert rp.main(["--golden", fx, "--base", str(base), "--out", str(out)]) == 1


def rp_band():
    from services.quality import writing_policy
    return writing_policy.band("meta")


def test_s273_a_band_that_is_not_the_policy_band_is_undeclared(tmp_path, monkeypatch):
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_blog("t9-b5", TITLE_OK, SHORT_META)])
    # mutation: the call site states a band that is not the policy's (and judges with it)
    code, doc = _base_then(tmp_path, fx, patch.object(service, "_META_MIN_CHARS", 160), monkeypatch=monkeypatch,
                           seo_on=True)
    assert code == 1 and "UNDECLARED" in [x["declared"] for x in doc["diff"]["diffs"]]


def test_s273_a_meta_completed_by_a_cited_sentence_is_d_seo_3(tmp_path, monkeypatch):
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_tagged("t9-c3", SHORT_META)])
    code, doc = _base_then(tmp_path, fx)
    assert code == 0 and doc["rows"]["t9-c3"]["seo"]["fit_calls"] == 1          # main: the fit is asked
    code, doc = _base_then(tmp_path, fx, monkeypatch=monkeypatch, seo_on=True)
    row = doc["rows"]["t9-c3"]["seo"]
    assert code == 0 and [x["declared"] for x in doc["diff"]["diffs"]] == ["D-seo-3"]
    assert row["fit_calls"] == 0 and row["meta_description"] == f"{SHORT_META} {FLAT}" and row["completion"]


def test_s273_a_completion_by_an_uncited_sentence_is_undeclared(tmp_path, monkeypatch):
    """Mutation: the builder accepts any post sentence (here the untagged one): the independent anchor check fails."""
    _env(monkeypatch)
    uncited = "The sea wall is the best walk on the coast for families."                 # 56 -> 154
    body = TAGGED_BLOG.replace("Some say it is the best walk on the coast.", uncited)
    fx = _fixture(tmp_path, [_tagged("t9-c4", SHORT_META, body=body.replace(f"{FLAT} [R:atom_dun1]\n\n", ""))])
    code, doc = _base_then(tmp_path, fx,
                           patch.object(service.seo_builder, "cited_sentences", return_value=[uncited]),
                           monkeypatch=monkeypatch, seo_on=True)
    assert code == 1 and [x["declared"] for x in doc["diff"]["diffs"]] == ["UNDECLARED"]
    assert doc["rows"]["t9-c4"]["seo"]["meta_description"] == f"{SHORT_META} {uncited}"


def test_s273_a_tag_of_another_piece_is_no_anchor(tmp_path, monkeypatch):
    _env(monkeypatch)
    fx = _fixture(tmp_path, [_tagged("t9-c5", SHORT_META, atom="atom_other")])
    code, doc = _base_then(tmp_path, fx, monkeypatch=monkeypatch, seo_on=True)
    assert code == 0 and [x["declared"] for x in doc["diff"]["diffs"]] == ["D-seo-4"]   # no completion: the fit
