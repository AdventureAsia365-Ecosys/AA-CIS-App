"""AA-1003 R2 — OPS-12 daily digest (render + section isolation), the report e-mail to the opt-in list, the TP-03
ops alert template. No DB, no AWS, no SES (stub client)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import report_jobs, report_ops_jobs
from services.reports import digest, registry
from shared import ops_alerts

UTC = timezone.utc
D = date(2026, 10, 29)
NOW = datetime(2026, 10, 30, 0, 31, tzinfo=UTC)


def _green_facts() -> dict:
    return {
        "errors": {},
        "apps": {"items": [{"id": "cis", "label": "CIS", "status": "ok", "reason": "ok", "git_sha": "abc12345ff"},
                           {"id": "booking", "label": "Booking", "status": "ok", "reason": "ok", "git_sha": "b1"},
                           {"id": "tripplanner", "label": "TripPlanner", "status": "no_probe",
                            "reason": "no health endpoint yet"}],
                 "workers": {"alive": 2, "commits": {"abc12345ff": 2}, "same_commit_as_api": True}},
        "alarms": {"status": "ok", "firing": [], "total": 30},
        "jobs": {"failed_by_kind": [], "failed_total": 0, "overdue_queued": 0, "oldest_due_at": None,
                 "expired_leases": 0},
        "llm": {"status": "ok", "last_ok_at": "2026-10-29T10:00:00+00:00", "cause": None, "calls": 12,
                "providers": [{"provider": "bedrock", "account": None, "embed": False, "calls": 12, "ok_calls": 12}],
                "failures": []},
        "probes": {"status": "ok", "passed": 2, "expected": 2, "window": {},
                   "probes": [{"probe": "liveness", "label": "Tour liveness gate", "cron_utc": "04:43",
                               "status": "passed", "value": 0, "value_field": "violations",
                               "ran_at": "2026-10-29T04:46:00+00:00"}]},
        "deploys": {"since": "x", "workers": [], "migrations": []},
        "waves": {"open": []},
        "handoff": {"last_check": None},
        "reports": {"flagged": []},
    }


def _kpi(kpi_id, day, value, scope="all"):
    return {"kpi_id": kpi_id, "day": day, "scope": scope, "value": value}


def test_digest_all_green_is_short_and_says_so():
    ops12 = registry.load().report("OPS-12")
    rows = [_kpi("K-LLM-COST-DAY", D - timedelta(days=i), 0.5) for i in range(0, 15)]
    out = digest.render_digest(registry.load(), ops12, D, _green_facts(), rows, NOW)
    assert out.output["attention"] == [] and out.summary.endswith(": all green")
    assert out.output["apps_green"] == {"green": 2, "with_probe": 2}
    assert "Nothing: all green." in out.markdown and "Not visible from CIS" in out.markdown


def test_digest_flags_every_kind_of_problem():
    facts = _green_facts()
    facts["errors"] = {"waves": "UndefinedTable: x"}
    facts["apps"]["items"][1].update(status="degraded", reason="/booking/health/ready answered 503")
    facts["apps"]["workers"].update(commits={"old": 1}, same_commit_as_api=False)
    facts["alarms"]["firing"] = [{"app": "cis", "name": "aa-cis-dev-5xx", "since": None}]
    facts["jobs"].update(failed_total=3, failed_by_kind=[{"kind": "a3_atomize", "n": 3}], overdue_queued=1)
    facts["llm"].update(status="blocked", cause={"error_class": "access_denied", "at": "t"})
    facts["probes"]["probes"][0].update(status="failed", value=4)
    facts["reports"]["flagged"] = [{"id": "EX-04", "name": "KPI scorecard", "due": "overdue", "failed_7d": 1}]
    rows = [_kpi("K-LLM-COST-DAY", D, 9.0), _kpi("K-LLM-COST-DAY", D - timedelta(days=1), 1.0),
            _kpi("K-LLM-FALLBACK-24H", D, 0.2), _kpi("K-TP-HANDOFFGAP", D, 1), _kpi("K-BK-PARKED", D, 2)]
    items = digest.attention(facts, digest._series(rows), D)
    text = " | ".join(items)
    for needle in ("Section waves", "Booking: degraded", "not the API's commit", "aa-cis-dev-5xx",
                   "3 job(s) failed", "overdue by more than 30 min", "LLM route blocked", "fallback rate 20.0 %",
                   "Tour liveness gate: failed", "LLM spend 9.00 USD", "1 TripPlanner inquiries",
                   "2 inquiries parked", "Report EX-04"):
        assert needle in text, needle
    out = digest.render_digest(registry.load(), registry.load().report("OPS-12"), D, facts, rows, NOW)
    assert out.summary.endswith(f"{len(items)} items need a look")


def test_an_idle_llm_route_with_no_answer_is_not_green():
    facts = _green_facts()
    # 29/10/2026 shape: the last answer is older than 6 h, the failed jobs have left the 24 h window
    facts["llm"].update(status="unknown", last_ok_at=None, calls=0, text_ok_calls=0, providers=[])
    items = digest.attention(facts, {}, D)
    assert any(i.startswith("LLM route unknown: no answered text call") for i in items)
    out = digest.render_digest(registry.load(), registry.load().report("OPS-12"), D, facts, [], NOW)
    assert not out.summary.endswith("all green")
    # embeddings answering do not prove the text route works
    facts["llm"].update(text_ok_calls=None, providers=[
        {"provider": "bedrock", "account": None, "embed": True, "calls": 4, "ok_calls": 4}])
    assert any(i.startswith("LLM route unknown") for i in digest.attention(facts, {}, D))
    # unknown with answered text calls earlier in the 24 h (just older than 6 h): no item
    facts["llm"].update(text_ok_calls=3)
    assert not any(i.startswith("LLM route") for i in digest.attention(facts, {}, D))


def test_worker_parity_is_not_judged_without_the_api_commit():
    live = [{"git_sha": "abc", "alive": True}, {"git_sha": "abc", "alive": True}]
    assert digest.worker_parity(live, "abc")["same_commit_as_api"] is True
    assert digest.worker_parity(live, "def")["same_commit_as_api"] is False
    unknown = digest.worker_parity(live, None)
    assert unknown == {"alive": 2, "commits": {"abc": 2}, "same_commit_as_api": None}
    facts = _green_facts()
    facts["apps"]["items"][0].update(status="no_answer", reason="health endpoint did not answer (ConnectTimeout)",
                                     git_sha=None)
    facts["apps"]["workers"] = unknown
    items = digest.attention(facts, {}, D)
    assert items == ["CIS: no_answer (health endpoint did not answer (ConnectTimeout))"]
    out = digest.render_digest(registry.load(), registry.load().report("OPS-12"), D, facts, [], NOW)
    assert "the API's commit is unknown (no answer), not compared." in out.markdown


def test_load_apps_reuses_the_platform_health_card(monkeypatch):
    from api.services import platform_apps
    from shared.jobs import queue

    calls = []

    async def card(app_id, client=None, *, base=None, paths=None):
        calls.append((app_id, base, paths))
        if app_id == "cis":
            return {"id": "cis", "label": "CIS", "status": "unknown",
                    "probe": {"status": "unknown", "reason": "health endpoint did not answer (ConnectTimeout)"}}
        return {"id": app_id, "label": app_id, "status": "ok", "git_sha": "b1",
                "probe": {"status": "ok", "reason": "Health and readiness endpoints answer 200"},
                "ready": {"schema_version": "bk-017"}}

    monkeypatch.setattr(platform_apps, "http_health_card", card)
    monkeypatch.setattr(queue, "worker_health", AsyncMock(return_value={"workers": [
        {"git_sha": "abc", "alive": True}, {"git_sha": "old", "alive": False}]}))
    monkeypatch.setenv("PUBLIC_API_BASE", "https://api.example.invalid/")
    out = asyncio.run(digest.load_apps(MagicMock()))
    assert calls[0] == ("cis", "https://api.example.invalid", ("/health", "/health/ready"))
    assert {c[0] for c in calls} == {"cis", "booking", "crm"}
    assert out["items"][0]["status"] == "no_answer" and out["items"][1]["schema_version"] == "bk-017"
    assert out["items"][-1]["status"] == "no_probe"
    assert out["workers"] == {"alive": 1, "commits": {"abc": 1}, "same_commit_as_api": None}


def test_cis_api_origin_defaults_to_dev_and_follows_public_api_base(monkeypatch):
    from api.core import app_registry

    monkeypatch.delenv("PUBLIC_API_BASE", raising=False)
    assert app_registry.cis_api_origin() == app_registry.CIS_API_ORIGIN
    monkeypatch.setenv("PUBLIC_API_BASE", " https://api.example.invalid/ ")
    assert app_registry.cis_api_origin() == "https://api.example.invalid"


def test_spend_flag_needs_both_the_factor_and_the_floor():
    def pts(v, hist):
        return digest._series([_kpi("K-LLM-COST-DAY", D, v)] + [
            _kpi("K-LLM-COST-DAY", D - timedelta(days=i), hist) for i in range(1, 15)])
    assert digest.spend_line(pts(0.9, 0.1), D)["flagged"] is False         # under 1 USD: noise on Dev
    assert digest.spend_line(pts(3.0, 2.0), D)["flagged"] is False         # under 2x the median
    assert digest.spend_line(pts(5.0, 2.0), D)["flagged"] is True


def test_unreadable_probes_and_alarms_are_named_not_hidden():
    facts = _green_facts()
    facts["probes"] = {"status": "unavailable", "reason": "not_permitted", "message": "pending"}
    facts["alarms"] = {"status": "unavailable", "reason": "not_permitted"}
    items = digest.attention(facts, {}, D)
    assert any("Nightly probe results unavailable (not_permitted)" in i for i in items)
    assert any("Alarms could not be read" in i for i in items)


def test_collect_isolates_a_failing_section(monkeypatch):
    for name in ("load_apps", "load_jobs", "load_llm", "load_deploys", "load_waves", "load_handoff", "load_reports"):
        monkeypatch.setattr(digest, name, AsyncMock(return_value={"ok": name}))
    monkeypatch.setattr(digest, "load_alarms", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(digest, "load_probes", AsyncMock(return_value={"status": "ok"}))
    facts = asyncio.run(digest.collect(MagicMock(), registry.load(), D, NOW))
    assert facts["errors"] == {"alarms": "RuntimeError: boom"} and facts["jobs"] == {"ok": "load_jobs"}


# ── report e-mail ───────────────────────────────────────────────────────────────────────────────────────────────

def test_recipients_come_from_the_opt_in_variable_only():
    env = {"OPS_DIGEST_EMAIL_TO": "Lead@Example.com; lead@example.com, not-an-address  ops@example.org"}
    to, refused = report_ops_jobs.report_recipients("OPS-12", env)
    assert to == ["lead@example.com", "ops@example.org"] and refused == ["not-an-address"]
    assert report_ops_jobs.report_recipients("EX-04", env) == ([], [])
    assert report_ops_jobs.report_recipients("OPS-12", {}) == ([], [])


def _run(status="succeeded", report_id="OPS-12"):
    return {"id": "22222222-2222-2222-2222-222222222222", "report_id": report_id, "status": status,
            "summary": "Daily platform health digest 2026-10-29: all green",
            "output": {"headline": "Daily platform health digest 2026-10-29: all green"},
            "output_markdown": "# Daily platform health digest 2026-10-29: all green\n\n<b>x</b>"}


def _ctx(payload, ses=None):
    ctx = MagicMock()
    ctx.payload, ctx.pool, ctx.job_id = payload, MagicMock(), "j"
    ctx.resources = {"ses": ses} if ses else {}
    return ctx


def test_report_email_is_skipped_without_opt_in(monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.com")
    monkeypatch.delenv("OPS_DIGEST_EMAIL_TO", raising=False)
    monkeypatch.setattr(report_ops_jobs.store, "get_run", AsyncMock(return_value=_run()))
    out = asyncio.run(report_ops_jobs.run_report_email(_ctx({"run_id": "r", "report_id": "OPS-12"})))
    assert out["status"] == "skipped" and "OPS_DIGEST_EMAIL_TO" in out["reason"]


def test_report_email_sends_the_markdown_to_the_list(monkeypatch):
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.com")
    monkeypatch.setenv("OPS_DIGEST_EMAIL_TO", "lead@example.com")
    monkeypatch.setattr(report_ops_jobs.store, "get_run", AsyncMock(return_value=_run()))
    ses = MagicMock()
    ses.send_email.return_value = {"MessageId": "m-1"}
    out = asyncio.run(report_ops_jobs.run_report_email(_ctx({"run_id": "r", "report_id": "OPS-12"}, ses)))
    assert out["status"] == "sent" and out["recipients_count"] == 1
    kw = ses.send_email.call_args.kwargs
    assert kw["Destination"]["ToAddresses"] == ["lead@example.com"]
    assert kw["Content"]["Simple"]["Subject"]["Data"].endswith("2026-10-29: all green")
    body = kw["Content"]["Simple"]["Body"]
    assert "<b>x</b>" in body["Text"]["Data"] and "&lt;b&gt;x&lt;/b&gt;" in body["Html"]["Data"]


@pytest.mark.parametrize("run,payload", [(_run(status="failed"), {"run_id": "r", "report_id": "OPS-12"}),
                                         (_run(), {"run_id": "r", "report_id": "EX-04"}),
                                         (None, {"run_id": "r", "report_id": "OPS-12"})])
def test_report_email_refuses_what_it_must_not_send(monkeypatch, run, payload):
    monkeypatch.setattr(report_ops_jobs.store, "get_run", AsyncMock(return_value=run))
    with pytest.raises(report_ops_jobs.NonRetryable):
        asyncio.run(report_ops_jobs.run_report_email(_ctx(payload)))


@pytest.mark.parametrize("report_id,trigger,emailed", [("OPS-12", "schedule", True), ("OPS-12", "manual", False),
                                                       ("EX-04", "schedule", False)])
def test_report_generate_queues_the_email_for_scheduled_digests_only(monkeypatch, report_id, trigger, emailed):
    from contextlib import asynccontextmanager

    run = {"id": "r1", "report_id": report_id, "status": "queued", "trigger": trigger,
           "period_start": D, "period_end": D}
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock())
    monkeypatch.setattr(report_jobs.store, "finish_ok", AsyncMock())
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(
        return_value=report_jobs.generate.Rendered("s", {}, "# md")))
    enq = AsyncMock(return_value={"job_id": "e1", "created": True})
    monkeypatch.setattr(report_ops_jobs, "enqueue_report_email", enq)
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: acq())
    ctx = _ctx({"run_id": "r1"})
    ctx.pool = pool
    out = asyncio.run(report_jobs.run_report(ctx))
    assert enq.await_count == (1 if emailed else 0) and ("email" in out) is emailed


def test_a_failed_email_enqueue_does_not_fail_the_report(monkeypatch):
    from contextlib import asynccontextmanager

    run = {"id": "r1", "report_id": "OPS-12", "status": "queued", "trigger": "schedule",
           "period_start": D, "period_end": D}
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock())
    monkeypatch.setattr(report_jobs.store, "finish_ok", AsyncMock())
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(
        return_value=report_jobs.generate.Rendered("s", {}, "# md")))
    monkeypatch.setattr(report_ops_jobs, "enqueue_report_email", AsyncMock(side_effect=RuntimeError("db")))
    pool = MagicMock()

    @asynccontextmanager
    async def acq():
        yield MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: acq())
    ctx = _ctx({"run_id": "r1"})
    ctx.pool = pool
    out = asyncio.run(report_jobs.run_report(ctx))
    assert "RuntimeError" in out["email"]["error"]


def test_report_kinds_are_background_and_not_deploy_blocking():
    from shared.jobs.registry import kinds

    k = kinds()
    for name in (report_ops_jobs.CHECK_KIND, report_ops_jobs.EMAIL_KIND):
        assert k[name].background and not k[name].deploy_blocking


# ── TP-03 ops alert e-mail ──────────────────────────────────────────────────────────────────────────────────────

def test_inquiry_handoff_alert_template_and_daily_key(monkeypatch):
    payload = {"template": ops_alerts.TEMPLATE_INQUIRY_HANDOFF_GAP, "gap": 2, "sent": 5, "received": 3,
               "parked": 0, "failed": 1, "window": {"first": "2026-10-24", "last": "2026-10-30"},
               "checked_at": "2026-10-30T03:00:00+00:00"}
    msg = ops_alerts.render_inquiry_handoff_gap(payload, to=("a@example.com",), base_url="https://admin.example",
                                                environment="dev")
    assert "2 TripPlanner inquiries not in the Booking inbox" in msg.subject
    assert "https://admin.example/admin/reports" in msg.text and "2026-10-24 to 2026-10-30" in msg.text
    from services.jobs import ops_alert_email_job
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_INQUIRY_HANDOFF_GAP] is \
        ops_alerts.render_inquiry_handoff_gap
    monkeypatch.setenv("SES_FROM_ADDRESS", "no-reply@example.com")
    from shared.jobs import queue
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(queue, "enqueue", enq)
    got = asyncio.run(ops_alerts.enqueue_inquiry_handoff_email(MagicMock(), details={"gap": 2},
                                                               now=datetime(2026, 10, 30, 3, tzinfo=UTC)))
    assert got == "job-1" and enq.await_args.kwargs["idempotency_key"] == \
        "ops_alert_email:inquiry_handoff_gap:2026-10-30"
