"""AA-1048 (Quality W2) — the R1 calibration harness (services/ground_truth/r1.py) and its script: DRY RUN plan and
cost, the cap, pinned identity, votes, unusable replies, the ledger rows with billed_fields, extraction confirmation in
code, and the measurement record. Every model call is a fake; invented texts only."""
import json
import types

import pytest

from services.ground_truth import r1

SOURCE = "Day 1: Arrive in Velmora. Dinner included.\nDay 2: Walk 12 km to Ostrin Valley."
FIELD = "Arrive in Velmora for dinner. Walk 40 km to Ostrin Valley. Breakfast is served daily."


def _items(n_bad=2, n_ok=2, hard=False):
    out = []
    claims = [("Walk 40 km to Ostrin Valley.", "fact.number", "violation"),
              ("Breakfast is served daily.", "fact.inclusion", "violation"),
              ("Arrive in Velmora for dinner.", "fact.inclusion", "ok"),
              ("Arrive in Velmora for dinner.", "fact.number", "ok")]
    for i, (text, cls, lab) in enumerate(claims[:n_bad] + claims[2:2 + n_ok]):
        out.append(r1.Item(f"{i:064x}", "t9_blog", cls, lab, "hard" if hard else "representative", "a" * 64,
                           "content_text", text, FIELD, SOURCE, ("Day 2: Walk 12 km to Ostrin Valley.",)))
    return out


def _resp(content, model="gpt-6-luna-openai", conn="openai-direct", cost=0.0004):
    return types.SimpleNamespace(content=content, model_used=model, connection_key=conn, satellite_account=None,
                                 cost_usd=cost, input_tokens=100, output_tokens=20, provider="openai",
                                 stop_reason="stop", fallback_used=False)


LUNA = r1.Instrument("gpt-6-luna-openai", "openai-direct", "entailment")
LUNA_X = r1.Instrument("gpt-6-luna-openai", "openai-direct", "extraction")


def test_instruments_and_plan():
    insts = r1.instruments()
    assert [i.id for i in insts] == ["gpt-6-luna-openai@openai-direct:entailment",
                                     "gpt-6-luna-openai@openai-direct:extraction",
                                     "llama4-maverick@acc2:entailment", "llama4-maverick@acc2:extraction"]
    plan = r1.plan(_items(), insts)
    rows = {r["instrument"]: r for r in plan["instruments"]}
    assert rows[LUNA.id]["calls"] == 4 * 3 and rows[LUNA_X.id]["calls"] == 1 * 3
    assert plan["fits_cap"] and plan["expected_total_usd"] < 0.05 and plan["max_tokens"] >= 8192
    assert plan["worst_case_total_usd"] > plan["expected_total_usd"]
    assert plan["labels"]["fact.number:violation"] == 1
    for bad in (dict(cap=2.5), dict(cap=0)):
        with pytest.raises(r1.R1Refused, match="cap"):
            r1.plan(_items(), insts, **bad)
    with pytest.raises(r1.R1Refused, match="no labelled"):
        r1.plan([], insts)
    with pytest.raises(r1.R1Refused, match="no price"):
        r1.plan(_items(), [r1.Instrument("mystery-model", "acc2", "entailment")])
    with pytest.raises(r1.R1Refused, match="mode"):
        r1.instruments(modes=["vibes"])


def test_items_from_keeps_violation_and_ok_only():
    export = [{"claim_hash": "h1", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Walk 40 km.", "field_text": FIELD, "source_text": SOURCE},
              {"claim_hash": "h2", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Unsure one.", "source_text": SOURCE},
              {"claim_hash": "h3", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Not labelled.", "source_text": SOURCE}]
    finals = {"h1": {"claim_class": "fact.number", "label": "violation"},
              "h2": {"claim_class": "fact.number", "label": "unsure"}}
    items = r1.items_from(export, finals)
    assert [i.claim_hash for i in items] == ["h1"] and items[0].evidence


def _run(items, insts, replies, cap=2.0, votes=3):
    seq = iter(replies)
    logged = []
    result = r1.run(items, insts, cap=cap, votes=votes, run_id="zz", caller=lambda *a: next(seq),
                    log=lambda resp, sig, parsed, err: logged.append((sig["instrument"], parsed is not None, err)))
    return result, logged


def test_entailment_votes_identity_and_unusable_replies():
    items = _items(n_bad=1, n_ok=1)
    sup = json.dumps({"verdict": "supported", "quote": "", "why": ""})
    uns = json.dumps({"verdict": "unsupported", "quote": "", "why": ""})
    replies = [_resp(uns), _resp(sup),                                   # vote 1
               _resp(uns), _resp("not json"),                            # vote 2 (item 2 unusable)
               _resp(uns, model="llama4-maverick", conn="acc2"), _resp(sup)]   # vote 3 (item 1 wrong model)
    result, logged = _run(items, [LUNA], replies)
    votes = result["votes"][LUNA.id]
    assert votes[items[0].claim_hash] == [True, True, None]
    assert votes[items[1].claim_hash] == [False, None, False]
    assert result["identity_mismatches"] == {LUNA.id: 1} and result["calls"] == 6
    assert sum(1 for _i, ok, err in logged if err is not None) == 1
    rec = r1.measure(items, votes)
    assert rec["classes"]["fact.number"]["tp"] == 1 and rec["ok_cleared"] == 1.0
    assert rec["retest"] is None and rec["unverified"] == 0


def test_cap_stops_before_the_call_that_would_pass_it():
    items = _items()
    uns = json.dumps({"verdict": "unsupported", "quote": "", "why": ""})
    expected = r1.call_cost(LUNA, r1.entailment_prompt(items[0]), r1.EXPECTED_OUT["entailment"])
    cap = expected * r1.SAFETY                                            # room for exactly one call
    result, _ = _run(items, [LUNA], [_resp(uns, cost=expected)] * 20, cap=cap)
    assert result["calls"] == 1 and result["stopped"]
    assert sum(v.count(None) for v in result["votes"][LUNA.id].values()) == 4 * 3 - 1


def test_extraction_confirms_quotes_in_code():
    items = _items()
    reply = json.dumps({"claims": [{"quote": "Walk 40 km to Ostrin Valley", "class": "fact.number", "why": "x"},
                                   {"quote": "A quote that is not in the text at all", "class": "fact.name",
                                    "why": "y"}]})
    flags = r1.flags_from_reply(LUNA_X, json.loads(reply), items)
    assert flags[items[0].claim_hash] is True
    assert not any(flags[i.claim_hash] for i in items[1:])
    result, _ = _run(items, [LUNA_X], [_resp(reply)] * 3)
    rep = r1.report(items, result, [LUNA_X])
    rec = rep["records"][LUNA_X.id]
    assert rec["retest"] == 1.0 and rec["classes"]["fact.number"]["precision"] == 1.0
    assert rec["classes"]["fact.inclusion"]["recall"] == 0.0
    assert rep["recommended"] is None                                     # nothing passes the bar on 4 items


def test_measure_bar_and_recommendation():
    items = []
    for i in range(60):
        lab = "violation" if i < 30 else "ok"
        items.append(r1.Item(f"{i:064x}", "t9_blog", "fact.number", lab, "hard" if i < 12 else "representative",
                             "a" * 64, "content_text", f"claim {i}", "", ""))
    votes = {it.claim_hash: [it.label == "violation"] * 3 for it in items}
    rec = r1.measure(items, votes)
    assert rec["classes"]["fact.number"]["passes_bar"] and rec["instrument_conditions_met"]
    assert rec["passing_classes"] == ["fact.number"] and rec["hard_subset"] == {"positives": 12, "caught": 12,
                                                                                "passes": True}
    noisy = {**votes, items[40].claim_hash: [True, False, True]}
    assert r1.measure(items, noisy)["retest"] < 1.0
    assert r1.majority([None, None, True]) is None and r1.majority([True, False, None]) is False
    rep = r1.report(items, {"votes": {LUNA.id: votes, LUNA_X.id: {}}}, [LUNA, LUNA_X])
    assert rep["recommended"] == LUNA.id


def test_live_logging_passes_billed_fields(monkeypatch):
    from shared.llm_client import call_log

    seen = {}
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: seen.setdefault("ok", kw))
    monkeypatch.setattr(call_log, "record_unusable_sync", lambda resp, **kw: seen.setdefault("bad", kw))
    resp = _resp("{}")
    resp.pricing_mode = "list"
    r1._log(resp, {"run_id": "zz"}, {}, None)
    r1._log(resp, {"run_id": "zz"}, None, ValueError("x"))
    assert seen["ok"]["stage"] == r1.STAGE == "adhoc_aa1048" and seen["ok"]["role"] == "judge"
    assert seen["ok"]["pricing_mode"] == "list" and seen["ok"]["connection_key"] == "openai-direct"
    assert seen["bad"]["stage"] == r1.STAGE


def test_identity_check_reads_the_answering_connection():
    assert r1._identity_ok(LUNA, _resp("{}"))
    assert not r1._identity_ok(LUNA, _resp("{}", conn="other-conn"))


def test_script_dry_run_makes_no_call_and_refuses_live_without_confirm(tmp_path, capsys):
    from scripts.aa1048 import r1_calibrate

    export = tmp_path / "claims.jsonl"
    finals = tmp_path / "finals.jsonl"
    rows = [{"claim_hash": it.claim_hash, "writer": it.writer, "content_sha256": it.content_sha256,
             "field": it.field, "claim_text": it.claim_text, "field_text": it.field_text,
             "source_text": it.source_text} for it in _items()]
    export.write_text("".join(json.dumps(r) + "\n" for r in rows))
    finals.write_text("".join(json.dumps({"claim_hash": it.claim_hash, "claim_class": it.claim_class,
                                          "label": it.label}) + "\n" for it in _items()))
    from services.golden_set import llm_guard

    undo = []
    monkey = llm_guard.install
    llm_guard.install = lambda reason: undo.append(reason) or (lambda: None)
    try:
        assert r1_calibrate.main(["--export", str(export), "--finals", str(finals)]) == 0
    finally:
        llm_guard.install = monkey
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run" and out["plan"]["items"] == 4 and undo == ["aa1048_r1_dry_run"]
    with pytest.raises(SystemExit):
        r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--live"])
    assert r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--candidates", "bad"]) == 2
    assert r1_calibrate._candidates("m@c,n@d") == [("m", "c"), ("n", "d")]
