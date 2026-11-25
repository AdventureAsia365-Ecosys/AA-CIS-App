"""AA-1154 (A2) — the CIS admin assistant checks the cards people ran: predicted against measured at the minutes of
cis.ops.assist_check_after_min, the hit rate on the status page and in the digest, each action's track record; plus
the four A1 follow-ups (the pii_scrub receipt on the assistant's ledger row, a viewer sees the cached AI part, the
evidence read fails soft with a "try again" message, the display fixes). No database, no AWS, no model: invented
values only."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import asyncpg
import pytest

from api.services import ops_assist
from api.services.ops_assist import analysis, catalog, evidence, explain, outcome, playbooks, replays
from api.services.ops_status import changes, digest
from shared import ops_assist_settings
from shared.llm_client import free_text_scrub
from shared.llm_client.models import LLMResponse

CASE = "llm-access-denied-2026-10-29"
_SECRET = "aa1154-a2-test-secret"
LIMITS = {"evidence_window_min": 30, "changes_window_min": 120, "refused_trigger_calls": 3,
          "throttled_trigger_calls": 3, "spend_spike_factor": 3.0, "spend_baseline_days": 7,
          "spend_spike_floor_usd_h": 0.1, "wide_scope_stages": 5, "small_money_usd": 0.1, "history_min_n": 5,
          "card_ttl_min": 30, "reason_min_chars": 20, "auto_severities": ["sev1", "sev2"], "auto_incident_gap_min": 240,
          "deep_model": "sonnet-5-5", "lookup_windows_min": [15, 60, 360, 1440],
          "check_after_min": [10, 30], "digest_days": 7, "build_timeout_s": 20}
T0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _answer() -> dict:
    return ops_assist.analyse_snapshot(copy.deepcopy(replays.load(CASE)["snapshot"]), LIMITS)


def _card(answer: dict, action: str) -> dict:
    return next(c for c in answer["cards"] if c["action"] == action)


def _bare(item_id: str, **parts) -> dict:
    s = {"as_of": "2026-11-20T03:00:00+00:00", "range": "24h", "item_id": item_id,
         "window": {"since": "2026-11-20T02:30:00+00:00", "minutes": 30},
         "changes_window": {"since": "2026-11-20T01:00:00+00:00", "minutes": 120}, "status": None, "item": None,
         "connections": [], "calls": {}, "spend": {}, "routes": None, "route_versions": [],
         "jobs": {"denied": [], "stuck": [], "queues": []}, "waves": [], "probe": {"switch_on": False, "last": None},
         "changes": [], "history": {}, "unavailable": {}}
    s.update(parts)
    return s


def _item(iid: str, app: str = "cis") -> dict:
    return {"id": iid, "severity": "sev2", "app": app, "title": "zz test item", "detail": "", "since": None,
            "source": "test", "status": "open", "links": [], "msg": None}


# ── verdicts (pure) ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expect,baseline,value,want", [
    ("zero", 3.0, 0.0, "hit"), ("zero", 3.0, 1.0, "miss"), ("nonzero", 0.0, 2.0, "hit"), ("nonzero", 0.0, 0.0, "miss"),
    ("down", 4.0, 1.0, "hit"), ("down", 4.0, 4.0, "miss"), ("down", 0.0, 0.0, "unclear"),
    ("down", None, 1.0, "unclear"),
    ("up", 1.0, 2.0, "hit"), ("up", 1.0, 1.0, "miss"), ("zero", 1.0, None, "unclear"),
])
def test_judge(expect, baseline, value, want):
    assert outcome.judge(expect, baseline, value) == want


def test_combine_any_miss_wins_then_any_hit():
    assert outcome.combine(["hit", "miss"]) == "miss"
    assert outcome.combine(["hit", "unclear"]) == "hit"
    assert outcome.combine(["unclear"]) == "unclear" and outcome.combine([]) == "unclear"


def _run(**kw) -> dict:
    base = {"run_id": 1, "item_id": "queue-shared", "action": "job_cancel", "recorded_at": T0,
            "check_after_min": [10, 30], "checks": [],
            "measures": [outcome.m("job_queued", "zero", "zz left", job_id="zz") | {"baseline": 1.0},
                         outcome.m("oldest_wait_min", "down", "zz wait") | {"baseline": 40.0}]}
    base.update(kw)
    return base


def test_due_checkpoints_and_the_final_verdict():
    r = _run()
    assert outcome.due_checkpoints(r, T0 + timedelta(minutes=9)) == []
    assert outcome.due_checkpoints(r, T0 + timedelta(minutes=12)) == [10]
    first = outcome.checkpoint(r, 10, {0: 0.0, 1: 41.0}, T0 + timedelta(minutes=12))
    assert first["verdict"] == "miss" and [m["verdict"] for m in first["measures"]] == ["hit", "miss"]
    assert outcome.final_verdict(r, [first]) is None
    r2 = _run(checks=[first])
    assert outcome.due_checkpoints(r2, T0 + timedelta(minutes=31)) == [30]
    last = outcome.checkpoint(r2, 30, {0: 0.0, 1: 2.0}, T0 + timedelta(minutes=31))
    assert last["verdict"] == "hit" and outcome.final_verdict(r2, [first, last]) == "hit"


def test_a_late_checkpoint_is_unclear_and_a_card_without_measures_too():
    r = _run()
    late = outcome.checkpoint(r, 10, {0: 0.0, 1: 1.0}, T0 + timedelta(minutes=10 + 10 + 1))
    assert late["late"] is True and late["verdict"] == "unclear"
    assert outcome.checkpoint(_run(measures=[]), 30, {}, T0 + timedelta(minutes=31))["verdict"] == "unclear"


def test_summarize_and_the_hit_rate_words():
    rows = [{"action": "job_cancel", "verdict": "hit"}, {"action": "job_cancel", "verdict": "miss"},
            {"action": "wave_pause", "verdict": "hit"}, {"action": "observe", "verdict": "unclear"},
            {"action": "observe", "verdict": None}]
    t = {"days": 7, **outcome.summarize(rows, {"job_cancel": "Cancel"})}
    assert (t["recorded"], t["checked"], t["hits"], t["unclear"], t["open"]) == (5, 3, 2, 1, 1)
    assert t["by_action"][0] == {"action": "job_cancel", "title": "Cancel", "recorded": 2, "checked": 2, "hits": 1}
    assert outcome.hit_rate_text(t) == ("In the last 7 days 5 action card(s) were marked as done; the assistant's "
                                        "prediction was right 2 of 3 checked (1 unclear, 1 still being checked).")
    assert outcome.hit_rate_text({"days": 7, "recorded": 0}).startswith("No action card")
    assert "none has a clear" in outcome.hit_rate_text({"days": 7, "recorded": 1, "checked": 0})


@pytest.mark.parametrize("item_id,want", [
    ("alarm-e42", ("event", "42")), ("rollout-e7", ("event", "7")), ("svc-aa-zz-api", ("ecs_service", "aa-zz-api")),
    ("health-cis", ("app_health", "cis")), ("queue-shared", ("queue", "shared.job")),
    ("llm-conn-zz", ("signal", "llm-conn-zz")), ("errors-cis", None), ("alarm-ezz", None),
])
def test_item_source(item_id, want):
    assert outcome.item_source(item_id) == want


# ── the cards carry measures ──────────────────────────────────────────────────────────────────────────────────────

def test_every_replayed_card_names_known_measures():
    a = _answer()
    for c in a["cards"]:
        for ms in c["measures"]:
            assert ms["metric"] in outcome.METRICS and ms["expect"] in outcome.EXPECTS and ms["label"]
    assert [ms["metric"] for ms in _card(a, "llm_probe")["measures"]] == ["probe_finished"]
    assert [ms["metric"] for ms in _card(a, "wave_pause")["measures"]][0] == "wave_paused"
    assert _card(a, "route_save")["measures"][0]["metric"] == "refused_calls"
    assert _card(a, "aws_support_case")["measures"] == []     # AWS answers when it answers: unclear by design


def test_queue_and_server_error_cards_measure_their_outcome_and_read_well():
    s = _bare("queue-shared", item=_item("queue-shared"),
              jobs={"denied": [], "stuck": [{"id": "zz-job", "kind": "zz_kind", "run_after": None,
                                             "registered": False}],
                    "queues": [{"id": "shared", "name": "shared.job", "status": "ok", "workers": 0}]})
    out = ops_assist.analyse_snapshot(s, LIMITS)
    cancel = _card(out, "job_cancel")
    assert cancel["measures"][0] == outcome.m("job_queued", "zero", "The job left the queue", job_id="zz-job")
    assert _card(out, "worker_start")["measures"][0]["metric"] == "live_workers"
    s2 = _bare("errors-cis", item=_item("errors-cis"), changes=[
        {"id": "e1", "t": "2026-11-20T02:58:00+00:00", "t2": None, "kind": "deploy", "severity": "info", "app": "cis",
         "subject": "aa-zz-api"}])
    roll = _card(ops_assist.analyse_snapshot(s2, LIMITS), "ecs_rollback")
    assert roll["title"] == "Roll CIS back to the revision before the last deploy"
    assert "at 02:58 UTC" in roll["reason"] and "+00:00" not in roll["reason"]       # S292: no raw ISO time
    assert roll["measures"][0]["params"] == {"item_id": "errors-cis"}


def test_a_track_record_from_history_turns_into_a_history_confidence():
    s = copy.deepcopy(replays.load(CASE)["snapshot"])
    s["history"] = {"llm_probe": {"n": 6, "hits": 5}}
    card = _card(ops_assist.analyse_snapshot(s, LIMITS), "llm_probe")
    assert card["predictions"][-1]["confidence"] == "history" and "5 of 6" in card["predictions"][-1]["text"]


# ── record and checks (fake pool) ─────────────────────────────────────────────────────────────────────────────────

class RunPool:
    """Answers the outcome SQL: one table in memory; the metric reads return what ``values`` says."""

    def __init__(self, values: Optional[dict] = None, missing: bool = False):
        self.values = values or {}
        self.missing = missing
        self.rows: list[dict] = []
        self.updates: list[tuple] = []

    def _gone(self):
        if self.missing:
            raise asyncpg.UndefinedTableError("relation \"acp_shared.ops_assist_run\" does not exist")

    async def fetchval(self, sql, *args):
        if "ops_assist_run" in sql:
            self._gone()
            if sql == outcome._RECENT_SQL:
                return next((r["run_id"] for r in self.rows if r["card_id"] == args[0] and r["recorded_at"] > args[1]),
                            None)
            if sql == outcome._INSERT_SQL:
                cols = ("item_id", "card_id", "action", "playbook", "risk_level", "range_key", "incident_id",
                        "predicted", "measures", "check_after_min", "recorded_by", "recorded_at")
                row = dict(zip(cols, args), run_id=len(self.rows) + 1, checks="[]", verdict=None, checked_at=None)
                self.rows.append(row)
                return row["run_id"]
            if sql == outcome._UPDATE_SQL:
                self.updates.append(args)
                r = next(x for x in self.rows if x["run_id"] == args[0])
                r.update(checks=args[1], verdict=args[2], checked_at=args[3])
                return args[0]
        if sql == outcome._INCIDENT_SQL:
            return 77
        for key, v in self.values.items():
            if key in sql:
                return v
        return None

    async def fetchrow(self, sql, *args):
        if sql == outcome._CALLS_SQL:
            return {"refused": 4.0, "throttled": 0.0, "answered": 9.0, "usd": 0.5}
        return None

    async def fetch(self, sql, *args):
        self._gone()
        if sql == outcome._OPEN_SQL:
            return [r for r in self.rows if r["verdict"] is None]
        if sql == outcome._HISTORY_SQL:
            return [{"action": "job_cancel", "n": 3, "hits": 2}]
        if sql == outcome._TRACK_SQL:
            return [{"action": r["action"], "verdict": r["verdict"]} for r in self.rows]
        if sql == outcome._LIST_SQL:
            return list(self.rows)
        return []


def _queue_answer() -> dict:
    s = _bare("queue-shared", item=_item("queue-shared"),
              jobs={"denied": [], "stuck": [{"id": "zz-job", "kind": "zz_kind", "run_after": None,
                                             "registered": False}], "queues": []})
    return ops_assist.analyse_snapshot(s, LIMITS)


def test_record_reads_the_baselines_links_the_incident_and_refuses_a_second_click():
    pool = RunPool(values={"status = 'queued')": 1.0, "min(run_after)": 42.0})
    a = _queue_answer()
    card = _card(a, "job_cancel")
    out = run(outcome.record(pool, a, card, actor="admin:zz", limits=LIMITS, now=T0))
    assert out["incident_id"] == 77 and out["check_after_min"] == [10, 30]
    assert [ms["baseline"] for ms in out["measures"]] == [1.0, 42.0]
    row = pool.rows[0]
    assert row["action"] == "job_cancel" and row["risk_level"] == card["risk"]["level"]
    assert row["recorded_by"] == "admin:zz"
    assert json.loads(row["predicted"])[0]["text"] == card["predictions"][0]["text"]
    with pytest.raises(outcome.AlreadyRecorded):
        run(outcome.record(pool, a, card, actor="admin:zz", limits=LIMITS, now=T0 + timedelta(minutes=5)))
    with pytest.raises(outcome.NotSetUp):
        run(outcome.record(RunPool(missing=True), a, card, actor="admin:zz", limits=LIMITS, now=T0))


def test_check_due_takes_each_checkpoint_once_and_finishes_the_run():
    pool = RunPool(values={"status = 'queued')": 1.0, "min(run_after)": 42.0})
    a = _queue_answer()
    run(outcome.record(pool, a, _card(a, "job_cancel"), actor="admin:zz", limits=LIMITS, now=T0))
    pool.values = {"status = 'queued')": 0.0, "min(run_after)": 3.0}
    assert run(outcome.check_due(pool, limits=LIMITS, now=T0 + timedelta(minutes=5))) == {"checked": 0,
                                                                                         "finished": 0}
    assert run(outcome.check_due(pool, limits=LIMITS, now=T0 + timedelta(minutes=12))) == {"checked": 1,
                                                                                          "finished": 0}
    assert run(outcome.check_due(pool, limits=LIMITS, now=T0 + timedelta(minutes=31))) == {"checked": 1,
                                                                                          "finished": 1}
    checks = json.loads(pool.rows[0]["checks"])
    assert [c["after_min"] for c in checks] == [10, 30] and pool.rows[0]["verdict"] == "hit"
    t = run(outcome.track(pool, days=7, now=T0 + timedelta(minutes=40)))
    assert (t["checked"], t["hits"]) == (1, 1) and "right 1 of 1" in t["text"]
    assert run(outcome.history(pool)) == {"job_cancel": {"n": 3, "hits": 2}}
    assert run(outcome.check_due(RunPool(missing=True), limits=LIMITS, now=T0))["not_set_up"] is True
    assert run(outcome.history(RunPool(missing=True))) == {}


def test_an_unreadable_measure_is_unclear_never_a_guess():
    class Boom(RunPool):
        async def fetchval(self, sql, *args):
            if "ops_assist_run" in sql or sql == outcome._INCIDENT_SQL:
                return await super().fetchval(sql, *args)
            raise OSError("zz down")

    assert run(outcome.read(Boom(), outcome.m("oldest_wait_min", "down", "zz"), {"now": T0, "limits": LIMITS})) is None


def test_signal_open_reads_the_assistants_own_signals_once_per_tick(monkeypatch):
    calls = []

    async def collect(pool, redis, **kw):
        calls.append(kw)
        return _bare("", connections=[{"connection_key": "zz", "circuit": {"refusing": True,
                                                                           "error_class": "access_denied"}}])

    monkeypatch.setattr(evidence, "collect", collect)
    ctx = {"now": T0, "limits": LIMITS}
    assert run(outcome.read(None, outcome.m("item_open", "zero", "zz", item_id="llm-conn-zz"), ctx)) == 1.0
    assert run(outcome.read(None, outcome.m("item_open", "zero", "zz", item_id="llm-conn-yy"), ctx)) == 0.0
    assert len(calls) == 1 and calls[0]["include_status"] is False


# ── fail soft (S292 follow-up 3) ──────────────────────────────────────────────────────────────────────────────────

def test_a_slow_evidence_read_raises_evidence_timeout(monkeypatch):
    async def slow(pool, redis, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(evidence, "collect", slow)
    with pytest.raises(ops_assist.EvidenceTimeout):
        run(ops_assist.collect_in_time(None, None, {**LIMITS, "build_timeout_s": 0.01}, item_id="zz"))


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", MagicMock(), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    monkeypatch.setattr(ops_assist_settings, "snapshot", lambda: dict(LIMITS))

    async def not_owner(pool, staff_id):
        return False

    monkeypatch.setattr(evidence, "business_owner", not_owner)
    yield TestClient(app)


def _h(role="admin") -> dict:
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "00000000-0000-0000-0000-000000001154"}
    if role:
        h["x-admin-role"] = role
    return h


def test_cards_route_answers_503_try_again_when_the_evidence_is_late(client, monkeypatch):
    async def late(*a, **kw):
        raise ops_assist.EvidenceTimeout("zz")

    monkeypatch.setattr(ops_assist, "collect_in_time", late)
    for resp in (client.get("/admin/ops-assist/items/errors-cis/cards", headers=_h()),
                 client.post("/admin/ops-assist/items/errors-cis/analyse", headers=_h())):
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["code"] == "assistant_timeout" and detail["retry"] is True and "Try again" in detail["message"]


# ── a viewer reads the cached AI part (S292 follow-up 2) ──────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.d: dict = {}

    async def get(self, k):
        return self.d.get(k)


def test_cached_view_merges_the_saved_ai_part_without_a_call(monkeypatch):
    base = _answer()

    async def analyse(pool, redis, item_id, **kw):
        return copy.deepcopy(base)

    monkeypatch.setattr(ops_assist, "analyse", analyse)
    redis = FakeRedis()
    plain = run(analysis.cached(None, redis, base["item_id"]))
    assert plain["llm"]["used"] is False and plain.get("ai_summary") is None
    first = base["cards"][-1]["id"]
    part = {"summary": "zz summary", "owner_summary": "zz owner", "ranking": [first], "explanations": {
        first: {"text": "zz why", "evidence": base["cards"][-1]["evidence"][:1]}}, "dropped": []}
    redis.d[analysis.cache_key(base["item_id"], "24h", "quick")] = json.dumps(
        {"part": part, "meta": explain.meta(depth="quick", used=True, analysed_at="2026-11-25T09:00:00+00:00")})
    seen = run(analysis.cached(None, redis, base["item_id"]))
    assert seen["llm"]["cached"] is True and seen["llm"]["calls"] == 0 and seen["ai_summary"]["text"] == "zz summary"
    assert next(c for c in seen["cards"] if c["id"] == first)["explanation"]["text"] == "zz why"
    owner = run(analysis.cached(None, redis, base["item_id"], business_owner=True))
    assert owner["audience"] == "summary" and owner["cards"] == [] and owner["ai_summary"]["owner_text"] == "zz owner"


# ── the pii_scrub receipt on the assistant's own ledger row (S292 follow-up 1) ───────────────────────────────────

class PreflightPool:
    def acquire(self):
        class _Conn:
            async def fetchrow(self, sql, *args):
                return {"active": True, "capped": True, "model_enabled": True}

        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def test_the_answered_row_carries_the_scrub_receipt_of_what_was_sent(monkeypatch):
    rows: list[dict] = []

    async def record(pool, **kw):
        rows.append(kw)

    monkeypatch.setattr("shared.llm_client.call_log.record_call_with_pool", record)
    sent: list[Any] = []

    def llm(req):
        sent.append(req)
        return LLMResponse(content='{"answer": "zz", "cited": []}', model_used="haiku-5-5", provider="anthropic",
                           input_tokens=10, output_tokens=5, cost_usd=0.0001)

    user = "zz alarm: write to zz.person@example.com or call +84 912 345 678"
    parsed, resp, err = run(explain.call_gateway(PreflightPool(), system="zz system", user=user, schema={},
                                                 depth="quick", task="zz", quality=lambda p: {}, llm=llm))
    assert err is None and resp is not None
    receipt = rows[0]["quality_signal"]["pii_scrub"]
    assert receipt["status"] == free_text_scrub.SCRUB_STATUS_OK and receipt["version"] == free_text_scrub.VERSION
    assert receipt["redacted"].get("[email]") == 1 and receipt["redacted"].get("[phone]") == 1
    assert "example.com" not in sent[0].user_prompt
    assert receipt["scrubbed_hash"] == free_text_scrub.scrubbed_hash(sent[0])
    again, _ = free_text_scrub.scrub_request(sent[0])          # the gateway's second scrub changes nothing
    assert free_text_scrub.scrubbed_hash(again) == receipt["scrubbed_hash"]


def test_a_failed_scrub_means_no_call(monkeypatch):
    def boom(req):
        raise ValueError("zz residual")

    monkeypatch.setattr(free_text_scrub, "scrub_request", boom)
    called = []
    out = run(explain.call_gateway(PreflightPool(), system="s", user="u", schema={}, depth="quick", task="zz",
                                   quality=lambda p: {}, llm=lambda r: called.append(r)))
    assert out == (None, None, "scrub_failed") and called == []


# ── POST /runs, GET /runs, GET /track ─────────────────────────────────────────────────────────────────────────────

def test_may_record_needs_the_cards_own_capability():
    from api.routers.admin_ops_assist import may_record

    assert may_record(frozenset({"ai.admin"}), {"approval": {"capability": ["ai.admin"]}}) is True
    assert may_record(frozenset({"ops.write"}), {"approval": {"capability": ["ai.admin"]}}) is False
    assert may_record(frozenset({"ops.write"}), {"approval": {"capability": []}}) is True
    assert may_record(frozenset({"ops.read"}), {"approval": {"capability": []}}) is False


def test_post_run_records_the_rebuilt_card(client, monkeypatch):
    a = _queue_answer()
    got = {}

    async def analyse(pool, redis, item_id, **kw):
        return copy.deepcopy(a)

    async def record(pool, answer, card, *, actor, limits, now=None):
        got.update(card=card["id"], actor=actor)
        return {"run_id": 5, "card_id": card["id"]}

    monkeypatch.setattr(ops_assist, "analyse", analyse)
    monkeypatch.setattr(outcome, "record", record)
    card_id = _card(a, "job_cancel")["id"]
    ok = client.post("/admin/ops-assist/runs", json={"item_id": "queue-shared", "card_id": card_id}, headers=_h())
    assert ok.status_code == 200, ok.text
    assert got == {"card": card_id, "actor": "admin:00000000-0000-0000-0000-000000001154"}
    gone = client.post("/admin/ops-assist/runs", json={"item_id": "queue-shared", "card_id": "zz:none"}, headers=_h())
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "card_gone"
    assert client.post("/admin/ops-assist/runs", json={"item_id": "queue-shared", "card_id": card_id},
                       headers=_h("viewer")).status_code == 403
    extra = client.post("/admin/ops-assist/runs", json={"item_id": "queue-shared", "card_id": card_id, "x": 1},
                        headers=_h())
    assert extra.status_code == 422


def test_post_run_maps_already_recorded_and_not_set_up(client, monkeypatch):
    a = _queue_answer()

    async def analyse(pool, redis, item_id, **kw):
        return copy.deepcopy(a)

    monkeypatch.setattr(ops_assist, "analyse", analyse)
    card_id = _card(a, "job_cancel")["id"]
    for exc, status, code in ((outcome.AlreadyRecorded("zz"), 409, "already_recorded"),
                              (outcome.NotSetUp("zz"), 503, "not_set_up")):
        async def record(*args, _e=exc, **kw):
            raise _e

        monkeypatch.setattr(outcome, "record", record)
        resp = client.post("/admin/ops-assist/runs", json={"item_id": "queue-shared", "card_id": card_id},
                           headers=_h())
        assert resp.status_code == status and resp.json()["detail"]["code"] == code


def test_runs_and_track_routes(client, monkeypatch):
    async def runs(pool, item_id=None):
        return [{"run_id": 1, "item_id": item_id or "zz"}]

    async def track(pool, *, days, now=None):
        return {"days": days, "recorded": 0, "text": "zz"}

    monkeypatch.setattr(outcome, "runs", runs)
    monkeypatch.setattr(outcome, "track", track)
    r = client.get("/admin/ops-assist/runs?item_id=queue-shared", headers=_h()).json()
    assert r == {"runs": [{"run_id": 1, "item_id": "queue-shared"}], "check_after_min": [10, 30]}
    assert client.get("/admin/ops-assist/track", headers=_h()).json()["days"] == 7

    async def missing(*a, **kw):
        raise outcome.NotSetUp("zz")

    monkeypatch.setattr(outcome, "track", missing)
    assert client.get("/admin/ops-assist/track", headers=_h()).json()["detail"]["code"] == "not_set_up"


# ── digest and display fixes ──────────────────────────────────────────────────────────────────────────────────────

def test_the_digest_carries_the_assistant_paragraph():
    part = {"status": "ok", "open": 0, "link": "https://admin.zz.example/admin/overview", "items": [],
            "assistant": {"text": "In the last 7 days zz."}}
    md = "\n".join(digest.markdown(part))
    assert "**The admin assistant:** In the last 7 days zz." in md
    assert "not available (not set up yet)" in "\n".join(digest.assistant_markdown({"error": "not set up yet"}))
    assert digest.assistant_markdown(None) == []


def test_a_migration_title_never_shows_a_double_colon():
    assert changes.migration_title("488", "AA-1141: zz role check") == "Migration 488: Zz role check"
    assert changes.migration_title("489", None) == "Migration 489"


def test_every_catalogue_action_with_a_check_after_has_a_measure_somewhere():
    """Each action a playbook proposes with a "check after" text gets a structured measure on its card (the replay
    and the queue snapshot cover every proposed action but route_preview, route_revert and app_bulk_pause, whose
    playbooks are covered in test_queue_and_server_error_cards_measure_their_outcome_and_read_well and the throttle
    and spike playbooks below)."""
    s = _bare("llm-throttle-zz", connections=[{"connection_key": "zz", "label": "zz",
                                               "circuit": {"state": "open", "error_class": "throttled"}}],
              calls={"zz": {"ok": 1, "refused": 0, "error": 3, "access_refused": 0, "classes": {"throttled": 5},
                            "refused_models": [], "refused_by_model": {}, "models": [], "cost_usd": 0.0}})
    out = ops_assist.analyse_snapshot(s, LIMITS)
    assert _card(out, "app_bulk_pause")["measures"][0]["metric"] == "throttled_calls"
    spike = ops_assist.analyse_snapshot(_bare("ai-cis", item=_item("ai-cis"),
                                              spend={"zz": {"per_hour_usd": 2.0, "baseline_hour_usd": 0.1,
                                                            "window_usd": 1.0}}), LIMITS)
    pause = _card(spike, "app_bulk_pause")
    assert pause["measures"][0]["metric"] == "app_spend_per_hour" and pause["title"].endswith("of CIS")
    assert {a.id for a in catalog.ACTIONS if a.check_after} >= {"llm_probe", "wave_pause", "app_bulk_pause"}
