"""S271 W0 (AA-900, judge root cause F1-F8) — the Writing Lab eval interim: R2 spends writer money only on a result
that can count. In-memory store, fake writer and judge (no DB, no model). Invented text only (public repo).

- controls paired on (sample_id, input_sha256): release 0 on an older golden version takes this version's baseline;
- refused at $0 when controls are missing or calibration cannot be measured; post_edit items without a baseline row
  are not planned controls;
- calibrate-only and require-calibrated stop before any writer call;
- a pinned judge is in the versions and the family check; a mixed judge makes the run invalid;
- the ledger estimate, a finished run's verdict kept past the cap, hard stop 1 only on fail vs a pass control.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import eval_checks as ec
from services.writing_memory import eval_judge as ej
from services.writing_memory import r2
from tests.unit.test_aa919_r2_runner import (BASELINE_RUN, OLD_BASELINE, OLD_R2_RUN, SET_ID, FakeStore, FakeWriter,
                                             _rows, golden, make_judge, run)


def _release0_on(run_id, content_class="empty"):
    return {"platform": {"release_id": "r0", "release_no": 0, "content_class": content_class, "rule_ids": [],
                         "eval_run_id": run_id, "scope": "platform"}, "tenant": None}


# ── (1) pairing ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_release_0_on_an_older_golden_version_pairs_every_planned_item_on_this_versions_baseline():
    """RC3: release 0 still points at the baseline of the version it was made on. Its item ids differ, so the old
    item_id match paired 0 candidates. Now the current baseline is the control: every planned non-trap item is paired
    and the control is the calibration call itself (same_row, no extra judge call)."""
    items = golden()
    store = FakeStore(items, active=_release0_on(OLD_BASELINE))
    out, writer, judge = run(store, r2.R2Config(writer="a1"))
    d = store.runs[out.run_id]["details"]
    assert d["control_run_id"] == BASELINE_RUN
    assert d["control_run_stale"]["run_id"] == OLD_BASELINE and d["control_run_stale"]["replaced_by"] == BASELINE_RUN
    non_trap = [i for i in items if not i["trap"]]
    assert d["controls"]["planned"] == d["controls"]["found"] == len(non_trap)
    assert d["controls"]["paired_on"] == "sample_id+input_sha256"
    controls = _rows(store, arm="control")
    assert len(controls) == len(non_trap) and all(r["details"]["calibration"] == "same_row" for r in controls)
    assert len([c for c in judge.calls if c[0] == "control"]) == len(non_trap)      # one judge call per item
    cands = [r for r in _rows(store, arm="candidate") if r["phase"] != "probe" or not r["details"].get("injection")]
    assert all(r["details"]["paired"] for r in cands if r["item_id"] in {i["item_id"] for i in non_trap})
    assert out.verdict == "pass"


def test_an_empty_release_on_an_older_version_is_replaced_too_but_a_rules_release_is_refused_at_zero():
    store = FakeStore(golden(), active=_release0_on(OLD_R2_RUN, content_class="empty"))
    out, _, _ = run(store, r2.R2Config(writer="a1"))
    assert store.runs[out.run_id]["details"]["control_run_stale"]["replaced_by"] == BASELINE_RUN
    # a rules release's eval run on another version: its outputs belong to the old items (CIS 284: a rejudge row
    # scores a result of the same item), so no control can be stored; refused before anything is written
    store2 = FakeStore(golden(), active=_release0_on(OLD_R2_RUN, content_class="rules"))
    writer = FakeWriter(store2)
    with pytest.raises(r2.R2Refused, match="another golden set version"):
        run(store2, r2.R2Config(writer="a1"), writer=writer)
    assert store2.runs == {} and store2.results == [] and writer.calls == []


def test_a_missing_control_refuses_the_run_at_zero_dollars():
    items = golden()
    store = FakeStore(items)
    gone = [i["item_id"] for i in items if i["split"] == "tune"][2]
    base = store.control_sources

    async def control_sources(run_id, its):
        got = await base(run_id, its)
        got.pop(gone, None)
        return got
    store.control_sources = control_sources
    writer = FakeWriter(store)
    with pytest.raises(r2.R2Refused, match="controls found for 13 of 14"):
        run(store, r2.R2Config(writer="a1"), writer=writer)
    assert store.runs == {} and writer.calls == []


def test_post_edit_holdout_items_without_a_baseline_row_are_not_planned_controls():
    """Skeptic 2: derive skips a post_edit item in the baseline import, so release 0 has no row for it. Counting it as
    planned would refuse every run; it is left out of the planned count (and still written, unpaired)."""
    items = golden()
    pe = [i for i in items if i["split"] == "holdout"][-1]
    pe["post_edit"] = True
    store = FakeStore(items)
    base = store.calibration_sources

    async def calibration_sources(run_id, ids):
        got = await base(run_id, ids)
        return {k: v for k, v in got.items() if k != pe["item_id"]}
    store.calibration_sources = calibration_sources
    out, _, _ = run(store, r2.R2Config(writer="a1"))
    d = store.runs[out.run_id]["details"]
    assert d["controls"]["planned"] == d["controls"]["found"] == len([i for i in items if not i["trap"]]) - 1
    assert not _rows(store, item_id=pe["item_id"], arm="control")


# ── (2) calibrate first ───────────────────────────────────────────────────────────────────────────────────────────

def test_calibrate_only_makes_zero_writer_calls():
    store = FakeStore(golden(), active=_release0_on(OLD_BASELINE))
    out, writer, judge = run(store, r2.R2Config(writer="a1", calibrate_only=True))
    assert writer.calls == [] and not _rows(store, arm="candidate")
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("informative", "completed", "calibrate")
    assert out.judge_counts and store.runs[out.run_id]["details"]["calibration_gate"] == "calibrate_only"
    assert {c[0] for c in judge.calls} == {"control"}


def test_calibrate_only_judges_no_control_of_its_own_after_release_1():
    """A release-1 control is another text than the frozen one: calibrate-only judges the frozen texts only."""
    from tests.unit.test_aa919_r2_runner import _release1_active
    items = golden()
    store = FakeStore(items, active=_release1_active())
    out, writer, judge = run(store, r2.R2Config(writer="a1", calibrate_only=True))
    calib = r2.calibration_items(items)
    assert writer.calls == [] and len(judge.calls) == len(calib)
    assert all(r["details"]["calibration"] == "only" for r in _rows(store, arm="control"))


def test_require_calibrated_is_the_default_and_an_uncalibrated_judge_spends_no_writer_money():
    assert r2.R2Config(writer="t9_blog").require_calibrated is True
    conf, _ = r2._parse(["--writer", "t9_blog"])
    assert conf.require_calibrated and not conf.calibrate_only
    conf, _ = r2._parse(["--writer", "t9_blog", "--allow-uncalibrated"])
    assert not conf.require_calibrated
    conf, _ = r2._parse(["--writer", "t9_blog", "--calibrate-only", "--judge-model", "gpt-zz"])
    assert conf.calibrate_only and conf.judge_model == "gpt-zz"
    with pytest.raises(SystemExit):
        r2._parse(["--writer", "t9_blog", "--calibrate-only", "--allow-uncalibrated"])
    items = golden()
    wrong = [i["item_id"] for i in items if i["split"] == "tune"][:6]
    store = FakeStore(items)
    out, writer, _ = run(store, r2.R2Config(writer="a1"), judge=make_judge(wrong_on=wrong))
    assert not out.judge_counts and writer.calls == [] and out.verdict == "informative"


def test_bad_pin_and_calibrate_only_combinations_are_refused():
    for kw in ({"judge_connection": "zz-direct"}, {"judge_model": "bad key!"},
               {"calibrate_only": True, "seed_fault": "qa_trace"}, {"calibrate_only": True, "run_kind": "reverify"}):
        with pytest.raises(r2.R2Refused):
            r2.R2Config(writer="a1", **kw).validate()


# ── (3) the pinned judge ──────────────────────────────────────────────────────────────────────────────────────────

def _judge_with(models: list[str], connection=None):
    """A fake judge answering as models[i] for call i (the last one after), with an identity like judge_output's."""
    inner = make_judge()
    n = {"i": 0}

    async def j(pool, **kw):
        v = await inner(pool, **kw)
        model = models[min(n["i"], len(models) - 1)]
        n["i"] += 1
        return SimpleNamespace(**{**v.__dict__, "model": model, "model_key": model, "connection": connection,
                                  "provider_model": f"{model}-2026"})
    j.calls = inner.calls
    return j


def test_a_pinned_judge_is_in_the_run_version_and_every_rows_judge_version():
    store = FakeStore(golden())
    out, _, _ = run(store, r2.R2Config(writer="a1", judge_model="zz-judge", judge_connection="zz-direct"),
                    judge=_judge_with(["zz-judge"], connection="zz-direct"))
    row = store.runs[out.run_id]
    assert row["prompt_version"].endswith("@zz-judge") and row["prompt_version"].startswith("a1:judge-")
    assert all(r["judge_version"] == "v-test@zz-judge" for r in store.results)
    assert all(r["details"]["judge"] == {"model_key": "zz-judge", "connection": "zz-direct",
                                         "provider_model": "zz-judge-2026"} for r in store.results)
    d = row["details"]
    assert d["judge"] == {"model_key": "zz-judge", "connection": "zz-direct", "pinned": True}
    assert d["judge_identities"] == [{"model": "zz-judge", "connection": "zz-direct", "rows": len(store.results)}]
    assert out.verdict == "pass"


def test_a_mixed_judge_makes_the_run_invalid_with_an_existing_stop_reason():
    """F3 as the skeptic corrected it: the CIS 284 CHECK has no 'judge_model_mixed' stop reason, so the run stops
    with 'error' (verdict invalid) and the reason goes in details."""
    store = FakeStore(golden())
    out, writer, _ = run(store, r2.R2Config(writer="a1"), judge=_judge_with(["zz-a"] * 5 + ["zz-b"]))
    assert (out.verdict, out.stop_reason, out.stopped_phase) == ("invalid", "error", "calibrate")
    d = store.runs[out.run_id]["details"]
    assert d["error"] == "judge_model_mixed" and {x["model"] for x in d["judge_identities"]} == {"zz-a", "zz-b"}
    assert writer.calls == [] and len(store.results) == 6                    # stopped at the first mixed row
    assert out.stop_reason in ("completed", "hard_stop", "futility", "budget_cap", "probe_fail", "error")


def test_a_judge_switching_model_mid_candidates_is_invalid_too():
    items = golden()
    n_cal = len(r2.calibration_items(items))
    store = FakeStore(items)
    out, writer, _ = run(store, r2.R2Config(writer="a1"), judge=_judge_with(["zz-a"] * (n_cal + 2) + ["zz-b"]))
    assert (out.verdict, out.stop_reason) == ("invalid", "error") and len(writer.calls) == 3


MODELS = [SimpleNamespace(model_key=k, family_key=f) for k, f in (
    ("zz-claude", "anthropic"), ("zz-claude-old", "anthropic"), ("zz-gpt", "openai"), ("zz-nova", "amazon"))]


def test_pinned_family_check_covers_every_writer_rung_and_unknown_pins():
    row = {"stage": "t9_write", "model_id": "zz-claude", "fallback_model_ids": ["zz-claude-old", "zz-gpt"]}
    assert r2.pinned_conflicts(row, "zz-gpt", MODELS) == ["t9_write: zz-gpt (openai)"]     # a fallback rung counts
    assert r2.pinned_conflicts(row, "zz-nova", MODELS) == []
    assert "not in the catalog" in r2.pinned_conflicts(row, "zz-unknown", MODELS)[0]


def test_check_judge_family_checks_the_pin_not_the_route():
    rows = [{"stage": "t9_write", "model_id": "zz-claude", "fallback_model_ids": ["zz-gpt"]}]
    route = AsyncMock(return_value=[])
    with patch("shared.llm_client.catalog.list_models", AsyncMock(return_value=MODELS)), \
            patch("shared.llm_client.role_config.list_stage_configs", AsyncMock(return_value=rows)), \
            patch.object(r2.family_rule, "stage_pair_conflicts", route):
        with pytest.raises(r2.R2Refused, match="pinned judge zz-gpt"):
            asyncio.run(r2.check_judge_family("t9_blog", "zz-gpt"))
        assert asyncio.run(r2.check_judge_family("t9_blog", "zz-nova")) == MODELS
        route.assert_not_awaited()
        asyncio.run(r2.check_judge_family("t9_blog"))
        route.assert_awaited_once()


def test_run_with_pool_binds_the_pin_to_the_judge_call():
    fn = r2.judge_fn_for(r2.R2Config(writer="a1", judge_model="zz-gpt", judge_connection="zz-direct"))
    assert fn.func is ej.judge_output and fn.keywords == {"model_key": "zz-gpt", "connection": "zz-direct"}
    assert r2.judge_fn_for(r2.R2Config(writer="a1")) is ej.judge_output


def _fake_reply(model_used, connection_key=None, satellite_account=None):
    return SimpleNamespace(content=json.dumps({"verdict": "fail", "score": 3, "failure_classes": ["fact.number"],
                                               "unsupported_claims": []}),
                           model_used=model_used, cost_usd=0.002, input_tokens=10, output_tokens=5,
                           connection_key=connection_key, satellite_account=satellite_account,
                           provider_model="zz-gpt-2026-01")


def _judge_once(monkeypatch, reply, **kw):
    seen, logged = {}, []

    class FakeClient:
        def generate(self, req):
            from shared.llm_client import deployments
            seen.update(model_tier=req.model_tier, override=deployments._priority_override.get())
            return reply

    async def fake_log(pool, **k):
        logged.append(k)

    monkeypatch.setattr(ej, "LLMClient", FakeClient)
    monkeypatch.setattr(ej, "record_call_with_pool", fake_log)
    coro = ej.judge_output(None, writer="t9_blog", source_text="zz", output_text="zz", run_id="r", item_id="i",
                           arm="candidate", **kw)
    return seen, logged, coro


def test_judge_output_records_the_identity_and_refuses_another_model_or_connection(monkeypatch):
    seen, logged, coro = _judge_once(monkeypatch, _fake_reply("zz-gpt", connection_key="zz-direct"),
                                     model_key="zz-gpt", connection="zz-direct")
    v = asyncio.run(coro)
    assert (v.model_key, v.connection, v.provider_model) == ("zz-gpt", "zz-direct", "zz-gpt-2026-01")
    assert ej.identity(v) == ("zz-gpt", "zz-direct")
    assert seen["model_tier"] == "zz-gpt" and seen["override"] == {"zz-gpt": {"zz-direct": -1_000_000}}
    assert logged[0]["quality_signal"]["judge_pin"] == "zz-gpt@zz-direct"
    # another connection answered: logged (billed), then refused, never a verdict
    _seen, logged, coro = _judge_once(monkeypatch, _fake_reply("zz-gpt", satellite_account="zz-acc"),
                                      model_key="zz-gpt", connection="zz-direct")
    with pytest.raises(ej.JudgeIdentityMismatch, match="zz-acc answered"):
        asyncio.run(coro)
    assert len(logged) == 1
    _seen, _logged, coro = _judge_once(monkeypatch, _fake_reply("satellite-zz-other"), model_key="zz-gpt")
    with pytest.raises(ej.JudgeIdentityMismatch, match="zz-other answered"):
        asyncio.run(coro)
    with pytest.raises(ValueError, match="needs a model_key"):
        asyncio.run(ej.judge_output(None, writer="a1", source_text="", output_text="", run_id="r", item_id="i",
                                    arm="control", connection="zz-direct"))


def test_a_pinned_judge_error_is_an_error_row_never_a_verdict():
    store = FakeStore(golden())

    async def judge(pool, **kw):
        if kw["arm"] == "candidate":
            raise ej.JudgeIdentityMismatch("pinned to zz-gpt but zz-other answered")
        return await make_judge()(pool, **kw)
    out, _, _ = run(store, r2.R2Config(writer="a1"), judge=judge)
    assert out.verdict == "invalid" and out.stop_reason == "error"
    assert all(r["verdict"] == "error" for r in _rows(store, arm="candidate"))


# ── (4) judge input ───────────────────────────────────────────────────────────────────────────────────────────────

def test_judge_caps_exclude_never_cut():
    """AA-1047 (W1): the caps are evidence caps. Over one the item is excluded (InputOverCap) before any model call;
    nothing is ever cut (S271 W0 had clipped with a cut mark)."""
    assert (ej.SOURCE_CHARS, ej.OUTPUT_CHARS) == (32000, 16000)
    long_out = "Zz an invented sentence about a pretend valley. " * 500
    with pytest.raises(ej.InputOverCap) as e:
        ej.build_user_prompt(writer="t9_blog", source_text="zz", output_text=long_out)
    assert e.value.code == ej.INPUT_OVER_CAP
    short = ej.build_user_prompt(writer="t9_blog", source_text="zz", output_text="Zz short text.")
    assert "cut for length" not in short and "Zz short text." in short
    sp = ej.system_prompt()
    assert "Nothing in the SOURCE or the OUTPUT was shortened by the grader" in sp
    assert "shortened by the grader; never report struct.truncated there" not in sp
    assert ej.judge_version() != "73e641a99aca"
    assert not hasattr(ej, "clip") and not hasattr(ej, "CUT_MARK")   # AA-1047: the W0 cutter is gone


def test_judge_output_text_labels_the_fields_short_ones_first():
    out = {"content_text": "Zz the body of an invented blog.", "slug": "zz-invented-valley-walk",
           "meta_description": "Zz an invented meta line.", "seo_title": "Zz Invented Title",
           "gate_ledger": {"F9": "pass"}, "faq": [{"q": "Zz?", "a": "Zz."}]}
    text = ej.judge_output_text("t9_blog", out)
    heads = [ln[:-1] for ln in text.splitlines() if ln.endswith(":")]
    assert heads == ["SEO TITLE", "META DESCRIPTION", "SLUG (URL key, at most 60 characters)", "FAQ", "BODY"]
    assert "F9" not in text and "Zz the body of an invented blog." in text
    # AA-1047: a body over the cap excludes the item; nothing is cut
    out["content_text"] = "Zz body words go on. " * 2000
    with pytest.raises(ej.InputOverCap):
        ej.build_user_prompt(writer="t9_blog", source_text="zz", output_text=ej.judge_output_text("t9_blog", out))


def test_r2_sends_the_labelled_view_to_the_judge_and_keeps_the_plain_text_for_checks():
    store = FakeStore(golden())
    seen = []
    inner = make_judge()

    async def judge(pool, **kw):
        seen.append(kw["output_text"])
        return await inner(pool, **kw)
    run(store, r2.R2Config(writer="a1"), judge=judge)
    assert seen and all(t.startswith("NAME:\n") for t in seen)


# ── (5) money ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_item_estimate_reads_the_ledger_with_a_margin_and_falls_back_to_config():
    led = r2.item_estimate("t9_blog", [0.10, 0.12, 0.134, 0.15, 0.30])
    assert led["source"] == "ledger" and led["item_usd"] == pytest.approx(0.134 * cfg.R2_ESTIMATE_MARGIN)
    assert led["reserve_usd"] == pytest.approx(led["item_usd"] + 2 * cfg.JUDGE_CALL_ESTIMATE_USD)
    few = r2.item_estimate("t9_blog", [0.1, 0.2])
    assert few["source"] == "config" and few["item_usd"] == cfg.R2_ITEM_ESTIMATE_USD["t9_blog"]
    assert cfg.JUDGE_CALL_ESTIMATE_USD == 0.005


def test_the_run_reserves_the_ledger_estimate():
    store = FakeStore(golden(), spend_per_writer_call=0.2, cost_samples=[0.5] * 10)       # 0.65 + 0.01 reserved
    out, writer, _ = run(store, r2.R2Config(writer="a1", cap_usd=1.0))
    assert out.stop_reason == "budget_cap" and len(writer.calls) == 2       # 0 -> 0.2 -> 0.4; 0.4 + 0.66 > 1
    assert store.runs[out.run_id]["details"]["estimate"]["source"] == "ledger"


def test_a_finished_run_keeps_its_verdict_when_the_last_item_passes_the_cap():
    items = golden()
    store = FakeStore(items)
    last = [i for i in items if i["split"] == "holdout"][-1]["input"]["tour"]["name"]

    class W(FakeWriter):
        async def __call__(self, writer, inp, *, view, shared_inputs):
            if inp["tour"]["name"] == last:
                self.store.spend += 2.0
            return await super().__call__(writer, inp, view=view, shared_inputs=shared_inputs)
    out, _, _ = run(store, r2.R2Config(writer="a1", cap_usd=1.5), writer=W(store))
    assert (out.verdict, out.stop_reason) == ("pass", "completed")
    assert store.runs[out.run_id]["details"]["overspend_usd"] == pytest.approx(0.5)


# ── (6) hard stop 1 ───────────────────────────────────────────────────────────────────────────────────────────────

def _v(verdict, codes=()):
    return {"verdict": verdict, "failure_codes": list(codes), "qa_artefacts": [], "isolation": {"leak": False},
            "injection": []}


def test_hard_stop_1_needs_a_candidate_fail_against_a_control_pass():
    new = ["fact.number"]
    assert ec.compare_item(control=_v("pass"), candidate=_v("fail", new), counts=True)["hard_stop"] == 1
    got = ec.compare_item(control=_v("pass"), candidate=_v("pass", new), counts=True)
    assert got["hard_stop"] is None and got["new_hard"] == new                  # reported, not a stop
    got = ec.compare_item(control=_v("fail"), candidate=_v("fail", new), counts=True)
    assert got["hard_stop"] is None and got["new_hard"] == new and not got["new_fail"]


def test_a_noisy_class_on_a_passing_candidate_no_longer_stops_the_run():
    store = FakeStore(golden())
    inner = make_judge()

    async def judge(pool, **kw):
        v = await inner(pool, **kw)
        if kw["arm"] == "candidate":            # a class reported on a pass (the judge is asked to)
            return SimpleNamespace(**{**v.__dict__, "failure_codes": ["fact.number"]})
        return v
    out, _, _ = run(store, r2.R2Config(writer="a1"), judge=judge)
    assert out.stop_reason == "completed" and out.verdict == "pass"


def test_the_set_id_constant_is_the_fake_current_version():
    assert SET_ID != "" and asyncio.run(FakeStore(golden()).run_info(BASELINE_RUN))["golden_set_id"] == SET_ID


def test_the_cli_does_not_queue_a_run_the_dry_run_refuses(monkeypatch, capsys):
    import asyncpg

    class Pool:
        async def close(self):
            return None

    async def create_pool(*a, **k):
        return Pool()

    async def est(pool, conf):
        return {"would_refuse": ["controls found 0 < planned 12"]}
    queued = []
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr("shared.secrets.get_database_url", lambda: "postgresql://zz")
    monkeypatch.setattr(r2, "estimate", est)
    monkeypatch.setattr("shared.jobs.registry.enqueue", lambda *a, **k: queued.append(a))
    assert asyncio.run(r2._main(["--writer", "t9_blog", "--enqueue"])) == 1
    assert queued == [] and '"refused"' in capsys.readouterr().out


# ── (F8) the DRY RUN refuses a run its own estimate says ends budget_cap ──────────────────────────────────────────

def _estimate_on(store, monkeypatch, conf):
    async def family(writer, judge_model=None):
        return []
    monkeypatch.setattr(r2, "PgStore", lambda pool, **k: store)
    monkeypatch.setattr(r2, "check_judge_family", family)
    return asyncio.run(r2.estimate(object(), conf))


def test_the_dry_run_refuses_an_estimate_over_the_cap(monkeypatch):
    store = FakeStore(golden(), cost_samples=[0.134] * 10)          # ledger: 0.134 x 1.3 + 2 judge calls per item
    est = _estimate_on(store, monkeypatch, r2.R2Config(writer="a1", cap_usd=2.0))
    assert sum(est["plan"].values()) >= 15 and est["item_estimate"]["source"] == "ledger"
    assert est["estimated_usd_max"] > 2.0
    assert any(x.startswith(f"estimated {est['estimated_usd_max']} > cap 2.0") for x in est["would_refuse"])
    # sized from the estimate, the same plan is not refused
    ok = _estimate_on(store, monkeypatch, r2.R2Config(writer="a1", cap_usd=float(int(est["estimated_usd_max"]) + 1)))
    assert ok["would_refuse"] == []
    # calibrate-only reserves judge calls only: cents, under any cap
    cal = _estimate_on(store, monkeypatch, r2.R2Config(writer="a1", cap_usd=0.5, calibrate_only=True))
    assert cal["estimated_usd_max"] < 0.5 and cal["would_refuse"] == []


def test_the_dry_run_refuses_a_run_the_month_cap_would_stop(monkeypatch):
    store = FakeStore(golden(), cost_samples=[0.134] * 10)

    async def month():
        return cfg.MONTH_CAP_USD - 1.0
    store.month_spend = month
    est = _estimate_on(store, monkeypatch, r2.R2Config(writer="a1", cap_usd=10.0))
    assert [x for x in est["would_refuse"] if x.startswith("month cap")]
    assert not [x for x in est["would_refuse"] if x.startswith("estimated")]


def test_the_cli_queues_nothing_when_the_estimate_passes_the_cap(monkeypatch, capsys):
    import asyncpg

    class Pool:
        async def close(self):
            return None

    async def create_pool(*a, **k):
        return Pool()
    queued = []
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr("shared.secrets.get_database_url", lambda: "postgresql://zz")
    monkeypatch.setattr("shared.jobs.registry.enqueue", lambda *a, **k: queued.append(a))
    store = FakeStore(golden(), cost_samples=[0.134] * 10)

    async def family(writer, judge_model=None):
        return []
    monkeypatch.setattr(r2, "PgStore", lambda pool, **k: store)
    monkeypatch.setattr(r2, "check_judge_family", family)
    assert asyncio.run(r2._main(["--writer", "a1", "--cap-usd", "2", "--enqueue"])) == 1
    out = capsys.readouterr().out
    assert queued == [] and '"refused"' in out and "> cap 2.0" in out
