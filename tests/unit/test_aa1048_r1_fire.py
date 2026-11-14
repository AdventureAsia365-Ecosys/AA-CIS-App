"""AA-1048 (S281 lane l13) — the R1 firing run on unlabelled eligible claims (services/ground_truth/r1_fire.py): the
sample is deterministic, salted, writer-proportional and capped by the setting; an eligible file that is in-sample for
the two-step prompts is refused in every mode; the cap and the settings refuse a run before any call; a live run (fake
caller) logs every reply through r1._log with a call id and writes a text-free report that fresh_pool sample reads as
--votes (a trial report is refused there and prices the full run). $0; every text, place and hash is invented."""
import hashlib
import json
import types

import pytest

from services.ground_truth import fresh_pool as fp
from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from services.ground_truth import r1_fire as rf

SOURCE = "Day 1: Arrive in Pellow.\nDay 2: Walk the dunes of Sarrow.\nDay 3: Drive back to Pellow."
SHAPED_RUNS = ["2977dd78", "3018008d", "33835b87"]


def _h(n: int) -> str:
    return hashlib.sha256(f"invented-fire-{n}".encode()).hexdigest()


def _piece(n: int) -> str:
    return hashlib.sha256(f"invented-fire-piece-{n}".encode()).hexdigest()


TRAIN = [n for n in range(200) if r1.split_of_line({"content_sha256": _piece(n)}) == "train"][:6]
HELD = [n for n in range(200) if r1.split_of_line({"content_sha256": _piece(n)}) == "held_out"][:2]


WORDS = ("amber", "basalt", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "island", "juniper", "kelp",
         "lagoon", "meadow")


def _claim(n: int) -> str:
    """Distinct invented sentences (R1 maps a quote onto the claims whose words it shares); every third at sunrise."""
    return f"The {WORDS[n % len(WORDS)]} stop{' at sunrise' if n % 3 == 0 else ''}."


def _lines(n=12, pieces=None):
    """n claims on 4 train pieces, writers alternating; each piece's field text holds all of its claims."""
    pieces = pieces or TRAIN[:4]
    by_piece = {p: [k for k in range(n) if pieces[k % len(pieces)] == p] for p in pieces}
    out = []
    for k in range(n):
        p = pieces[k % len(pieces)]
        field = "Intro line.\n" + "\n".join(_claim(j) for j in by_piece[p]) + "\nClosing line."
        out.append({"claim_hash": _h(k), "writer": "t2" if k % 2 else "t9_blog", "field": "itineraries",
                    "claim_text": _claim(k), "field_text": field, "source_text": SOURCE,
                    "content_sha256": _piece(p), "stratum": "representative"})
    return out


def _files(tmp_path, lines=None, **manifest):
    lines = lines if lines is not None else _lines()
    elig = tmp_path / "eligible.jsonl"
    elig.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    man = {"kind": "fresh_eligible", "issue": "AA-1048", "split": "train", "excluded_runs": SHAPED_RUNS,
           "excluded_pools": [pr.S279_POOL], "excluded_pieces": [_piece(TRAIN[5])[:16]],
           "eligible_sha256": fp._sha(elig.read_text(encoding="utf-8")), **manifest}
    mp = tmp_path / "eligible-manifest.json"
    mp.write_text(json.dumps(man), encoding="utf-8")
    return str(elig), str(mp)


def _resp(payload, model="gpt-6-luna-openai", conn="openai-direct", cost=0.0004, request_id="req-1"):
    return types.SimpleNamespace(content=json.dumps(payload), model_used=model, connection_key=conn,
                                 satellite_account=None, cost_usd=cost, input_tokens=900, output_tokens=40,
                                 provider="openai", stop_reason="stop", fallback_used=False, request_id=request_id)


def _sunrise_caller(calls):
    """Quotes every line of the field text that says 'at sunrise' (code confirms each quote is in the text)."""
    def caller(inst, system, user, schema):
        calls.append(schema["name"])
        text = user.split("\nTEXT (", 1)[1].split("\n", 1)[1]
        return _resp({"claims": [{"quote": ln, "class": "fact.embellish", "why": "x"}
                                 for ln in text.splitlines() if "sunrise" in ln]}, request_id=f"req-{len(calls)}")
    return caller


def _live(monkeypatch, caller, rows=None):
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(rf, "_probe", lambda since: (lambda: 0.0))
    monkeypatch.setattr(rf, "_caller", caller)
    from shared.llm_client import call_log

    sink = rows if rows is not None else []
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: sink.append(kw))
    monkeypatch.setattr(rf, "_ledger_rows", lambda run_id: len(sink))
    return sink


# ── instrument, settings ──────────────────────────────────────────────────────────────────────────────────────────


def test_the_fire_instrument_is_the_r1_extraction_instrument_fresh_pool_reads():
    inst = rf.instrument()
    assert inst.id == fp.FIRE_INSTRUMENT and inst.mode == "extraction" and inst.prompt == pr.DEFAULT


def test_limits_come_from_the_settings_registry():
    lim = rf.limits()
    assert lim.sample_max == int(rf.settings.get("cis.quality.claim_fire_sample_max"))
    assert lim.cap_usd == ev.limits().cap_usd <= r1.HARD_CAP_USD and lim.trial_max == ev.limits().trial_claims


# ── sample ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_sample_is_deterministic_salted_writer_proportional_and_capped():
    lines = _lines(12)
    a = rf.draw(lines, 6, "aa1048-fire:0011223344556677")
    assert a == rf.draw(list(reversed(lines)), 6, "aa1048-fire:0011223344556677") and len(a) == 6
    assert sum(e["writer"] == "t2" for e in a) == 3                                  # 6 : 6 of 12 -> 3 : 3 of 6
    b = rf.draw(lines, 6, "aa1048-fire:8899aabbccddeeff")
    assert rf.digest(a) != rf.digest(b) and rf.digest(a) == rf.digest(list(reversed(a)))
    assert len(rf.draw(lines, 50, "s")) == 12
    assert rf.salt_of({"eligible_sha256": "ab" * 32}) == "aa1048-fire:" + "ab" * 8
    assert rf.salt_of({"eligible_sha256": "ab" * 32}) != fp.salt_of({"eligible_sha256": "ab" * 32})


def test_an_unlabelled_line_becomes_an_item_that_says_so():
    item = rf.item_of(_lines(1)[0])
    assert item.label == "unlabelled" and item.claim_class == "unlabelled" and item.split == "train"
    assert item.object_key.endswith(":itineraries")


# ── out of sample ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_fresh_train_manifest_is_out_of_sample():
    lines = _lines()
    oos = rf.out_of_sample({"split": "train", "excluded_runs": SHAPED_RUNS, "excluded_pools": [],
                            "excluded_pieces": []}, lines)
    assert rf.oos_refusal(oos) is None and oos["pool_side"] == "train_split"
    named = rf.out_of_sample({"split": "all", "excluded_runs": SHAPED_RUNS, "excluded_pools": [pr.S279_POOL]},
                             lines + _lines(1, pieces=[HELD[0]]))
    assert rf.oos_refusal(named) is None and named["pool_side"] == "excluded"


@pytest.mark.parametrize("manifest,extra,needle", [
    ({"excluded_runs": ["3018008d", "33835b87"]}, [], "2977dd78"),
    ({"excluded_pieces": [_piece(TRAIN[0])[:16]]}, [], "excluded as in-sample"),
    ({}, "held", "not on the manifest's split"),
    ({"split": "held_out", "excluded_pools": []}, "only_held", "shaped on pool"),
])
def test_an_in_sample_eligible_file_is_refused_before_any_call(tmp_path, monkeypatch, capsys, manifest, extra,
                                                               needle):
    lines = _lines()
    if extra == "held":
        lines += _lines(1, pieces=[HELD[0]])
    if extra == "only_held":
        lines = _lines(4, pieces=[HELD[0], HELD[1]])
    elig, man = _files(tmp_path, lines, **manifest)
    monkeypatch.setattr(rf, "_caller", lambda *a: pytest.fail("a refused run makes no call"))
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    assert rf.main(["--eligible", elig, "--manifest", man]) == 2
    assert needle in json.loads(capsys.readouterr().out)["refused"]
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend"]) == 2
    assert needle in json.loads(capsys.readouterr().out)["refused"]


def test_another_eligible_file_or_a_non_eligible_manifest_is_refused(tmp_path, capsys):
    elig, man = _files(tmp_path)
    with open(elig, "a", encoding="utf-8") as f:
        f.write(json.dumps(_lines(13)[12]) + "\n")
    assert rf.main(["--eligible", elig, "--manifest", man]) == 2
    assert "sha256 differs" in json.loads(capsys.readouterr().out)["refused"]
    elig, man = _files(tmp_path, kind="fresh_pool")
    assert rf.main(["--eligible", elig, "--manifest", man]) == 2
    assert "fresh_eligible" in json.loads(capsys.readouterr().out)["refused"]


# ── DRY RUN, settings and cap refusals ────────────────────────────────────────────────────────────────────────────


def test_dry_run_prints_the_sample_and_the_plan_without_a_call(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    monkeypatch.setattr(rf, "_caller", lambda *a: pytest.fail("a DRY RUN makes no call"))
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    assert rf.main(["--eligible", elig, "--manifest", man, "--size", "8"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["mode"] == "dry_run" and rep["kind"] == "r1_fire" and rep["fire_kind"] == "full"
    assert rep["sample"]["claims"] == 8 and rep["sample"]["by_writer"] == {"t2": 4, "t9_blog": 4}
    assert rep["plan"]["instruments"][0]["calls"] == rep["firing"]["objects"] * r1.VOTES
    assert "labels" not in rep["plan"] and "votes" not in rep and "Sarrow" not in json.dumps(rep)
    assert rf.main(["--eligible", elig, "--manifest", man, "--size", "8", "--expect-sample",
                    rep["sample"]["digest"]]) == 0
    capsys.readouterr()
    assert rf.main(["--eligible", elig, "--manifest", man, "--size", "7", "--expect-sample",
                    rep["sample"]["digest"]]) == 2
    assert "not the expected" in json.loads(capsys.readouterr().out)["refused"]


def test_size_trial_and_cap_outside_the_settings_are_refused(tmp_path, capsys):
    elig, man = _files(tmp_path)
    lim = rf.limits()
    for args, needle in ((["--size", str(lim.sample_max + 1)], "claim_fire_sample_max"),
                         (["--trial", str(lim.trial_max + 1)], "instrument_eval_trial_claims"),
                         (["--cap", str(lim.cap_usd + 0.01)], "instrument_eval_cap_usd"),
                         (["--live"], "--confirm-spend")):
        assert rf.main(["--eligible", elig, "--manifest", man, *args]) == 2
        assert needle in json.loads(capsys.readouterr().out)["refused"]


def test_a_plan_over_the_cap_is_refused_and_the_budget_stops_before_the_cap():
    lines = _lines()
    elig_items = [rf.item_of(e) for e in lines]
    assert rf.plan(elig_items, rf.instrument(), cap=0.0001)["fits_cap"] is False
    res = rf.fire(elig_items, rf.instrument(), cap=0.0001, run_id="r", log=lambda *a: None,
                  caller=lambda *a: pytest.fail("no call may start: the first worst case passes the cap"))
    assert res["calls"] == 0 and res["stopped"].startswith("cap 0.0001 USD") and res["ledger"] == []
    assert all(v == [None] * r1.VOTES for v in res["votes"].values())


def test_cli_refuses_a_plan_over_the_cap_before_any_call(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    _live(monkeypatch, lambda *a: pytest.fail("a refused run makes no call"))
    assert rf.main(["--eligible", elig, "--manifest", man, "--cap", "0.0001", "--live", "--confirm-spend"]) == 3
    assert "does not fit the cap" in json.loads(capsys.readouterr().out)["refused"]


# ── live (fake caller) ────────────────────────────────────────────────────────────────────────────────────────────


def test_fire_votes_per_object_and_logs_every_reply_with_a_call_id():
    lines = _lines()
    items = [rf.item_of(e) for e in lines]
    calls, logged = [], []
    res = rf.fire(items, rf.instrument(), cap=1.0, run_id="run-a", caller=_sunrise_caller(calls),
                  log=lambda resp, sig, parsed, err: logged.append(sig))
    objects = len({i.object_key for i in items})
    assert res["calls"] == objects * r1.VOTES == len(logged) == len(res["ledger"]) and not res["stopped"]
    assert set(calls) == {"claim_extraction"}
    fired = rf.fired_of(res["votes"])
    assert {h for h, v in fired.items() if v} == {_h(k) for k in range(12) if k % 3 == 0}
    assert [e["call_id"] for e in res["ledger"]] == [f"run-a:{n}" for n in range(1, res["calls"] + 1)]
    assert [s["call_id"] for s in logged] == [e["call_id"] for e in res["ledger"]]
    assert {s["purpose"] for s in logged} == {"fresh_pool_fire"} and {s["fire_kind"] for s in logged} == {"full"}
    assert {s["prompt_version"] for s in logged} == {rf.instrument().prompt_version.tag}
    assert res["ledger"][0]["request_id"] == "req-1" and res["ledger"][0]["outcome"] == "vote"
    assert res["mean_prompt_chars"] > 0 and res["charged_usd"] == pytest.approx(0.0004 * res["calls"])


def test_every_reply_goes_through_the_billed_ledger_writer(monkeypatch):
    rows = []
    from shared.llm_client import call_log

    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    rf.fire([rf.item_of(_lines(1)[0])], rf.instrument(), cap=1.0, run_id="r", votes=1,
            caller=lambda *a: _resp({"claims": []}))
    assert len(rows) == 1 and rows[0]["stage"] == r1.STAGE and rows[0]["role"] == "judge"
    assert rows[0]["prompt_version"] == rf.instrument().prompt_version.tag
    assert rows[0]["quality_signal"]["call_id"] == "r:1" and rows[0]["quality_signal"]["purpose"] == "fresh_pool_fire"


def test_a_caller_error_stops_cleanly_and_another_connection_is_no_vote():
    item = rf.item_of(_lines(1)[0])

    def boom(*a):
        raise RuntimeError("route exhausted")
    res = rf.fire([item], rf.instrument(), cap=1.0, run_id="r", caller=boom, log=lambda *a: None)
    assert res["calls"] == 1 and "caller error" in res["stopped"] and res["ledger"][0]["outcome"] == "caller_error"
    assert res["votes"][item.claim_hash] == [None] * r1.VOTES
    res = rf.fire([item], rf.instrument(), cap=1.0, run_id="r", votes=1, log=lambda *a: None,
                  caller=lambda *a: _resp({"claims": []}, conn="acc1"))
    assert res["votes"][item.claim_hash] == [None] and res["identity_mismatches"] == 1
    assert res["ledger"][0]["outcome"] == "identity_mismatch"


def test_live_full_report_feeds_fresh_pool_sample_and_a_trial_prices_it(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    rows = _live(monkeypatch, _sunrise_caller([]))
    trial = tmp_path / "trial.json"
    assert rf.main(["--eligible", elig, "--manifest", man, "--trial", "2", "--live", "--confirm-spend",
                    "--out", str(trial)]) == 0
    t = json.loads(capsys.readouterr().out)
    assert t["fire_kind"] == "trial" and t["firing"]["claims"] == 2 and t["ledger_rows"] == len(rows) == t["calls"]
    # a trial report is refused by fresh_pool sample
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(trial),
                    "--out-dir", str(tmp_path / "s0")]) == 2
    assert "trial" in json.loads(capsys.readouterr().out)["refused"]
    # the trial prices the full run
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    assert rf.main(["--eligible", elig, "--manifest", man, "--measured", str(trial)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["plan"]["instruments"][0]["expected_from"] == "measured" and dry["measured"]["calls"] == t["calls"]
    full = tmp_path / "fire.json"
    assert rf.main(["--eligible", elig, "--manifest", man, "--expect-sample", dry["sample"]["digest"],
                    "--measured", str(trial), "--live", "--confirm-spend", "--out", str(full)]) == 0
    rep = json.loads(full.read_text(encoding="utf-8"))
    assert rep["ok"] and rep["fire_kind"] == "full" and set(rep["votes"]) == {fp.FIRE_INSTRUMENT}
    assert set(rep["fired"]) == {_h(k) for k in range(12)} and rep["fired_count"] == {"true": 4, "false": 8}
    assert all(set(e) == {"call_id", "vote", "object", "claims", "request_id", "charged_usd", "outcome"}
               for e in rep["ledger"])
    assert "Sarrow" not in full.read_text(encoding="utf-8") and "Pellow" not in full.read_text(encoding="utf-8")
    capsys.readouterr()
    out = tmp_path / "sample"
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(full), "--out-dir", str(out)]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["voted"] == 12 and s["fired"] == 4 and s["claims"] == 4 and s["votes_run"] == rep["run_id"][:8]
    assert s["votes_partial"] is False and s["votes_sha256"] == fp._sha(full.read_text(encoding="utf-8"))[:16]


def test_fresh_pool_refuses_a_fire_report_of_another_file_and_measured_refuses_a_dry_run(tmp_path, capsys):
    elig, man = _files(tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"kind": "r1_fire", "mode": "live", "fire_kind": "full", "eligible_sha256": "0" * 64,
                                 "votes": {fp.FIRE_INSTRUMENT: {_h(0): [True] * 3}}}), encoding="utf-8")
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(other),
                    "--out-dir", str(tmp_path / "s")]) == 2
    assert "another eligible file" in json.loads(capsys.readouterr().out)["refused"]
    with pytest.raises(rf.FireRefused, match="finished live fire report"):
        rf.measured_expectation({"kind": "r1_fire", "mode": "dry_run"}, rf.instrument())
    with pytest.raises(rf.FireRefused, match="another instrument"):
        rf.measured_expectation({"kind": "r1_fire", "mode": "live", "calls": 3, "instrument": "x"}, rf.instrument())
