"""AA-1048 (S281 lane l13) — the R1 firing run on unlabelled eligible claims (services/ground_truth/r1_fire.py): the
sample is deterministic, salted, writer-proportional and capped by the setting; an eligible file that is in-sample for
the two-step prompts is refused in every mode; the cap and the settings refuse a run before any call; a live run (fake
caller) logs every reply through r1._log with a call id and writes a text-free report that fresh_pool sample reads as
--votes (a trial report is refused there and prices the full run). $0; every text, place and hash is invented."""
import hashlib
import math
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
    monkeypatch.setattr(rf, "_ledger_rows", lambda run_id: sum(r["quality_signal"]["run_id"] == run_id for r in sink))
    monkeypatch.setattr(rf, "_ledger_calls", lambda run_id: [r["quality_signal"]["call_id"] for r in sink
                                                             if r["quality_signal"]["run_id"] == run_id])
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
    assert all(set(e) == {"call_id", "vote", "object", "claims", "request_id", "charged_usd", "outcome", "latency_s"}
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


# ── checkpoint, resume, time estimate (S281: a fire cut by the exec timeout lost its report) ──────────────────────


class Killed(BaseException):
    """The exec timeout killing the process: nothing in the run catches it, only the checkpoint survives."""


def _killing(caller, after):
    n = []

    def kill(*a):
        if len(n) >= after:
            raise Killed()
        n.append(1)
        return caller(*a)
    return kill


def _ckpt_lines(path):
    return [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]


def _same_run(a, b):
    """The parts of two reports one run must have identically (ledger without run id, request id and latency)."""
    def ledger(rep):
        return [{**{k: v for k, v in e.items() if k not in ("request_id", "latency_s")},
                 "call_id": e["call_id"].rsplit(":", 1)[1]} for e in rep["ledger"]]
    keys = ("votes", "fired", "fired_count", "calls", "charged_usd", "stopped", "ok", "identity_mismatches")
    return {k: a[k] for k in keys} == {k: b[k] for k in keys} and ledger(a) == ledger(b)


def test_a_live_run_writes_one_text_free_checkpoint_line_per_billed_call(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    rows = _live(monkeypatch, _sunrise_caller([]))
    out = tmp_path / "fire.json"
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(out)]) == 0
    rep = json.loads(capsys.readouterr().out)
    lines = _ckpt_lines(str(out) + rf.CHECKPOINT_SUFFIX)
    head, calls = lines[0], lines[1:]
    assert head["type"] == "run" and head["kind"] == rf.CHECKPOINT_KIND and head["run_id"] == rep["run_id"]
    assert {k: head[k] for k in rf.IDENTITY} == {
        "eligible_sha256": rep["eligible_sha256"], "sample_digest": rep["sample"]["digest"],
        "instrument": fp.FIRE_INSTRUMENT, "prompt_version": rep["prompt_version"], "size": rep["sample"]["size"],
        "fire_kind": "full", "trial": None, "votes": r1.VOTES}
    assert len(calls) == rep["calls"] == len(rows) and [c["call_id"] for c in calls] == [
        e["call_id"] for e in rep["ledger"]]
    assert all(set(c["flags"]) <= {_h(k) for k in range(12)} and all(isinstance(v, bool) for v in c["flags"].values())
               for c in calls)
    text = (tmp_path / "fire.json.ckpt.jsonl").read_text(encoding="utf-8")
    for word in ("Sarrow", "Pellow", "sunrise", "amber", "Intro line"):
        assert word not in text
    assert rep["checkpoint"] == "fire.json.ckpt.jsonl" and rep["mean_latency_s"] is not None


def test_a_live_run_needs_a_checkpoint_path_and_never_reuses_one(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    _live(monkeypatch, lambda *a: pytest.fail("a refused run makes no call"))
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend"]) == 2
    assert "--out or --checkpoint" in json.loads(capsys.readouterr().out)["refused"]
    used = tmp_path / "used.ckpt.jsonl"
    used.write_text('{"type": "run"}\n', encoding="utf-8")
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend",
                    "--checkpoint", str(used)]) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["refused"]


def test_resume_after_a_kill_equals_one_uninterrupted_run_and_feeds_fresh_pool(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    rows = _live(monkeypatch, _sunrise_caller([]))
    one = tmp_path / "one.json"
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(one)]) == 0
    capsys.readouterr()
    whole = json.loads(one.read_text(encoding="utf-8"))
    cut = tmp_path / "cut.json"
    monkeypatch.setattr(rf, "_caller", _killing(_sunrise_caller([]), after=5))
    with pytest.raises(Killed):
        rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(cut)])
    capsys.readouterr()
    ckpt = str(cut) + rf.CHECKPOINT_SUFFIX
    head, calls = _ckpt_lines(ckpt)[0], _ckpt_lines(ckpt)[1:]
    assert not cut.exists() and len(calls) == 5
    seen = []
    monkeypatch.setattr(rf, "_probe", lambda since: seen.append(since) or (lambda: 0.0))
    monkeypatch.setattr(rf, "_caller", _sunrise_caller([]))
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--resume", ckpt,
                    "--out", str(cut)]) == 0
    capsys.readouterr()
    rep = json.loads(cut.read_text(encoding="utf-8"))
    assert rep["run_id"] == head["run_id"] and seen[0].isoformat() == head["started_at"]
    assert _same_run(rep, whole) and rep["resumed"] == {"prior_calls": 5, "new_calls": whole["calls"] - 5}
    assert rep["ledger_rows"] == whole["calls"] and len(_ckpt_lines(ckpt)) == whole["calls"] + 1
    assert sum(r["quality_signal"]["run_id"] == head["run_id"] for r in rows) == whole["calls"]   # none re-billed
    a, b = tmp_path / "sa", tmp_path / "sb"
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(one), "--out-dir", str(a)]) == 0
    sa = json.loads(capsys.readouterr().out)
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(cut), "--out-dir", str(b)]) == 0
    sb = json.loads(capsys.readouterr().out)
    assert sa["candidates_sha256"] == sb["candidates_sha256"]
    assert sb["votes_resumed"] is True and sa["votes_resumed"] is False


def test_a_caller_error_is_called_again_on_resume_and_a_partial_report_is_refused_by_fresh_pool(tmp_path, monkeypatch,
                                                                                                capsys):
    elig, man = _files(tmp_path)
    _live(monkeypatch, _sunrise_caller([]))
    whole = rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend",
                     "--out", str(tmp_path / "one.json")])
    capsys.readouterr()
    assert whole == 0
    whole = json.loads((tmp_path / "one.json").read_text(encoding="utf-8"))
    good, n = _sunrise_caller([]), []

    def flaky(*a):
        n.append(1)
        if len(n) == 4:
            raise RuntimeError("route exhausted")
        return good(*a)
    monkeypatch.setattr(rf, "_caller", flaky)
    part = tmp_path / "part.json"
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(part)]) == 3
    capsys.readouterr()
    assert fp.main(["sample", "--eligible", elig, "--manifest", man, "--votes", str(part),
                    "--out-dir", str(tmp_path / "s")]) == 2
    assert "--resume" in json.loads(capsys.readouterr().out)["refused"]
    monkeypatch.setattr(rf, "_caller", _sunrise_caller([]))
    ckpt = str(part) + rf.CHECKPOINT_SUFFIX
    assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--resume", ckpt,
                    "--out", str(part)]) == 0
    rep = json.loads(part.read_text(encoding="utf-8"))
    assert rep["votes"] == whole["votes"] and rep["fired"] == whole["fired"] and rep["stopped"] is None
    assert rep["calls"] == whole["calls"] + 1 and [e["outcome"] for e in rep["ledger"]].count("caller_error") == 1
    assert rep["worst_case_charges"] == {"caller_error_worst_case": 1}
    assert rep["ledger_rows"] == whole["calls"]


@pytest.mark.parametrize("args,needle", [
    (["--size", "11"], "sample_digest, size"),
    (["--trial", "2"], "fire_kind, trial"),
])
def test_resume_refuses_another_run_identity_before_any_call(tmp_path, monkeypatch, capsys, args, needle):
    elig, man = _files(tmp_path)
    _live(monkeypatch, _killing(_sunrise_caller([]), after=2))
    with pytest.raises(Killed):
        rf.main(["--eligible", elig, "--manifest", man, "--size", "12", "--live", "--confirm-spend",
                 "--out", str(tmp_path / "f.json")])
    capsys.readouterr()
    monkeypatch.setattr(rf, "_caller", lambda *a: pytest.fail("a refused resume makes no call"))
    ckpt = str(tmp_path / "f.json") + rf.CHECKPOINT_SUFFIX
    for live in ([], ["--live", "--confirm-spend"]):
        assert rf.main(["--eligible", elig, "--manifest", man, *args, "--resume", ckpt, *live]) == 2
        assert needle in json.loads(capsys.readouterr().out)["refused"]
    assert rf.main(["--eligible", elig, "--manifest", man, "--resume", str(tmp_path / "f.json") + "x"]) == 2
    assert "cannot read the checkpoint" in json.loads(capsys.readouterr().out)["refused"]
    assert rf.main(["--eligible", elig, "--manifest", man, "--size", "12", "--resume", ckpt, "--live",
                    "--confirm-spend", "--checkpoint", str(tmp_path / "other.jsonl")]) == 2
    assert "same path" in json.loads(capsys.readouterr().out)["refused"]


def test_resume_refuses_when_the_ledger_disagrees_or_cannot_be_read(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    _live(monkeypatch, _killing(_sunrise_caller([]), after=3))
    with pytest.raises(Killed):
        rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(tmp_path / "f.json")])
    capsys.readouterr()
    ckpt = str(tmp_path / "f.json") + rf.CHECKPOINT_SUFFIX
    head = _ckpt_lines(ckpt)[0]
    monkeypatch.setattr(rf, "_caller", lambda *a: pytest.fail("a refused resume makes no call"))
    for ledger, needle in (([f"{head['run_id']}:1", f"{head['run_id']}:2"], "disagree"),
                           ([f"{head['run_id']}:{n}" for n in (1, 2, 3, 4)], "not in the checkpoint"),
                           (None, "cannot read")):
        monkeypatch.setattr(rf, "_ledger_calls", lambda run_id, v=ledger: v)
        assert rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--resume", ckpt]) == 2
        assert needle in json.loads(capsys.readouterr().out)["refused"]


def test_reconcile_counts_billed_replies_only_and_a_cut_last_line_is_dropped(tmp_path):
    calls = [{"call_id": "r:1", "outcome": "vote"}, {"call_id": "r:2", "outcome": "caller_error"},
             {"call_id": "r:3", "outcome": "unusable"}, {"call_id": "r:4", "outcome": "identity_mismatch"}]
    assert rf.reconcile(calls, ["r:1", "r:3", "r:4"]) is None
    assert "disagree" in rf.reconcile(calls, ["r:1", "r:3", "r:4", "r:4"])
    p = tmp_path / "c.jsonl"
    head = {"type": "run", "kind": rf.CHECKPOINT_KIND, "run_id": "r"}
    p.write_text(json.dumps(head) + "\n" + json.dumps({"type": "call", "call_id": "r:1"}) + '\n{"type": "ca',
                 encoding="utf-8")
    assert rf.read_checkpoint(str(p)) == (head, [{"type": "call", "call_id": "r:1"}])
    p.write_text(json.dumps(head) + '\n{"type": "ca\n' + json.dumps({"type": "call", "call_id": "r:1"}) + "\n",
                 encoding="utf-8")
    with pytest.raises(rf.FireRefused, match="line 2 is not JSON"):
        rf.read_checkpoint(str(p))
    p.write_text(json.dumps(head) + "\n" + json.dumps({"type": "call", "call_id": "other:1"}) + "\n",
                 encoding="utf-8")
    with pytest.raises(rf.FireRefused, match="one run's"):
        rf.read_checkpoint(str(p))


def test_the_checkpoint_spend_counts_against_the_cap_on_resume():
    items = [rf.item_of(e) for e in _lines()]
    inst = rf.instrument()
    lines = []
    whole = rf.fire(items, inst, cap=1.0, run_id="r", caller=_sunrise_caller([]), log=lambda *a: None,
                    checkpoint=lines.append)
    with pytest.raises(Killed):
        rf.fire(items, inst, cap=1.0, run_id="r", caller=_killing(_sunrise_caller([]), 4), log=lambda *a: None,
                checkpoint=(cut := []).append)
    res = rf.fire(items, inst, cap=1.0, run_id="r", caller=_sunrise_caller([]), log=lambda *a: None, prior=cut)
    assert res["votes"] == whole["votes"] and res["charged_usd"] == whole["charged_usd"]
    assert res["calls"] == whole["calls"] and len(lines) == whole["calls"]
    # a prior spend that leaves less than the next call's worst case: no call, the spend is the checkpoint's
    user, _group = r1.calls(items, inst)[1]
    worst = r1.worst_call_cost(inst, user, 1)
    heavy = [{**c, "charged_usd": round(0.5 - worst / 2, 6) / len(cut)} for c in cut]
    res = rf.fire(items, inst, cap=0.5, run_id="r", log=lambda *a: None, prior=heavy,
                  caller=lambda *a: pytest.fail("the carried spend leaves no room for a call"))
    assert res["calls"] == 4 and res["spent_usd"] == pytest.approx(0.5 - worst / 2, abs=1e-5)
    assert res["stopped"].startswith("cap 0.5 USD")


def test_the_time_estimate_sizes_the_exec_timeout():
    est = rf.estimate(100, {"mean_latency_s": 24.0})
    assert est["seconds"] == 2400 and est["suggested_ecsx_script_timeout"] == 3600 and est["over_exec_cap"] is False
    big = rf.estimate(354, {"mean_latency_s": 24.0})
    assert big["seconds"] == 8496 and big["suggested_ecsx_script_timeout"] == rf.ECSX_SCRIPT_TIMEOUT_MAX == 10800
    assert big["over_exec_cap"] is True and "--resume" in big["note"]
    assert rf.estimate(10, None)["seconds"] is None and rf.estimate(10, {"mean_latency_s": None})["seconds"] is None
    assert rf.estimate(1, {"mean_latency_s": 0.2})["suggested_ecsx_script_timeout"] == 1


def test_dry_run_prints_the_estimate_from_the_measured_latency_and_the_remaining_calls(tmp_path, monkeypatch, capsys):
    elig, man = _files(tmp_path)
    measured = tmp_path / "m.json"
    measured.write_text(json.dumps({"kind": "r1_fire", "mode": "live", "stopped": None, "calls": 6, "run_id": "m" * 36,
                                    "charged_usd": 0.0024, "mean_prompt_chars": 4000.0, "mean_latency_s": 24.0,
                                    "instrument": rf.instrument().id,
                                    "prompt_version": rf.instrument().prompt_version.tag}), encoding="utf-8")
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    assert rf.main(["--eligible", elig, "--manifest", man, "--measured", str(measured)]) == 0
    rep = json.loads(capsys.readouterr().out)
    calls = rep["plan"]["instruments"][0]["calls"]
    assert rep["measured"]["mean_latency_s"] == 24.0 and rep["estimate"]["calls"] == calls
    assert rep["estimate"]["seconds"] == calls * 24 and rep["estimate"]["suggested_ecsx_script_timeout"] == min(
        math.ceil(calls * 24 * 1.5), 10800)
    _live(monkeypatch, _killing(_sunrise_caller([]), after=3))
    with pytest.raises(Killed):
        rf.main(["--eligible", elig, "--manifest", man, "--live", "--confirm-spend", "--out", str(tmp_path / "f.json")])
    capsys.readouterr()
    assert rf.main(["--eligible", elig, "--manifest", man, "--measured", str(measured),
                    "--resume", str(tmp_path / "f.json") + rf.CHECKPOINT_SUFFIX]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["estimate"]["calls"] == rep["resume"]["remaining_calls"] == calls - 3
    assert rep["resume"]["checkpoint_calls"] == 3 and rep["mode"] == "dry_run"
