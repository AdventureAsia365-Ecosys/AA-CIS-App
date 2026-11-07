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
                           "content_text", text, FIELD, SOURCE, ("Day 2: Walk 12 km to Ostrin Valley.",),
                           "held_out"))
    return out


def _tour_in(split):
    """An invented tour id whose group key hashes to `split`."""
    from services.ground_truth import claims as cl

    return next(f"tour-{n}" for n in range(1000) if cl.split_of(f"tour_id:tour-{n}") == split)


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
    assert plan["fits_cap"] and plan["expected_total_usd"] < 0.1 and plan["max_tokens"] >= 8192
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


def test_items_from_keeps_violation_and_ok_only_of_the_split():
    held, train = {"tour_id": _tour_in("held_out")}, {"tour_id": _tour_in("train")}
    export = [{"claim_hash": "h1", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Walk 40 km.", "field_text": FIELD, "source_text": SOURCE, "refs": held},
              {"claim_hash": "h2", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Unsure one.", "source_text": SOURCE, "refs": held},
              {"claim_hash": "h3", "writer": "t9_blog", "content_sha256": "a" * 64, "field": "content_text",
               "claim_text": "Not labelled.", "source_text": SOURCE, "refs": held},
              {"claim_hash": "h4", "writer": "t9_blog", "content_sha256": "b" * 64, "field": "content_text",
               "claim_text": "Walk 40 km again.", "source_text": SOURCE, "refs": train, "split": "held_out"}]
    finals = {"h1": {"claim_class": "fact.number", "label": "violation"},
              "h2": {"claim_class": "fact.number", "label": "unsure"},
              "h4": {"claim_class": "fact.number", "label": "violation"}}
    items = r1.items_from(export, finals)
    assert [i.claim_hash for i in items] == ["h1"] and items[0].evidence and items[0].split == "held_out"
    # the split is recomputed from the group key, never read from the line (h4 says held_out, its tour is train)
    assert [i.claim_hash for i in r1.items_from(export, finals, split="train")] == ["h4"]
    assert [i.claim_hash for i in r1.items_from(export, finals, split="all")] == ["h1", "h4"]
    with pytest.raises(r1.R1Refused, match="split"):
        r1.items_from(export, finals, split="gate")


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
    assert rec["retest"] == 1.0 and rec["unverified"] == 0             # 2 agreeing real votes each


def test_cap_stops_before_the_call_that_would_pass_it():
    items = _items()
    uns = json.dumps({"verdict": "unsupported", "quote": "", "why": ""})
    worst = max(r1.worst_call_cost(LUNA, r1.entailment_prompt(i)) for i in items)
    cost = worst / 10
    cap = worst + cost / 2                                                # room for exactly one call
    result, _ = _run(items, [LUNA], [_resp(uns, cost=cost)] * 20, cap=cap)
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
                             "a" * 64, "content_text", f"claim {i}", "", "", (), "held_out"))
    votes = {it.claim_hash: [it.label == "violation"] * 3 for it in items}
    rec = r1.measure(items, votes)
    assert rec["classes"]["fact.number"]["passes_bar"] and rec["instrument_conditions_met"]
    assert rec["split"] == "held_out"
    assert rec["passing_classes"] == ["t9_blog:fact.number"] and rec["hard_subset"] == {"positives": 12, "caught": 12,
                                                                                "passes": True}
    noisy = {**votes, items[40].claim_hash: [True, False, True]}
    assert r1.measure(items, noisy)["retest"] < 1.0
    assert r1.majority([None, None, True]) is None and r1.majority([True, False, None]) is None
    rep = r1.report(items, {"votes": {LUNA.id: votes, LUNA_X.id: {}}}, [LUNA, LUNA_X])
    assert rep["recommended"] == LUNA.id and rep["split"] == "held_out"
    # the same numbers on the train side (or pooled) are exploration: no passing class, no recommendation
    import dataclasses

    for other in ("train", "mixed"):
        moved = [dataclasses.replace(it, split="train") if other == "train" or n % 2 else it
                 for n, it in enumerate(items)]
        rec = r1.measure(moved, votes)
        assert rec["split"] == other and rec["classes"]["fact.number"]["passes_bar"]
        assert rec["passing_classes"] == [] and r1.report(moved, {"votes": {LUNA.id: votes}}, [LUNA])[
            "recommended"] is None


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
             "source_text": it.source_text, "refs": {"tour_id": _tour_in("held_out")}} for it in _items()]
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
    assert out["plan"]["split"] == "held_out"
    assert r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--split", "train"]) == 2
    assert "no labelled" in json.loads(capsys.readouterr().out)["refused"]
    with pytest.raises(SystemExit):
        r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--live"])
    assert r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--candidates", "bad"]) == 2
    assert r1_calibrate._candidates("m@c,n@d") == [("m", "c"), ("n", "d")]


# ── review S272: the 2 USD cap is hard ────────────────────────────────────────────────────────────────────────────

KIMI = r1.Instrument("kimi-k3", "acc2", "entailment")


def _many(n=50):
    return [r1.Item(f"{i:064x}", "t9_blog", "fact.number", "violation" if i % 2 else "ok", "representative",
                    "a" * 64, "content_text", f"Walk {i} km to Ostrin Valley.", FIELD, SOURCE,
                    ("Day 2: Walk 12 km to Ostrin Valley.",), "held_out") for i in range(n)]


def _kimi_reply(**kw):
    r = _resp(json.dumps({"verdict": "unsupported", "quote": "", "why": ""}), model="kimi-k3", conn="acc2")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_cap_counts_the_cut_attempt_of_a_truncation_retry():
    """Review S272 repro: kimi-k3 entailment, 50 items x 3 votes, cap 2.0, every reply cost_usd 0.01 with
    truncation_cut_cost_usd 0.30. The budget charged 0.01 a call (spent 1.5, no stop) while 46.5 USD was billed."""
    billed = []

    def caller(inst, system, user, schema):
        r = _kimi_reply(cost_usd=0.01, truncation_cut_cost_usd=0.30, truncation_retry_max_tokens=16384,
                        truncation_retry_reason="cut_reply")
        billed.append(0.31)
        return r
    result = r1.run(_many(), [KIMI], cap=2.0, run_id="zz", caller=caller, log=lambda *a: None)
    assert sum(billed) <= 2.0 and result["stopped"]
    assert result["charged_usd"] == pytest.approx(sum(billed))
    # a retry that gives no cut cost is charged the first attempt's worst case; a refusal to return a cut bills nothing
    user = r1.entailment_prompt(_many(1)[0])
    amount, notes = r1.billed_cost(KIMI, user, _kimi_reply(cost_usd=0.01, truncation_retry_max_tokens=16384))
    assert amount == pytest.approx(0.01 + r1.call_cost(KIMI, user, r1.ceiling("kimi-k3"))) and notes
    amount, _ = r1.billed_cost(KIMI, user, _kimi_reply(cost_usd=0.01, truncation_retry_max_tokens=16384,
                                                       truncation_retry_reason="truncated_refusal"))
    assert amount == pytest.approx(0.01)
    # earlier attempts the reply reports (attempt_no 3: two before it) are charged one attempt's worst case each
    amount, _ = r1.billed_cost(KIMI, user, _kimi_reply(cost_usd=0.01, attempt_no=3))
    assert amount == pytest.approx(0.01 + 2 * r1.worst_call_cost(KIMI, user))


def test_cap_charges_a_missing_cost_at_the_worst_case():
    """Review S272 repro: cost_usd None and 8192-token replies: the budget charged the expected 150-token cost
    (0.54 USD) while 20.28 USD was billed."""
    billed = []

    def caller(inst, system, user, schema):
        billed.append(r1.call_cost(inst, user, 8192))
        return _kimi_reply(cost_usd=None, output_tokens=8192)
    result = r1.run(_many(), [KIMI], cap=2.0, run_id="zz", caller=caller, log=lambda *a: None)
    assert sum(billed) <= 2.0 and result["stopped"] and result["charged_usd"] >= sum(billed)
    assert result["worst_case_charges"]["no_cost_worst_case"] == len(billed)
    assert r1.billed_cost(KIMI, "x", _kimi_reply(cost_usd=0))[0] == r1.worst_call_cost(KIMI, "x")


def test_the_gate_uses_the_worst_case_with_the_retry_ceiling_and_the_reasoning_floor():
    assert r1.ceiling("kimi-k3") == 8192 and r1.retry_ceiling("kimi-k3") == 16384
    assert r1.worst_call_cost(KIMI, "x") == pytest.approx(r1.call_cost(KIMI, "x", 8192)
                                                          + r1.call_cost(KIMI, "x", 16384))
    # the plan adds the expected reasoning of a reasoning model (GPT-6 Luna), none for a model that does not reason
    assert r1.expected_out(LUNA) == r1.EXPECTED_OUT["entailment"] + r1.EXPECTED_REASONING
    assert r1.expected_out(r1.Instrument("llama4-maverick", "acc2", "entailment")) == r1.EXPECTED_OUT["entailment"]
    plan = r1.plan(_items(), [LUNA])
    (row,) = plan["instruments"]
    assert row["expected_out_tokens"] == r1.expected_out(LUNA) and row["retry_ceiling"] == 16384
    # a ledger reading above the run's own charges stops it
    result = r1.run(_many(4), [KIMI], cap=2.0, run_id="zz", caller=lambda *a: _kimi_reply(cost_usd=0.001),
                    log=lambda *a: None, spend_probe=lambda: 1.99)
    assert result["calls"] == 0 and result["stopped"] and result["ledger_usd"] == 1.99


def test_a_caller_error_keeps_the_votes_charges_the_worst_case_and_stops_clean():
    items = _many(4)
    uns = json.dumps({"verdict": "unsupported", "quote": "", "why": ""})
    seq = iter([_resp(uns), _resp(uns), RuntimeError("route exhausted")])

    def caller(*a):
        nxt = next(seq)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    result = r1.run(items, [LUNA], cap=2.0, run_id="zz", caller=caller, log=lambda *a: None)
    votes = result["votes"][LUNA.id]
    assert votes[items[0].claim_hash][0] is True and votes[items[1].claim_hash][0] is True
    assert votes[items[2].claim_hash] == [None, None, None] and result["calls"] == 3
    assert result["stopped"].startswith("caller error") and result["caller_errors"][0]["error"] == "RuntimeError"
    worst = r1.worst_call_cost(LUNA, r1.entailment_prompt(items[2]))
    assert result["charged_usd"] == pytest.approx(0.0008 + worst, abs=1e-6)
    rep = r1.report(items, result, [LUNA])
    assert rep["partial"] is True and rep["caller_errors"]


def test_script_writes_the_partial_report_after_a_caller_error(tmp_path, monkeypatch, capsys):
    from scripts.aa1048 import r1_calibrate

    held = _tour_in("held_out")
    export, finals, out = tmp_path / "claims.jsonl", tmp_path / "finals.jsonl", tmp_path / "r1.json"
    export.write_text("".join(json.dumps({"claim_hash": it.claim_hash, "writer": it.writer,
                                          "content_sha256": it.content_sha256, "field": it.field,
                                          "claim_text": it.claim_text, "field_text": it.field_text,
                                          "source_text": it.source_text, "refs": {"tour_id": held}}) + "\n"
                              for it in _items()))
    finals.write_text("".join(json.dumps({"claim_hash": it.claim_hash, "claim_class": it.claim_class,
                                          "label": it.label}) + "\n" for it in _items()))

    def boom(*a):
        raise TimeoutError("deadline passed")
    monkeypatch.setattr(r1_calibrate, "_caller", boom)
    monkeypatch.setattr(r1_calibrate, "ledger_probe", lambda since: (lambda: None))
    rc = r1_calibrate.main(["--export", str(export), "--finals", str(finals), "--live", "--confirm-spend",
                            "--cap", "2", "--candidates", "gpt-6-luna-openai@openai-direct", "--modes", "entailment",
                            "--out", str(out)])
    assert rc == 3
    rep = json.loads(out.read_text())
    assert rep["partial"] is True and rep["caller_errors"][0]["error"] == "TimeoutError" and rep["charged_usd"] > 0
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ── review S272: instrument-wide precision, unverified votes ─────────────────────────────────────────────────────


def _item(i, cls, label, stratum="representative"):
    return r1.Item(f"{i:064x}", "t9_blog", cls, label, stratum, "a" * 64, "content_text", f"claim {i}", "", "", (),
                   "held_out")


def test_precision_is_the_instruments_over_every_fire():
    """Review S272 repro: 20 hard fact.number violations all flagged, 200 ok scope.framing claims of which 30 are
    flagged: fact.number passed at class precision 1.0 while the instrument's precision as a hold is 20 / 50."""
    items = [_item(i, "fact.number", "violation", "hard") for i in range(20)]
    items += [_item(100 + i, "scope.framing", "ok") for i in range(200)]
    votes = {it.claim_hash: [True] * 3 for it in items[:20]}
    votes.update({it.claim_hash: [n < 30] * 3 for n, it in enumerate(items[20:])})
    rec = r1.measure(items, votes)
    assert rec["ok_cleared"] == 0.85 and rec["classes"]["fact.number"]["precision"] == 1.0
    assert (rec["tp"], rec["fired"], rec["precision"]) == (20, 50, 0.4) and not rec["precision_passes_bar"]
    assert not rec["classes"]["fact.number"]["passes_bar"] and rec["passing_classes"] == []


def test_unverified_votes_count_as_misses():
    """Review S272 repro: 30 hard violations, 20 of them [None, None, None], the other 10 flagged: the hard subset
    passed on 10 of 10 answered."""
    items = [_item(i, "fact.number", "violation", "hard") for i in range(30)]
    items += [_item(100 + i, "fact.number", "ok") for i in range(30)]
    votes = {it.claim_hash: ([None] * 3 if n < 20 else [True] * 3) for n, it in enumerate(items[:30])}
    votes.update({it.claim_hash: [False] * 3 for it in items[30:]})
    rec = r1.measure(items, votes)
    assert rec["hard_subset"] == {"positives": 30, "caught": 10, "passes": False}
    assert rec["classes"]["fact.number"]["recall"] == round(10 / 30, 4) and rec["unverified"] == 20
    assert rec["unverified_by_label"] == {"violation": 20} and not rec["instrument_conditions_met"]
    # a few unverified ok claims are not cleared, and over UNVERIFIED_MAX the instrument fails its conditions
    votes = {it.claim_hash: [True] * 3 for it in items[:30]}
    votes.update({it.claim_hash: ([None] * 3 if n < 4 else [False] * 3) for n, it in enumerate(items[30:])})
    rec = r1.measure(items, votes)
    assert rec["ok_cleared"] == round(26 / 30, 4) and rec["unverified_rate"] > r1.UNVERIFIED_MAX
    assert not rec["instrument_conditions_met"] and rec["passing_classes"] == []


# ── review S272 round 3: non-finite costs, route attempts, split votes, per-writer precision ────────────────────


def test_a_nan_or_inf_cost_is_charged_the_worst_case():
    """Review S272 repro: cost_usd NaN, 300 items x 3 votes, cap 0.05: 900 calls ran with spent 0.0 and no stop."""
    items = _many(300)
    worst = min(r1.worst_call_cost(LUNA, r1.entailment_prompt(i)) for i in items)
    uns = json.dumps({"verdict": "unsupported", "quote": "", "why": ""})
    for bad in (float("nan"), float("inf")):
        result = r1.run(items, [LUNA], cap=0.05, run_id="zz", caller=lambda *a, b=bad: _resp(uns, cost=b),
                        log=lambda *a: None)
        assert result["stopped"] and 0 < result["calls"] <= int(0.05 / worst)
        assert result["spent_usd"] <= 0.05 and result["worst_case_charges"]["no_cost_worst_case"] == result["calls"]
    # a NaN cut cost takes the worst-case cut path; Budget.charge falls back to the worst case on a NaN amount
    user = r1.entailment_prompt(items[0])
    amount, notes = r1.billed_cost(KIMI, user, _kimi_reply(cost_usd=0.01, truncation_cut_cost_usd=float("nan"),
                                                           truncation_retry_max_tokens=16384))
    assert amount == pytest.approx(0.01 + r1.call_cost(KIMI, user, r1.ceiling("kimi-k3"))) and notes
    b = r1.Budget(1.0)
    b.charge(float("nan"), 0.3)
    assert b.charged == pytest.approx(0.3)
    # a NaN ledger reading (a NaN cost row makes the SUM NaN) stops the run before any call
    result = r1.run(_many(4), [KIMI], cap=2.0, run_id="zz", caller=lambda *a: _kimi_reply(cost_usd=0.001),
                    log=lambda *a: None, spend_probe=lambda: float("nan"))
    assert result["calls"] == 0 and "ledger" in result["stopped"]


def test_the_gate_reserves_every_route_attempt():
    """Review S272 repro: cap 1.01 x worst_call_cost, a reply of 0.5 x worst with attempt_no 4: the call ran and the
    spend ended about 2.5x the cap."""
    items = _many(1)
    user = r1.entailment_prompt(items[0])
    one = r1.worst_call_cost(KIMI, user)

    def reply(*a):
        return _kimi_reply(cost_usd=0.5 * one, attempt_no=4)
    # four reserved attempts: the call does not fit, nothing is spent
    result = r1.run(items, [KIMI], cap=1.01 * one, votes=1, run_id="zz", caller=reply, log=lambda *a: None,
                    attempts_of=lambda inst: 4)
    assert result["calls"] == 0 and result["stopped"] and result["spent_usd"] == 0
    assert r1.worst_call_cost(KIMI, user, 4) == pytest.approx(4 * one)
    # a reply past the reserved attempts stops the run at once
    result = r1.run(_many(4), [KIMI], cap=2.0, votes=1, run_id="zz", caller=reply, log=lambda *a: None)
    assert result["calls"] == 1 and "reserved attempt" in result["stopped"]

    def boom(*a):
        raise RuntimeError("route exhausted")
    # a caller error is charged every reserved attempt
    result = r1.run(items, [KIMI], cap=2.0, votes=1, run_id="zz", caller=boom, log=lambda *a: None,
                    attempts_of=lambda inst: 3)
    assert result["charged_usd"] == pytest.approx(3 * one, abs=1e-6) and result["reserved_attempts"] == {KIMI.id: 3}


def test_route_attempts_counts_the_models_enabled_deployments(monkeypatch):
    from shared.llm_client import deployments

    monkeypatch.setattr(deployments, "alias_targets_sync", lambda key: ())
    monkeypatch.setattr(deployments, "deployments_for_sync", lambda key: ["acc2", "acc3", "openai"])
    assert r1.route_attempts(KIMI) == 3
    monkeypatch.setattr(deployments, "alias_targets_sync", lambda key: ("m1", "m2"))
    assert r1.route_attempts(KIMI) == 6
    monkeypatch.setattr(deployments, "deployments_for_sync", lambda key: [])
    from shared.llm_client.errors import MAX_CHAIN_ATTEMPTS
    assert r1.route_attempts(KIMI) == MAX_CHAIN_ATTEMPTS       # unreadable/empty registry: the gateway's own cap

    def boom(key):
        raise RuntimeError("registry down")
    monkeypatch.setattr(deployments, "deployments_for_sync", boom)
    assert r1.route_attempts(KIMI) == MAX_CHAIN_ATTEMPTS


def test_a_split_vote_with_a_missing_vote_is_unverified():
    """Review S272 repro: 25 hard violations [T, T, T] and 100 ok claims [T, F, None]: the ok claims counted as
    cleared and the instrument passed while it fires on half of the real votes on ok claims."""
    assert r1.majority([True, False, None]) is None and r1.majority([True, None, True]) is True
    assert r1.majority([False, False, None]) is False and r1.majority([True, False]) is None
    items = [_item(i, "fact.number", "violation", "hard") for i in range(25)]
    items += [_item(100 + i, "fact.number", "ok") for i in range(100)]
    votes = {it.claim_hash: [True] * 3 for it in items[:25]}
    votes.update({it.claim_hash: [True, False, None] for it in items[25:]})
    rec = r1.measure(items, votes)
    assert rec["unverified"] == 100 and rec["ok_cleared"] == 0.0 and rec["retest"] == round(25 / 125, 4)
    assert not rec["instrument_conditions_met"] and rec["passing_classes"] == []


def test_precision_bar_is_per_writer_surface():
    """Review S272 repro: a1 120 hard violations caught + 200 ok cleared, t9_social 20 violations + 12 ok all fired:
    pooled precision 0.921 passed the class while t9_social fires at 0.625."""
    def it(i, writer, label, stratum="representative"):
        return r1.Item(f"{i:064x}", writer, "unsupported_superlative", label, stratum, "a" * 64, "content_text",
                       f"claim {i}", "", "", (), "held_out")
    items = [it(i, "a1", "violation", "hard") for i in range(120)]
    items += [it(1000 + i, "a1", "ok") for i in range(200)]
    items += [it(2000 + i, "t9_social", "violation") for i in range(20)]
    items += [it(3000 + i, "t9_social", "ok") for i in range(12)]
    votes = {x.claim_hash: [x.writer == "t9_social" or x.label == "violation"] * 3 for x in items}
    rec = r1.measure(items, votes)
    assert rec["precision"] == round(140 / 152, 4) and rec["precision_passes_bar"]          # pooled: reading only
    social = rec["writers"]["t9_social"]
    assert social["precision"] == 0.625 and not social["precision_passes_bar"]
    assert rec["writers"]["a1"]["precision_passes_bar"]
    assert rec["classes"]["unsupported_superlative"]["passing_writers"] == ["a1"]
    assert rec["instrument_conditions_met"] and rec["passing_classes"] == ["a1:unsupported_superlative"]


def test_a_writer_passes_only_on_its_own_labelled_violations():
    """Review S272 repro: a1 has 18 caught violations of Y and 1 caught violation of X; t9_blog has 19 missed
    violations of X. Pooled n_pos let 'a1:X' pass on t9_blog's labels; n_pos now counts per writer and class."""
    def it(i, writer, cls, label, stratum="representative"):
        return r1.Item(f"{i:064x}", writer, cls, label, stratum, "a" * 64, "content_text", f"claim {i}", "", "", (),
                       "held_out")
    items = [it(i, "a1", "Y", "violation", "hard" if i < 10 else "representative") for i in range(18)]
    items += [it(100, "a1", "X", "violation")]
    items += [it(200 + i, "a1", "Z", "ok") for i in range(20)]
    items += [it(300 + i, "t9_blog", "X", "violation") for i in range(19)]
    items += [it(400 + i, "t9_blog", "Z", "ok") for i in range(180)]
    votes = {x.claim_hash: [x.writer == "a1" and x.label == "violation"] * 3 for x in items}
    for x in items[19 + 20 + 19: 19 + 20 + 19 + 30]:
        votes[x.claim_hash] = [True] * 3
    rec = r1.measure(items, votes)
    assert rec["classes"]["X"]["n_pos_by_writer"] == {"a1": 1, "t9_blog": 19}
    assert "a1:X" not in rec["passing_classes"]
