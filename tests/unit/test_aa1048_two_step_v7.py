"""AA-1048 (S282 lane l9-v7) — the two-step instrument v7: the pinned v7c / v7j prompt tags (both reply schemas in
their tags), the pinned pre-registration (same bars as v5 and v6, no claim-level hold-out: a new fresh pool), the
closed v5 and v6 step sets, the one-gated-run rule for v7, the train-split shaping pool that a train-split pool does not
keep out, a v7j reply that clears a sentence, and fresh_pool --exclude-run reading a two-step run report as it is (votes
nested per step). Every model call is a fake; every text, place and hash here is invented."""
import dataclasses
import hashlib
import json
import types

import pytest

from services.ground_truth import fresh_pool as fp
from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from services.ground_truth import two_step as ts

SOURCE = ("Day 1: Arrive in Pellow. Dinner at the inn.\nDay 2: The Vessa weaving hall, its walls covered in "
          "paintings.\nPlace background: the hall has two courtyards.")
FIELD = "Two days in Pellow.\nThe painted walls of the Vessa weaving hall.\nBack to Pellow."
CLAIM = "The painted walls of the Vessa weaving hall."
CAND = "gpt-6-luna-openai@openai-direct"
V7C_TAG = "aa1048-r1-v7c@0fb75e310375"
V7J_TAG = "aa1048-r1-v7j@5a91a98e9136"
V6C_TAG = "aa1048-r1-v6c@711ef584d7a0"
SHAPED = ["2977dd78", "3018008d", "33835b87", "9b0d46f8", "c5983e03"]


def _h(n: int) -> str:
    return hashlib.sha256(f"invented-two-step-v7-{n}".encode()).hexdigest()


def _pi(n, group, piece="b"):
    item = r1.Item(_h(n), "t2", "fact.embellish", "violation" if group == "tp" else "ok", "representative",
                   piece * 64, "itineraries", CLAIM, FIELD, SOURCE, (), "train")
    return ev.PoolItem(item, group)


def _resp(payload, cost=0.0002):
    return types.SimpleNamespace(content=json.dumps(payload), model_used="gpt-6-luna-openai",
                                 connection_key="openai-direct", satellite_account=None, cost_usd=cost,
                                 input_tokens=900, output_tokens=40, provider="openai", stop_reason="stop",
                                 fallback_used=False)


# ── prompt pinning ────────────────────────────────────────────────────────────────────────────────────────────────


def test_v7_step_prompts_are_pinned_and_the_older_tags_are_unchanged():
    assert pr.get("v7c").tag == V7C_TAG and pr.get("v7j").tag == V7J_TAG
    assert pr.get("v6c").tag == V6C_TAG and pr.get("v6j").tag == "aa1048-r1-v6j@21f0ea6956df"
    assert pr.get("v5c").tag == "aa1048-r1-v5c@50704a977e52" and pr.get("v5j").tag == "aa1048-r1-v5j@0c3529d50a55"
    assert pr.get("v7c").step == "classify" and pr.get("v7j").step == "judge"
    # v7 was shaped on v6's gated run on the S281 fresh pool: the run and the whole pool are in-sample for it
    assert sorted(pr.get("v7c").shaped_on) == SHAPED == sorted(pr.get("v7j").shaped_on)
    assert pr.get("v7c").shaped_on_pools == (pr.S279_POOL, pr.FRESH_POOL) == pr.get("v7j").shaped_on_pools
    assert pr.FRESH_POOL in pr.TRAIN_SPLIT_POOLS and pr.S279_POOL not in pr.TRAIN_SPLIT_POOLS


@pytest.mark.parametrize("version,schema", [("v7c", "V7C_SCHEMA"), ("v7j", "V7J_SCHEMA")])
def test_each_v7_reply_schema_is_part_of_its_tag(version, schema):
    pv, sch = pr.get(version), getattr(pr, schema)
    assert json.dumps(sch, sort_keys=True) in pv.templates
    assert dataclasses.replace(pv, templates=pv.templates[:-1] + ("{}",)).tag != pv.tag


def test_the_v7_schemas_ask_for_the_source_wording_before_the_decision():
    c = list(pr.V7C_SCHEMA["schema"]["properties"])
    assert c.index("set_aside") < c.index("checked") < c.index("new_specifics") < c.index("kind")
    row = pr.V7C_SCHEMA["schema"]["properties"]["checked"]["items"]
    assert row["required"] == ["specific", "source_wording", "same_fact"]
    j = list(pr.V7J_SCHEMA["schema"]["properties"])
    assert j.index("checks") < j.index("verdict") and {"quote", "why"} <= set(j)
    finding = pr.V7J_SCHEMA["schema"]["properties"]["checks"]["items"]["properties"]["finding"]
    assert finding["enum"] == list(pr.V7J_FINDINGS) == ["not_trip", "same_fact", "one_step", "not_given", "against"]
    insts = ts.instruments(CAND)
    assert ts.schema_of(insts["classify"]) is pr.V7C_SCHEMA and ts.schema_of(insts["judge"]) is pr.V7J_SCHEMA
    assert ts.schema_of(ts.instruments(CAND, "v6")["judge"]) is r1.ENTAILMENT_SCHEMA


def test_v7_texts_look_up_facts_not_words_and_keep_the_recall_guard():
    c, j = pr.V7C_SYSTEM, pr.V7J_SYSTEM
    assert c.index("Set aside") < c.index("Look for the FACT, not the words") < c.index("Answer 'restatement'")
    for text in (c, j):
        for phrase in ("the same thing under another name", "the same number or measure written another way",
                       "the same quality in other words", "place background gives for a place the trip visits",
                       "These are NOT the same fact", "NEW quality, mood, light, view, sound, measure, group",
                       "stretched to other days, to more nights", "never set aside"):
            assert phrase in text
    # step 2 can clear: a row per listed specific, and the verdict follows the rows
    assert "clear it when the source supports it" in j
    assert "'supported' when every row is not_trip, same_fact or one_step" in j
    assert "'unsupported' when at least one row is not_given or against" in j
    for finding in pr.V7J_FINDINGS:
        assert f"- {finding}:" in j
    assert "answer new_claim: the second reader decides" in c and "'checked'" in c
    for invented in ("Vessa", "Pellow", "Brannock", "Mirre", "Saint Avel", "Cobbe", "Corran", "Halvik", "Tasska"):
        assert invented in c


def test_step_two_user_prompt_is_unchanged_for_v7():
    item = _pi(1, "tp").item
    assert ts.step1_user(item, "v7c") == pr.v2_entailment_user(item)
    assert ts.step2_user(item, ["painted walls"]) == pr.v5j_user(item, ["painted walls"])


# ── pre-registration ──────────────────────────────────────────────────────────────────────────────────────────────


def test_v7_pins_the_sha256_of_its_committed_preregistration_and_is_the_default():
    sset = ts.STEP_SETS["v7"]
    assert ts.DEFAULT_STEP_SET == "v7" and ts.PREREGISTRATION == sset.preregistration and not sset.closed
    assert ts.file_sha256(sset.preregistration) == sset.prereg_sha256
    prereg = ts.load_preregistration(sset.preregistration)
    insts = ts.instruments(CAND)
    assert prereg["prompt_versions"] == {"classify": V7C_TAG, "judge": V7J_TAG}
    assert sorted(prereg["shaped_on"]) == ts.shaped_on(insts) == SHAPED
    assert sorted(prereg["shaped_on_pools"]) == ts.shaped_on_pools(insts)


def test_v7_registers_the_same_bars_a_new_id_and_no_claim_level_hold_out():
    v6 = ts.load_preregistration(ts.STEP_SETS["v6"].preregistration)
    v7 = ts.load_preregistration(ts.STEP_SETS["v7"].preregistration)
    assert v7["bars"] == v6["bars"] == {"fp_dropped_min_pct": 60.0, "tp_kept_min_pct": 90.0,
                                        "route_tp_min_pct": 95.0, "judge_tp_kept_min_pct": 95.0,
                                        "judge_fp_dropped_min_pct": 40.0}
    assert v7["bars_settings"] == v6["bars_settings"]
    assert v7["id"] == "aa1048-two-step-v7" != v6["id"] and v7["votes"] == v6["votes"] == 3
    assert v7["candidate"] == v6["candidate"] == CAND and v7["stage"] == r1.STAGE and v7["role"] == r1.ROLE
    assert v7["held_out"] is None and set(v7["hypotheses"]) == {"H1", "H2", "H3", "H4"}
    assert ts.held_out(v7, [_pi(1, "tp")], "c" * 16) == ([_pi(1, "tp")], None)


@pytest.mark.parametrize("steps", ["v5", "v6"])
def test_v5_and_v6_are_closed_step_sets(steps):
    assert ts.STEP_SETS[steps].closed and "no_go" in ts.STEP_SETS[steps].closed


# ── CLI: v6 closed, one gated run of v7 ───────────────────────────────────────────────────────────────────────────


def _setup(tmp_path, monkeypatch, prior, **manifest_extra):
    rows = [_pi(n, "tp", piece="c") for n in range(3)] + [_pi(100 + n, "fp_paraphrase", piece="d")
                                                          for n in range(3)]
    path = tmp_path / "pool.jsonl"
    path.write_text("".join(json.dumps(p.line()) + "\n" for p in rows), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    manifest = tmp_path / "fresh.json"
    manifest.write_text(json.dumps({"pool_sha256": ts.pool_sha256(text), "excluded_runs": SHAPED,
                                    "excluded_pieces": ["e" * 16], "excluded_pools": [pr.S279_POOL, pr.FRESH_POOL],
                                    "split": "train", "step1_gold": {}, **manifest_extra}), encoding="utf-8")
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_probe", lambda since: (lambda: 0.0))
    monkeypatch.setattr(ts, "_prior_runs", lambda: prior)
    calls = []

    def caller(inst, system, user, schema):
        calls.append((inst.prompt, schema["name"]))
        if inst.prompt == "v7c":
            return _resp({"set_aside": [], "checked": [{"specific": "painted walls", "source_wording": "paintings",
                                                        "same_fact": False}],
                          "new_specifics": ["painted walls"], "kind": "new_claim", "why": "x"})
        return _resp({"checks": [{"specific": "painted walls", "source_wording": "walls covered in paintings",
                                  "finding": "same_fact"}], "verdict": "supported", "quote": "paintings", "why": "x"})
    monkeypatch.setattr(ts, "_caller", caller)
    from shared.llm_client import call_log

    logged = []
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: logged.append(kw))
    return str(path), str(manifest), calls, logged


def _prior(run, kind, prereg, pv, pool="f" * 16):
    return {"run_id": run * 8 + "-x", "prereg": prereg, "pool": pool, "run_kind": kind, "prompt_version": pv,
            "first_at": "2026-11-16T01:00:00+00:00", "calls": 3}


V5_GATED = _prior("9", "full", "aa1048-two-step-v5", "aa1048-r1-v5c@50704a977e52")
V6_GATED = _prior("c", "full", "aa1048-two-step-v6", V6C_TAG, pool=pr.FRESH_POOL)
LIVE = ["--live", "--confirm-spend"]


def test_v6_is_closed_for_every_live_run_but_its_dry_run_still_reports(tmp_path, monkeypatch, capsys):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [V5_GATED, V6_GATED])
    for flags in (["--trial", "2"], ["--limit", "6"]):
        assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", *LIVE, *flags]) == 2
        assert "step set v6 is closed" in json.loads(capsys.readouterr().out)["refused"]
    assert calls == []
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--steps", "v6", "--limit", "6"])
    rep = json.loads(capsys.readouterr().out)
    assert rc in (0, 3) and rep["steps"] == "v6" and rep["mode"] == "dry_run"
    assert rep["prompt_versions"]["classify"] == V6C_TAG and rep["preregistration"]["sha_matches"] is True


def test_the_first_gated_v7_run_runs_after_the_v5_and_v6_gated_runs_and_step_two_can_clear(tmp_path, monkeypatch,
                                                                                           capsys):
    pool, manifest, calls, logged = _setup(tmp_path, monkeypatch, [V5_GATED, V6_GATED])
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--limit", "6", *LIVE])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["steps"] == "v7" and rep["run_kind"] == "full" and rep["stop_rule"]["refused"] is None
    assert rep["preregistration"]["matches"] is True and rep["preregistration"]["sha_matches"] is True
    assert rep["out_of_sample"]["missing"] == [] and rep["out_of_sample"]["pool_side"] == "excluded"
    assert rep["out_of_sample"]["held_out"] is None and rep["subset"]["by_group"] == {"fp_paraphrase": 3, "tp": 3}
    assert {c for c, _ in calls} == {"v7c", "v7j"}
    assert {n for _, n in calls} == {"claim_restatement_v7", "claim_entailment_v7"}
    assert {r["quality_signal"]["prereg"] for r in logged} == {"aa1048-two-step-v7"}
    assert {r["prompt_version"] for r in logged} == {V7C_TAG, V7J_TAG}
    # step 1 sent every claim on, step 2 cleared every one of them: a supported row clears
    steps = rep["score"]["steps"]
    assert steps["step2"] == {"fp_paraphrase": {"cleared": 3}, "tp": {"cleared": 3}}
    assert steps["judge_fp_dropped_share"] == 1.0 and steps["passes"]["judge_tp_kept"] is False


def test_a_second_gated_v7_run_is_refused_and_a_trial_may_repeat(tmp_path, monkeypatch, capsys):
    v7_gated = _prior("d", "full", "aa1048-two-step-v7", V7C_TAG, pool="e" * 16)
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [V5_GATED, V6_GATED, v7_gated])
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--limit", "6", *LIVE]) == 2
    assert "one gated run" in json.loads(capsys.readouterr().out)["refused"] and calls == []
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--trial", "1", *LIVE]) == 0
    assert json.loads(capsys.readouterr().out)["run_kind"] == "trial"


def test_a_live_v7_run_refuses_a_changed_registration_file(tmp_path, monkeypatch, capsys):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [])
    monkeypatch.setitem(ts.STEP_SETS, "v7", dataclasses.replace(ts.STEP_SETS["v7"], prereg_sha256="0" * 64))
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--trial", "1", *LIVE]) == 2
    assert "changed after it was pinned" in json.loads(capsys.readouterr().out)["refused"] and calls == []


@pytest.mark.parametrize("extra,needle", [
    ({"excluded_runs": SHAPED[:4]}, "c5983e03"),                             # v6's gated run not excluded
    ({"excluded_pools": [pr.S279_POOL]}, pr.FRESH_POOL),                      # train pool: the split does not cover it
    ({"excluded_pools": [pr.FRESH_POOL], "split": "held_out"}, pr.S279_POOL),
])
def test_a_live_v7_run_needs_a_manifest_that_excludes_every_shaping_run_and_pool(tmp_path, monkeypatch, capsys,
                                                                                 extra, needle):
    pool, manifest, calls, _ = _setup(tmp_path, monkeypatch, [], **extra)
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--trial", "1", *LIVE]) == 2
    assert needle in json.loads(capsys.readouterr().out)["refused"] and calls == []


def test_a_train_split_pool_still_keeps_the_held_out_s279_pool_out():
    pool = [_pi(1, "tp", piece="c")]
    text = "pool text"
    manifest = {"pool_sha256": ts.pool_sha256(text), "excluded_runs": SHAPED, "excluded_pieces": [],
                "excluded_pools": [pr.FRESH_POOL], "split": "train"}
    oos = ts.fresh_check(manifest, text, pool, SHAPED, [pr.S279_POOL, pr.FRESH_POOL])
    assert oos["pools_missing"] == [pr.S279_POOL] and oos["pool_side"] == "train_split" and ts.oos_refusal(oos) is None


# ── fresh_pool --exclude-run reads a two-step run report as it is ─────────────────────────────────────────────────


def _two_step_report(run="c5983e03-aaaa"):
    """The shape two_step writes: votes per step, step 2 only on the claims step 1 sent on (hash prefixes, 16 hex)."""
    return json.dumps({"run_id": run, "steps": "v6",
                       "votes": {"classify": {_h(1)[:16]: [True, True, True], _h(2)[:16]: [False, False, False]},
                                 "judge": {_h(1)[:16]: [True, True, True]}}})


def test_prefixes_from_takes_the_union_of_the_step_claim_keys_of_a_two_step_report():
    text = _two_step_report()
    assert ev.prefixes_from(text) == {_h(1)[:16], _h(2)[:16]} and ev.run_of(text) == "c5983e03"
    flat = json.dumps({"run_id": "3018008d-x", "votes": {_h(3)[:16]: [True]}})
    assert ev.prefixes_from(flat) == {_h(3)[:16]}                               # a one-step report as before
    with pytest.raises(ev.EvalRefused, match="per-step"):
        ev.prefixes_from(json.dumps({"run_id": "c5983e03-x", "votes": {"classify": {}, "judge": {}}}))
    with pytest.raises(ev.EvalRefused, match=">= 8 hex"):
        ev.prefixes_from(json.dumps({"votes": {"classify": {"abc": [True]}}}))


def test_fresh_pool_eligible_keeps_out_the_pieces_of_a_two_step_run_report(tmp_path, capsys):
    train = [n for n in range(200) if r1.split_of_line({"content_sha256": _piece(n)}) == "train"][:3]
    lines = [_line(n, train[n - 1]) for n in (1, 2, 3)]
    exports = tmp_path / "claims.jsonl"
    exports.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    report = tmp_path / "ts-gated-report.json"
    report.write_text(_two_step_report(), encoding="utf-8")
    pieces, runs = fp.shaped_pieces([], [report.read_text(encoding="utf-8")], lines)
    assert pieces == {_piece(train[0]), _piece(train[1])} and runs == ["c5983e03"]
    assert fp.main(["eligible", "--export", str(exports), "--exclude-run", str(report), "--out",
                    str(tmp_path / "e.jsonl"), "--manifest", str(tmp_path / "e.json")]) == 0
    man = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    assert man["excluded_runs"] == ["c5983e03"] and man["claims"] == 1
    assert json.loads(capsys.readouterr().out)["claims"] == 1


def _piece(n: int) -> str:
    return hashlib.sha256(f"invented-v7-piece-{n}".encode()).hexdigest()


def _line(n, piece_n):
    claim = f"Sentence {n} about the Vessa weaving hall."
    return {"claim_hash": _h(n), "writer": "t2", "field": "itineraries", "claim_text": claim,
            "field_text": f"Intro line.\n{claim}\nClosing line.", "source_text": SOURCE,
            "content_sha256": _piece(piece_n), "stratum": "representative", "claim_class": "fact.embellish"}
