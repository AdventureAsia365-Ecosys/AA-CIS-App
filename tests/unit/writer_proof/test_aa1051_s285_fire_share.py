"""AA-1051 (S285 C1) — the fire share measurement (services/writer_proof/fire_share.py): the sample's stored control
outputs, the route check, the $0 plan (calls = fields x votes, the estimate from the measured fire cost), the live gate
(confirmation, D12 workflow cap, D12 total, estimate), the paid loop through a FAKE caller (counts per output, the W5
eval label on every logged call, the per-call cap, another answerer) and the shares. No DB, no model call; invented
text only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.writer_proof import fire_share as fs
from services.writer_proof import instrument as wi
from services.writer_proof import prereg as pr
from services.writer_proof import price, spend
from services.writing_memory import config as cfg
from services.writing_memory.eval_judge import EvidenceView, InputOverCap
from tests.unit.writer_proof import test_aa1051_runner as base
from tests.unit.writer_proof import test_aa1051_s283_instrument as s283

IID = "aa1048-two-step-v7"
SOURCE, BAD = s283.SOURCE, s283.BAD
GOOD_OUT = "SEO TITLE:\nRiver market day by boat\n\nBODY:\nWe visit the river market by boat. We walk the old quarter."
BAD_OUT = s283.OUTPUT                     # 2 fields, 4 claims, one of them BAD
SPEND = {"total": 2.65, "workflow": 0.0, "rows": 10, "reserved": {"workflow": 0.0, "total": 0.0}}


def _pre():
    return pr.load("w5-s1")


def _samples():
    ids = _pre().doc["sample_ids"]
    return [fs.Sample(ids[0], SOURCE, BAD_OUT), fs.Sample(ids[1], SOURCE, GOOD_OUT)]


def _inputs(**kw):
    p = wi.pinned(IID)
    out = {"golden_set": "aa729@1.7.0", "samples": _samples(), "skipped": [], "pinned": p,
           "fire": wi.fire_instrument(), "measured": {p.tags["fire"]: {"calls": 500, "mean_usd": 0.001}},
           "route": {"model_key": p.model_key, "connection": p.connection, "role": "judge"},
           "spend": dict(SPEND), "blocking": []}
    out.update(kw)
    return out


# ── the sample and the route ─────────────────────────────────────────────────────────────────────────────────────

def test_sample_evidence_keeps_the_sample_order_and_says_why_an_item_is_left_out(monkeypatch):
    from services.writing_memory import eval_judge as ej
    ids = _pre().doc["sample_ids"]

    def view(writer, inp, output, shared=None):
        if output.get("over"):
            raise InputOverCap("input_over_cap: zz")
        return EvidenceView(source=inp["src"], output=output["text"], sha="zz")
    monkeypatch.setattr(ej, "evidence_view", view)
    items = [{"sample_id": ids[2], "input": {"src": "s2"}, "control_output": {"text": "o2"}},
             {"sample_id": ids[0], "input": {"src": "s0"}, "control_output": {"text": "o0"}},
             {"sample_id": ids[1], "input": {"src": "s1"}, "control_output": None},
             {"sample_id": ids[3], "input": {"src": "s3"}, "control_output": {"over": True}},
             {"sample_id": "zz-dev-1", "input": {"src": "x"}, "control_output": {"text": "x"}}]
    got, skipped = fs.sample_evidence(_pre(), items, None)
    assert [(s.sample_id, s.source, s.output) for s in got] == [(ids[0], "s0", "o0"), (ids[2], "s2", "o2")]
    why = {s["sample_id"]: s["why"] for s in skipped}
    assert why[ids[1]] == "no stored control output" and why[ids[3]] == "over the eval judge's evidence cap"
    assert why[ids[4]] == "not in the golden version" and len(skipped) == len(ids) - 2


@pytest.mark.parametrize("row,needle", [
    (None, "no row"),
    ({"enabled": False, "access_state": "ok", "served_roles": None}, "disabled"),
    ({"enabled": True, "access_state": "quota_exhausted", "served_roles": None}, "quota_exhausted"),
    ({"enabled": True, "access_state": "ok", "served_roles": ["writer"]}, "does not serve role judge"),
])
def test_connection_problems(row, needle):
    assert any(needle in p for p in fs.connection_problems(row, "zz-conn"))


def test_a_connection_serving_judge_or_every_role_passes():
    assert fs.connection_problems({"enabled": True, "access_state": "ok", "served_roles": ["judge"]}, "c") == []
    assert fs.connection_problems({"enabled": True, "access_state": "unknown", "served_roles": None}, "c") == []


# ── the $0 plan and the gate ─────────────────────────────────────────────────────────────────────────────────────

def test_the_plan_counts_one_fire_call_per_field_per_vote_and_prices_the_measured_fire_call():
    out = asyncio.run(fs.dry_run(None, _pre(), inputs=_inputs(), caller=s283._caller()))
    p = wi.pinned(IID)
    assert out["mode"] == "dry_run" and out["instrument"] == IID and out["votes"] == p.votes == 3
    assert out["fire_prompt"] == p.tags["fire"] and out["fire_prompt"].startswith("aa1048-r1-v1@")
    assert [(o["fields"], o["claims"], o["calls"]) for o in out["outputs"]] == [(2, 4, 6), (2, 3, 6)]
    assert out["totals"] == {"outputs": 2, "claims": 7, "calls": 12}
    assert out["fire_call"]["source"] == "ledger_mean" and out["estimated_usd"] == round(12 * 0.001 * 1.3, 4)
    assert out["margin"] == cfg.R2_ESTIMATE_MARGIN and out["worst_case_usd"] > 0
    assert out["blocking"] == [] and "_planned" not in out
    assert out["d12_workflow_cap_usd"] == pr.workflow_cap("s1") and out["d12_total_cap_usd"] == pr.total_cap()
    assert "claim_text" not in str(out) and BAD not in str(out)          # counts only, never the text


def test_the_plan_blocks_without_an_output():
    out = asyncio.run(fs.dry_run(None, _pre(), inputs=_inputs(samples=[]), caller=s283._caller()))
    assert any("no judgeable control output" in b for b in out["blocking"])


def _plan(**kw):
    return fs.plan_of(_pre(), _inputs(**kw), attempts=1)


def test_a_confirmed_cap_within_the_d12_caps_and_above_the_estimate_passes():
    assert fs.refusals(_plan(), workflow="s1", cap_usd=2.5, confirm=spend.CONFIRM_TOKEN) == []


@pytest.mark.parametrize("kw,needle", [
    ({"confirm": None}, "no confirmation"),
    ({"cap_usd": None}, "no cap"),
    ({"cap_usd": 9.0}, "above the D12 cap 8.0"),
    ({"cap_usd": 0.01}, "above the cap 0.01"),
    ({"spend": {"total": 31.0, "reserved": {"total": 0.0}}}, "passes the D12 total 33.0"),
    ({"spend": {"total": 2.0, "reserved": {"total": 30.0}}}, "passes the D12 total"),
    ({"spend": {"total": None, "reserved": {"total": 0.0}}}, "fail closed"),
    ({"blocking": ["zz blocking line"]}, "zz blocking line"),
])
def test_the_live_gate_refuses(kw, needle):
    gate = {"cap_usd": 2.5, "confirm": spend.CONFIRM_TOKEN}
    plan_kw = {k: v for k, v in kw.items() if k in ("spend", "blocking")}
    gate.update({k: v for k, v in kw.items() if k in gate})
    out = fs.refusals(_plan(**plan_kw), workflow="s1", **gate)
    assert any(needle in r for r in out), out


def test_the_cap_ceiling_is_the_setting_in_force(monkeypatch):
    from shared import settings
    plan = _plan()
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: 2.0 if key == pr.WORKFLOWS["s1"]["cap_setting"]
                        else real(key, tenant_id))
    assert any("above the D12 cap 2.0" in r for r in fs.refusals(plan, workflow="s1", cap_usd=2.5,
                                                                    confirm=spend.CONFIRM_TOKEN))


def test_a_refused_run_calls_nothing():
    caller = s283._caller()
    with pytest.raises(fs.FireShareRefused, match="no confirmation"):
        asyncio.run(fs.run(None, _pre(), cap_usd=2.5, confirm=None, requested_by="pytest", inputs=_inputs(),
                           caller=caller, log=_log([]), ledger=_ledger(0.0)))
    assert caller.calls == []


# ── the paid loop through a fake caller ─────────────────────────────────────────────────────────────────────────

def _log(rows):
    async def log(resp, inst, signal, parsed, error):
        from shared.llm_client import call_log
        rows.append({"label": call_log.eval_label(), "signal": dict(signal), "error": error})
    return log


def _ledger(value):
    async def ledger(pool, run_id, stages):
        assert stages == (spend.W5_STAGE,)
        return value
    return ledger


def _run(cap=2.5, caller=None, rows=None, **kw):
    return asyncio.run(fs.run(None, _pre(), cap_usd=cap, confirm=spend.CONFIRM_TOKEN, requested_by="pytest",
                              inputs=_inputs(**kw), caller=caller or s283._caller(),
                              log=_log([] if rows is None else rows), ledger=_ledger(0.006)))


def test_the_run_fires_every_output_and_counts_the_shares():
    rows: list = []
    caller = s283._caller()
    out = _run(caller=caller, rows=rows)
    assert out["ok"] is True and out["mode"] == "live" and out["stopped"] is None and out["identity_mismatch"] is None
    assert set(caller.calls) == {"fire"} and len(caller.calls) == 12            # the fire step alone, no step 1 / 2
    first, second = out["outputs"]
    assert (first["claims"], first["fired"], first["cleared"], first["unverified"]) == (4, 1, 3, 0)
    assert (second["claims"], second["fired"], second["calls"]) == (3, 0, 6) and first["complete"]
    t = out["totals"]
    assert (t["claims"], t["fired"], t["outputs_complete"], t["outputs_cut"]) == (7, 1, 2, 0)
    assert t["fire_share"] == round(1 / 7, 4) and t["planning_share"] == 0.143       # 1/7 rounded UP
    assert out["spend"] == {"charged_usd": round(12 * 0.0005, 6), "ledger_usd": 0.006, "calls": 12,
                            "stage": spend.W5_STAGE}
    # every logged call is relabelled to the W5 stage with this run's id, and names its sample item
    assert {r["label"] for r in rows} == {(spend.W5_STAGE, out["run_id"])} and len(rows) == 12
    assert {r["signal"]["item_id"] for r in rows} == {first["sample_id"], second["sample_id"]}
    assert all(r["signal"]["measure"] == "fire_share" and r["signal"]["step"] == "fire" for r in rows)
    entry = out["fire_share_entry"]
    assert entry["instrument"] == IID and entry["writer"] == "a1" and entry["share"] == 0.143
    assert entry["source"].startswith(f"R1 fire {out['run_id'][:8]} on the w5-s1 GATE controls, aa729@1.7.0")
    assert BAD not in str(out)


def test_the_run_stops_before_a_call_that_would_pass_the_cap():
    caller = s283._caller()
    out = _run(cap=1e-6, caller=caller, measured={wi.pinned(IID).tags["fire"]: {"calls": 500, "mean_usd": 1e-9}})
    assert out["ok"] is False and "run cap" in out["stopped"] and caller.calls == []
    assert out["outputs"][0]["complete"] is False and out["totals"]["outputs_complete"] == 0
    assert out["totals"]["fire_share"] is None and "fire_share_entry" not in out


def test_another_answerer_stops_the_run_and_gives_no_entry():
    out = _run(caller=s283._caller(model="zz-other-model"))
    assert out["ok"] is False and "another model" in out["identity_mismatch"]
    assert out["totals"]["outputs_complete"] == 0 and "fire_share_entry" not in out


def test_totals_count_only_complete_outputs_and_round_the_planning_share_up():
    outs = [{"claims": 10, "fired": 2, "unverified": 1, "cleared": 7, "complete": True},
            {"claims": 20, "fired": 20, "unverified": 0, "cleared": 0, "complete": False}]
    t = fs.totals(outs)
    assert (t["claims"], t["fired"], t["outputs_cut"]) == (10, 2, 1)
    assert (t["fire_share"], t["sent_on_share"], t["planning_share"]) == (0.2, 0.3, 0.3)
    assert fs.totals([{"claims": 3, "fired": 1, "unverified": 0, "cleared": 2, "complete": True}])[
        "planning_share"] == 0.334


# ── the reads ────────────────────────────────────────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, connection_row):
        self.connection_row = connection_row

    async def fetchrow(self, sql, *args):
        if sql == price.CATALOG_SQL:
            return base.CATALOG
        if sql == price.DEPLOYMENT_SQL:
            return base.DEPLOYMENT
        if sql == fs.CONNECTION_SQL:
            assert args == ("openai-direct",)
            return self.connection_row
        assert sql == spend.SPEND_SQL
        return {"total": 2.65, "workflow": 0.0, "rows": 10}

    async def fetch(self, sql, *args):
        if sql == price.INSTRUMENT_CALLS_SQL:
            assert tuple(args[0]) == price.INSTRUMENT_STAGES and list(args[2]) == [wi.pinned(IID).tags["fire"]]
            return [{"tag": args[2][0], "calls": 400, "mean_usd": 0.0012}]
        if sql == spend.ACTIVE_JOBS_SQL:
            return []
        if sql == spend.ATTEMPTS_SQL:
            return []
        raise AssertionError(sql)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return base._Acq(self.conn)


def _store(monkeypatch, version="1.7.0"):
    from services.writing_memory import r2
    ids = _pre().doc["sample_ids"]

    class Store:
        def __init__(self, pool, stages=None):
            assert stages == (spend.W5_STAGE,)

        async def golden_set(self):
            return {"set_id": "set", "name": "aa729", "version": version, "shared_inputs": {}}

        async def items(self, set_id, writer, tenant):
            assert writer == "a1"
            return [{"sample_id": ids[0], "input": {}, "control_output": {}}]
    monkeypatch.setattr(r2, "PgStore", Store)
    monkeypatch.setattr(fs, "sample_evidence", lambda pre, items, shared: ([fs.Sample(ids[0], SOURCE, BAD_OUT)], []))


def test_read_inputs_reads_the_route_the_fire_cost_and_the_w5_spend(monkeypatch):
    _store(monkeypatch)
    row = {"connection_key": "openai-direct", "enabled": True, "access_state": "ok", "served_roles": None}
    got = asyncio.run(fs.read_inputs(_Pool(_Conn(row)), _pre()))
    assert got["blocking"] == [] and got["golden_set"] == "aa729@1.7.0" and len(got["samples"]) == 1
    assert got["route"] == {"model_key": "gpt-6-luna-openai", "connection": "openai-direct", "role": "judge",
                            "enabled": True, "access_state": "ok", "served_roles": None}
    assert got["measured"] == {wi.pinned(IID).tags["fire"]: {"calls": 400, "mean_usd": 0.0012}}
    assert got["spend"]["total"] == 2.65 and got["spend"]["reserved"] == {"workflow": 0, "total": 0}


def test_read_inputs_blocks_on_another_golden_version_and_a_closed_route(monkeypatch):
    _store(monkeypatch, version="1.6.0")
    row = {"connection_key": "openai-direct", "enabled": False, "access_state": "ok", "served_roles": None}
    got = asyncio.run(fs.read_inputs(_Pool(_Conn(row)), _pre()))
    assert any("the active set is aa729@1.6.0" in b for b in got["blocking"])
    assert any("disabled" in b for b in got["blocking"])


def test_a_prereg_without_a_claim_instrument_has_no_fire_step(monkeypatch):
    _store(monkeypatch)
    doc = {**_pre().doc, "judge": {"model_key": "gpt-6-luna-openai", "connection": "openai-direct"}}
    pre = SimpleNamespace(id="w5-s1", writer="a1", workflow="s1", doc=doc)
    got = asyncio.run(fs.read_inputs(_Pool(_Conn(None)), pre))
    assert got["pinned"] is None and any("names no claim instrument" in b for b in got["blocking"])
