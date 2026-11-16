"""AA-1048 (S282 lane l4-v6) — the two-step instrument v6: the pinned v6c / v6j prompt tags (v6c's reply schema in
its tag), the pinned pre-registration files (sha256 per step set, same bars as v5), the claim-level hold-out of the
false positives v6 was shaped on, the closed v5 step set and the one-gated-run rule for v6. Every model call is a fake;
every text, place and hash here is invented (the registered hold-out prefixes are read from the committed file).
S282 lane l9-v7: v6 is closed too (its gated run c5983e03 was no_go) and v7 is the default; the live paths of v6 below
run on a reopened copy of the v6 step set (`_setup`), so the hold-out logic stays covered."""
import dataclasses
import hashlib
import json
import types

import pytest

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from services.ground_truth import two_step as ts

SOURCE = "Day 1: Arrive in Pellow. Dinner at the inn.\nDay 2: A walk around Quarne lake."
FIELD = "Two days at Quarne lake.\nA walk around Quarne lake at sunrise."
CAND = "gpt-6-luna-openai@openai-direct"
V6C_TAG = "aa1048-r1-v6c@711ef584d7a0"
V6J_TAG = "aa1048-r1-v6j@21f0ea6956df"


def _h(n: int) -> str:
    return hashlib.sha256(f"invented-two-step-v6-{n}".encode()).hexdigest()


def _pi(n, group, claim_hash=None, piece="b"):
    item = r1.Item(claim_hash or _h(n), "t2", "fact.embellish", "violation" if group == "tp" else "ok",
                   "representative", piece * 64, "itineraries", "A walk around Quarne lake at sunrise.", FIELD,
                   SOURCE, (), "train")
    return ev.PoolItem(item, group)


def _resp(payload, cost=0.0002):
    return types.SimpleNamespace(content=json.dumps(payload), model_used="gpt-6-luna-openai",
                                 connection_key="openai-direct", satellite_account=None, cost_usd=cost,
                                 input_tokens=900, output_tokens=40, provider="openai", stop_reason="stop",
                                 fallback_used=False)


# ── prompt pinning ────────────────────────────────────────────────────────────────────────────────────────────────


def test_v6_step_prompts_are_pinned_and_the_older_tags_are_unchanged():
    assert pr.get("v6c").tag == V6C_TAG and pr.get("v6j").tag == V6J_TAG
    assert pr.get("v5c").tag == "aa1048-r1-v5c@50704a977e52" and pr.get("v5j").tag == "aa1048-r1-v5j@0c3529d50a55"
    assert pr.get("v6c").step == "classify" and pr.get("v6j").step == "judge"
    # v6 was shaped on v5's gated run as well (its false positives are held out claim by claim)
    assert set(pr.get("v6c").shaped_on) == {"3018008d", "33835b87", "2977dd78", "9b0d46f8"} == set(
        pr.get("v6j").shaped_on)
    assert pr.get("v6c").shaped_on_pools == (pr.S279_POOL,) == pr.get("v6j").shaped_on_pools


def test_the_v6c_reply_schema_is_part_of_its_tag():
    v6c = pr.get("v6c")
    assert json.dumps(pr.V6C_SCHEMA, sort_keys=True) in v6c.templates
    changed = dataclasses.replace(v6c, templates=v6c.templates[:-1] + ("{}",))
    assert changed.tag != v6c.tag
    props = list(pr.V6C_SCHEMA["schema"]["properties"])
    assert props.index("set_aside") < props.index("kind")                       # what it sets aside comes first
    insts = ts.instruments(CAND, "v6")
    assert ts.schema_of(insts["classify"]) is pr.V6C_SCHEMA
    assert ts.schema_of(insts["judge"]) is r1.ENTAILMENT_SCHEMA
    assert ts.schema_of(ts.instruments(CAND, "v5")["classify"]) is ts.CLASSIFY_SCHEMA


def test_v6_texts_set_aside_before_the_lookup_and_keep_the_recall_guard():
    c, j = pr.V6C_SYSTEM, pr.V6J_SYSTEM
    assert c.index("Set aside") < c.index("Look each trip specific up")
    assert j.index("Is it about what THIS trip gives") < j.index("A trip specific is supported")
    for text in (c, j):
        for word in ("background about a place, people, craft", "the writer's reading", "names who the text speaks",
                     "never set aside", "pace, effort, comfort, safety", "other well-known name"):
            assert word in text
    assert "answer new_claim: the second reader decides" in c and "'set_aside'" in c
    for invented in ("Halvik", "Orsa", "Drevna", "Kettle Ford", "Lusk", "Tasska", "Ilbern", "Quell"):
        assert invented in c


def test_step_one_user_prompt_is_the_v2_one_for_either_set():
    item = _pi(1, "tp").item
    assert ts.step1_user(item, "v6c") == pr.v2_entailment_user(item) == ts.step1_user(item, "v5c")


# ── pre-registration files ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("steps", ["v5", "v6"])
def test_each_step_set_pins_the_sha256_of_its_committed_preregistration(steps):
    sset = ts.STEP_SETS[steps]
    assert ts.file_sha256(sset.preregistration) == sset.prereg_sha256
    prereg = ts.load_preregistration(sset.preregistration)
    insts = ts.instruments(CAND, steps)
    assert prereg["prompt_versions"] == {s: insts[s].prompt_version.tag for s in ts.STEPS}
    assert sorted(prereg["shaped_on"]) == ts.shaped_on(insts)


def test_v6_registers_the_same_bars_as_v5_and_a_new_id():
    v5 = ts.load_preregistration(ts.STEP_SETS["v5"].preregistration)
    v6 = ts.load_preregistration(ts.STEP_SETS["v6"].preregistration)
    assert v6["bars"] == v5["bars"] and v6["bars_settings"] == v5["bars_settings"]
    assert v6["id"] == "aa1048-two-step-v6" != v5["id"] and v6["votes"] == v5["votes"] == 3
    assert v6["candidate"] == v5["candidate"] and v6["stage"] == r1.STAGE
    assert ts.STEP_SETS["v6"].closed and "c5983e03" in ts.STEP_SETS["v6"].closed       # S282: closed after its run


def test_v6_holds_out_the_false_positives_of_the_v5_gated_run():
    ho = ts.load_preregistration(ts.STEP_SETS["v6"].preregistration)["held_out"]
    assert ho["run"] == "9b0d46f8" and ho["run"] in pr.get("v6c").shaped_on and ho["pool"] == pr.FRESH_POOL
    assert len(ho["claims"]) == len(set(ho["claims"])) == 25 == sum(ho["by_group"].values())
    assert set(ho["by_group"]) <= set(ev.FP_SAMPLED)                              # false positives only
    assert all(len(c) == 16 and int(c, 16) >= 0 for c in ho["claims"])
    assert "without reading" in ho["tp_side"]


# ── claim-level hold-out ──────────────────────────────────────────────────────────────────────────────────────────

HOLD = {"run": "9b0d46f8-aaaa", "pool": "c" * 16, "claims": [_h(2)[:16], _h(3)[:16]], "tp_side": "invented"}


def test_held_out_drops_the_listed_claims_on_the_registered_pool_only():
    pool = [_pi(1, "tp"), _pi(2, "fp_paraphrase"), _pi(3, "fp_background"), _pi(4, "fp_paraphrase")]
    kept, rec = ts.held_out({"held_out": HOLD}, pool, "c" * 16)
    assert [p.item.claim_hash for p in kept] == [_h(1), _h(4)]
    assert rec["applies"] and rec["dropped"] == 2 and rec["not_in_pool"] == 0 and rec["run"] == "9b0d46f8"
    assert rec["by_group"] == {"fp_background": 1, "fp_paraphrase": 1} and ts.covered_runs(rec) == ["9b0d46f8"]
    kept, rec = ts.held_out({"held_out": HOLD}, pool, "d" * 16)                  # another pool: nothing dropped
    assert kept == pool and not rec["applies"] and ts.covered_runs(rec) == []
    kept, rec = ts.held_out({"held_out": HOLD}, pool[:2], "c" * 16)              # a listed claim is missing
    assert rec["not_in_pool"] == 1 and ts.covered_runs(rec) == []
    assert ts.held_out({}, pool, "c" * 16) == (pool, None)
    with pytest.raises(ts.TwoStepRefused, match="held_out"):
        ts.held_out({"held_out": {**HOLD, "claims": ["abc"]}}, pool, "c" * 16)


def test_fresh_check_counts_a_held_out_run_as_kept_out_and_refuses_a_partial_hold_out():
    pool = [_pi(1, "tp", piece="c")]
    text = "pool text"
    shaped = ["2977dd78", "3018008d", "33835b87", "9b0d46f8"]
    manifest = {"pool_sha256": ts.pool_sha256(text), "excluded_runs": shaped[:3], "excluded_pieces": [],
                "excluded_pools": [pr.S279_POOL], "split": "train"}
    bare = ts.fresh_check(manifest, text, pool, shaped, [pr.S279_POOL])
    assert bare["missing"] == ["9b0d46f8"] and "9b0d46f8" in ts.oos_refusal(bare)
    held = ts.fresh_check(manifest, text, pool, shaped, [pr.S279_POOL], covered=["9b0d46f8"])
    assert held["missing"] == [] and held["covered_by_held_out"] == ["9b0d46f8"] and ts.oos_refusal(held) is None
    partial = {**held, "held_out": {"applies": True, "not_in_pool": 2}}
    assert "held-out claim(s)" in ts.oos_refusal(partial)


# ── CLI: v5 closed, one gated run of v6 ───────────────────────────────────────────────────────────────────────────


def _setup(tmp_path, monkeypatch, prior, *, hold=True):
    """An invented fresh pool registered as v6's hold-out pool (a copy of the v6 registration names it)."""
    rows = [_pi(n, "tp", piece="c") for n in range(3)] + [_pi(100 + n, "fp_paraphrase", piece="d")
                                                          for n in range(5)]
    path = tmp_path / "pool.jsonl"
    path.write_text("".join(json.dumps(p.line()) + "\n" for p in rows), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    manifest = tmp_path / "fresh.json"
    manifest.write_text(json.dumps({"pool_sha256": ts.pool_sha256(text),
                                    "excluded_runs": ["3018008d", "33835b87", "2977dd78"],
                                    "excluded_pieces": ["e" * 16], "excluded_pools": [pr.S279_POOL],
                                    "split": "train", "step1_gold": {}}), encoding="utf-8")
    prereg = ts.load_preregistration(ts.STEP_SETS["v6"].preregistration)
    if hold:
        prereg["held_out"] = {**prereg["held_out"], "pool": ts.pool_sha256(text)[:16],
                              "claims": [_h(100)[:16], _h(101)[:16]], "by_group": {"fp_paraphrase": 2}}
    reg = tmp_path / "v6.json"
    reg.write_text(json.dumps(prereg), encoding="utf-8")
    # v6 is closed since its gated run (S282); a reopened copy keeps its live paths (hold-out, stop rule) tested
    monkeypatch.setitem(ts.STEP_SETS, "v6", dataclasses.replace(ts.STEP_SETS["v6"], preregistration=reg,
                                                                prereg_sha256=ts.file_sha256(reg), closed=""))
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_probe", lambda since: (lambda: 0.0))
    monkeypatch.setattr(ts, "_prior_runs", lambda: prior)
    calls = []

    def caller(inst, system, user, schema):
        calls.append((inst.prompt, schema["name"]))
        return _resp({"set_aside": ["background"], "new_specifics": [], "kind": "restatement", "why": "x"})
    monkeypatch.setattr(ts, "_caller", caller)
    from shared.llm_client import call_log

    rows_logged = []
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows_logged.append(kw))
    return str(path), str(manifest), calls, rows_logged


def _prior(run, kind, prereg, pv, pool="f" * 16):
    return {"run_id": run * 8 + "-x", "prereg": prereg, "pool": pool, "run_kind": kind, "prompt_version": pv,
            "first_at": "2026-11-16T01:00:00+00:00", "calls": 3}


V5_GATED = _prior("9", "full", "aa1048-two-step-v5", "aa1048-r1-v5c@50704a977e52")


def test_v5_is_closed_for_every_live_run_but_its_dry_run_still_reports(tmp_path, monkeypatch, capsys):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [V5_GATED])
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    for flags in (["--trial", "2"], ["--limit", "6"]):
        assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v5", "--live", "--confirm-spend",
                        *flags]) == 2
        assert "step set v5 is closed" in json.loads(capsys.readouterr().out)["refused"]
    assert calls == []
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v5", "--limit", "6"])
    rep = json.loads(capsys.readouterr().out)
    assert rc in (0, 3) and rep["steps"] == "v5" and rep["mode"] == "dry_run"
    assert rep["prompt_versions"]["classify"] == "aa1048-r1-v5c@50704a977e52" and rep["preregistration"]["matches"]


def test_the_first_gated_v6_run_runs_after_the_v5_gated_run_and_drops_the_held_out_claims(tmp_path, monkeypatch,
                                                                                            capsys):
    pool, manifest, calls, rows = _setup(tmp_path, monkeypatch, [V5_GATED])
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--limit", "6", "--live",
                  "--confirm-spend"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["steps"] == "v6" and rep["run_kind"] == "full" and rep["stop_rule"]["refused"] is None
    hold = rep["out_of_sample"]["held_out"]
    assert hold["dropped"] == 2 and rep["out_of_sample"]["covered_by_held_out"] == ["9b0d46f8"]
    assert rep["subset"]["by_group"] == {"fp_paraphrase": 3, "tp": 3}            # 5 false positives - 2 held out
    assert {c for c, _ in calls} == {"v6c"} and {n for _, n in calls} == {"claim_restatement_v6"}
    assert {r["quality_signal"]["prereg"] for r in rows} == {"aa1048-two-step-v6"}
    assert rep["preregistration"]["sha_matches"] is True


def test_a_second_gated_v6_run_is_refused(tmp_path, monkeypatch, capsys):
    v6_gated = _prior("a", "full", "aa1048-two-step-v6", V6C_TAG, pool="e" * 16)
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [V5_GATED, v6_gated])
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--limit", "6", "--live",
                  "--confirm-spend"]) == 2
    assert "one gated run" in json.loads(capsys.readouterr().out)["refused"] and calls == []
    # a trial of v6 may still repeat
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--trial", "1", "--live",
                  "--confirm-spend"]) == 0
    assert json.loads(capsys.readouterr().out)["run_kind"] == "trial"


def test_a_live_v6_run_refuses_a_changed_registration_file(tmp_path, monkeypatch, capsys):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [])
    monkeypatch.setitem(ts.STEP_SETS, "v6", dataclasses.replace(ts.STEP_SETS["v6"], prereg_sha256="0" * 64))
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--trial", "1", "--live",
                  "--confirm-spend"]) == 2
    assert "changed after it was pinned" in json.loads(capsys.readouterr().out)["refused"] and calls == []


def test_a_live_v6_run_on_a_pool_without_its_hold_out_needs_the_run_excluded(tmp_path, monkeypatch, capsys):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [], hold=False)      # held_out names another pool
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--trial", "1", "--live",
                  "--confirm-spend"]) == 2
    assert "9b0d46f8" in json.loads(capsys.readouterr().out)["refused"] and calls == []
