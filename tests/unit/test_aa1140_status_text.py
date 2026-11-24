"""AA-1140 (S289 live check, Vietnamese mode screenshot 24/11) — the text of the Ecosystem status page:

1. outside free text (CloudWatch alarm descriptions, ops check messages, ECS reasons, job errors) reaches the page
   without tracker IDs (AA-942, "(CRM-14, V1)", KAN-20, PR-27, CON-3) and is never cut mid-word on the server;
   a runtime check walks every string of the built verdict and timeline;
2. every attention item and event carries a structured ``msg`` the page words in EN or VI, and every key the
   backend emits has a template on the page (lib/opsStatusText.ts ``MSG_KEYS``);
3. the free storage tile judges free space as a share of the allocated storage (CIS 477) with the GiB pair as the
   fallback, and the sampler reads the size with one DescribeDBInstances. Invented names only; no DB, no AWS."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from api.services import issue_refs
from api.services.ops_status import sampler, sections, verdict
from shared import ops_status_thresholds as th

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 24, 2, 7, 30, tzinfo=timezone.utc)
TRACKER_ID = re.compile(r"(?<![A-Za-z0-9-])(?:AA|CRM|KAN|PR|CON)-\d+")
LONG = ("zz-db holds a backup copy older than 8 days (a manual snapshot that the copy job did not replace). The "
        "gateway of the copy job is checked first, then its log group; a copy older than the window breaks the "
        "restore objective until a fresh copy lands.")


# ── 1. issue IDs and word-boundary cuts ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("AA-942 (CRM-14, V1): zz-db holds a copy.", "zz-db holds a copy."),
    ("AA-942 — snapshots before this day hold no zz data.", "Snapshots before this day hold no zz data."),
    ("A burst of refusals. AA-703.", "A burst of refusals."),
    ("p95 over 2 s (latency SLO, AA-969).", "p95 over 2 s (latency SLO)."),
    ("Copy age above the window (CRM-14, V1).", "Copy age above the window."),
    ("KAN-20: compare with the benchmark.", "Compare with the benchmark."),
    ("Report the progress on PR-27 and CON-3 later.", "Report the progress on and later."),
    ("AA-Booking API: more than 5 % answered 5xx.", "AA-Booking API: more than 5 % answered 5xx."),
    ("Runbook CIS-08 for EVENT-0087 (V1).", "Runbook CIS-08 for EVENT-0087 (V1)."),
    ("", ""),
])
def test_strip_issue_refs(raw, expected):
    assert issue_refs.strip_issue_refs(raw) == expected


def test_clip_words_never_cuts_mid_word():
    out = issue_refs.clip_words(LONG, 120)
    assert len(out) <= 120 and out.endswith("…")
    assert LONG.startswith(out[:-1]) and LONG[len(out) - 1] in " ,.;"   # the cut sits on a word boundary
    assert issue_refs.clip_words("short", 120) == "short"
    assert issue_refs.clean("AA-1: ", 50) is None


def _alarm_event(detail: str) -> dict:
    return {"event_id": 7, "occurred_at": NOW - timedelta(hours=1), "ended_at": None, "kind": "alarm",
            "severity": "sev3", "app": "shared", "subject": "zz-copy-age-zz-db", "title": "Alarm: zz-copy-age-zz-db",
            "detail": detail, "details": {"severity_tag": "ticket"}}


def test_an_alarm_description_reaches_the_page_whole_and_without_ids():
    row = sections.event_rows([_alarm_event("AA-942 (CRM-14, V1): " + LONG)], [], [])[0]
    assert row["detail"] == LONG and row["detail_source"] is True
    assert row["msg"] == {"key": "alarm", "name": "zz-copy-age-zz-db"}
    item = verdict.alarm_items([row])[0]
    assert item["detail"] == LONG and item["detail_source"] is True and item["msg"]["key"] == "alarm"


def test_the_sampler_keeps_the_whole_description_without_ids():
    alarms = {"shared": {"firing": [{"name": "zz", "severity": "ticket", "since": NOW.isoformat(),
                                     "description": "AA-942 (CRM-14, V1): " + LONG}]}}
    assert sampler.alarm_firing(alarms)[0]["description"] == LONG


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)


def test_no_tracker_id_in_any_string_the_verdict_and_timeline_send():
    # Every outside source carries IDs; the built output must carry none (the class the S289 screenshot showed).
    ev = [_alarm_event("AA-942 (CRM-14, V1): " + LONG),
          {**_alarm_event("Rollout stopped. AA-12."), "event_id": 8, "kind": "rollout_failed",
           "title": "Rollout failed: zz-api (rev 4)", "subject": "zz-api", "details": {"revision": "4"}}]
    bells = [{"id": 9, "event_type": "platform.ops_check.jobs_starved", "entity_id": "jobs_starved",
              "payload": json.dumps({"title": "KAN-20: Job queue starved", "message": "zz waited (PR-27, V2)."}),
              "is_read": False, "created_at": NOW - timedelta(minutes=10)}]
    fails = [{"kind": "zz_kind", "hour": NOW - timedelta(hours=2), "n": 2, "last_error": "CON-3: zz timeout"}]
    rows = sections.event_rows(ev, bells, fails)
    apps = {"status": "ok", "data": [{"app": "crm", "label": "Partner CRM",
                                      "health": {"state": "down", "reason": "zz (AA-1, V1) refused"},
                                      "traffic": {"status": "not_measured"}}]}
    secs = {"signals": {"status": "ok"}, "apps": apps,
            "runtimes": {"status": "ok", "data": {"rows": []}}, "jobs": {"status": "ok", "data": {"queues": []}},
            "resources": {"status": "ok", "data": {"tiles": []}}, "ai": {"status": "ok", "data": {"apps": []}}}
    out = verdict.decide(secs, th.snapshot(), rows)
    found = [s for s in _strings({"verdict": out, "events": rows}) if TRACKER_ID.search(s)]
    assert not found, found


# ── 2. structured messages ─────────────────────────────────────────────────────────────────────────────────────

def _frontend_keys() -> set[str]:
    src = (REPO / "frontend" / "lib" / "opsStatusText.ts").read_text(encoding="utf-8")
    block = re.search(r"export const MSG_KEYS = \[(.*?)\] as const;", src, re.S)
    assert block, "lib/opsStatusText.ts must list MSG_KEYS"
    return set(re.findall(r'"([a-z0-9_]+)"', block.group(1)))


def _all_items() -> list[dict]:
    snap = th.snapshot()
    apps = [{"app": "cis", "label": "CIS", "health": {"state": "degraded", "reason": "zz"},
             "traffic": {"status": "ok", "requests": 900, "errors": 90, "client_errors": 200, "error_rate_pct": 10.0,
                         "client_error_rate_pct": 22.2, "p95_ms": 4000, "errors_level": "crit", "p95_level": "crit",
                         "client_errors_level": "warn"}},
            {"app": "booking", "label": "Booking", "health": {"state": "down"}, "traffic": {"status": "x"}}]
    rows = [{"kind": "ecs_service", "subject": "zz-api", "app": "cis", "state": "down", "reason": "0 of 1 running",
             "reason_code": "none_running", "running": 0, "desired": 1}]
    queues = [{"id": "a", "name": "shared.job", "app": "cis", "status": "ok", "workers": 0},
              {"id": "b", "name": "crm.job", "app": "crm", "status": "ok", "level": "warn", "oldest_wait_min": 20}]
    tiles = [{"id": "ecs", "label": "ECS tasks running", "level": "warn", "latest": 3, "capacity": 4},
             {"id": "cpu", "label": "Database CPU", "level": "crit", "latest": 95, "unit": "%", "warn": 70,
              "crit": 90, "dir": "above"}]
    ai = [{"app": "cis", "level": "crit", "spent_usd": 5, "cap_usd": 5},
          {"app": "crm", "level": "warn", "spent_usd": 4, "cap_usd": 5, "used_pct": 80}]
    deliv = {"guards": [{"key": "a", "label": "zz probe", "state": "failed", "value": 2, "day": "2026-11-23"},
                        {"key": "b", "label": "zz guard", "state": "missing", "age_h": 40}],
             "repos": [{"id": "cis", "label": "CIS", "main": "red", "red_hours": 3}]}
    ev = sections.event_rows([_alarm_event("zz")], [{"id": 1, "entity_id": "jobs_starved", "is_read": False,
                                                     "payload": {"title": "zz"}, "created_at": NOW}], [])
    return (verdict.app_items(apps, snap) + verdict.runtime_items(rows) + verdict.queue_items(queues, snap)
            + verdict.resource_items(tiles) + verdict.ai_items(ai) + verdict.delivery_items(deliv)
            + verdict.alarm_items(ev) + verdict.check_items(ev))


def test_every_item_has_a_message_the_page_can_word():
    known = _frontend_keys()
    items = _all_items()
    keys = {i["msg"]["key"] for i in items} | {i["detail_msg"]["key"] for i in items if i["detail_msg"]}
    assert "other" not in keys and keys <= known, keys - known
    assert {"errors", "p95", "client_errors", "health_down", "health_degraded", "service", "queue_no_worker",
            "queue_wait", "ecs_tasks", "resource", "ai_cap", "ai_near", "guard_failed", "guard_missing", "main_red",
            "alarm", "check", "of_requests", "ai_spend", "guard_value", "guard_age", "red_for"} <= keys
    errors = next(i for i in items if i["id"] == "errors-cis")
    assert errors["msg"] == {"key": "errors", "app": "cis", "value": 10.0, "limit": 5.0, "unit": "%"}
    assert errors["title"] == "CIS: 10 % server errors, limit 5 %"   # the English fallback stays


def test_every_event_has_a_message_the_page_can_word():
    known = _frontend_keys()
    ev = [{"event_id": 1, "occurred_at": NOW, "ended_at": NOW, "kind": "deploy", "severity": "info", "app": "cis",
           "subject": "zz-api", "title": "Deploy zz-api dev-abc (rev 9)", "detail": "api: rollout completed",
           "details": {"revision": "9", "image_tag": "dev-abc", "rollout": "COMPLETED"}},
          {"event_id": 2, "occurred_at": NOW, "ended_at": None, "kind": "service_down", "severity": "sev1",
           "app": "cis", "subject": "zz-worker", "title": "zz-worker: 0 of 2 tasks running", "detail": None,
           "details": {}}]
    rows = sections.event_rows(ev, [], [{"kind": "zz", "hour": NOW, "n": 1, "last_error": None}])
    assert {r["msg"]["key"] for r in rows} <= known
    down = next(r for r in rows if r["kind"] == "service_down")
    assert down["msg"] == {"key": "service_down", "subject": "zz-worker", "running": 0, "desired": 2}
    deploy = next(r for r in rows if r["kind"] == "deploy")
    assert deploy["msg"]["tag"] == "dev-abc" and deploy["detail"] is None and deploy["detail_source"] is False
    assert sections.service_down_msg("zz", "zz: scaled to zero")["zero"] is True


@pytest.mark.parametrize("v, code", [
    ({"desired": 0, "running": 0}, "scaled_to_zero"), ({"desired": 2, "running": 0}, "none_running"),
    ({"desired": 2, "running": 2, "rollout": "FAILED"}, "rollout_failed"), ({"desired": 2, "running": 1},
                                                                            "below_desired"),
    ({"desired": 1, "running": 1, "rollout": "IN_PROGRESS"}, "rollout_in_progress"),
    ({"desired": 1, "running": 1, "rollout": "COMPLETED"}, "running"), ({}, None),
])
def test_service_reason_code(v, code):
    assert sections.service_reason_code("ecs_service", v) == code
    assert sections.service_reason_code("app_health", v) is None


# ── 3. free storage as a share of the allocated storage ────────────────────────────────────────────────────────

def _tile(tid, label, unit, values, lim, agg, note=None, source="cloudwatch"):
    latest = next((v for v in reversed(values) if v is not None), None)
    return {"id": tid, "label": label, "unit": unit, "series": values, "latest": latest, "warn": lim.get("warn"),
            "crit": lim.get("crit"), "dir": lim.get("dir"), "warn_key": lim.get("warn_key"),
            "crit_key": lim.get("crit_key"),
            "level": sections.level(latest, lim.get("warn"), lim.get("crit"), below=lim.get("dir") == "below")}


def test_free_storage_on_a_small_autoscaling_volume_is_not_an_alarm():
    # Dev: 20 GiB allocated (autoscaling to 100), 14.83 GiB free for weeks; the old 15 GiB limit raised a SEV3
    t = sections.free_storage_tile(_tile, [14.9, 14.83], {"allocated_gib": 20, "max_allocated_gib": 100})
    assert (t["level"], t["basis"], t["warn"], t["crit"]) == ("ok", "pct", 4.0, 2.0)
    assert (t["warn_pct"], t["crit_pct"], t["allocated_gib"], t["max_allocated_gib"], t["latest_pct"]) == (
        20.0, 10.0, 20.0, 100, 74.2)
    assert t["warn_key"] == th.RDS_FREE_WARN_PCT and not verdict.resource_items([t])


def test_free_storage_low_share_is_an_item_with_the_share_and_its_gib():
    t = sections.free_storage_tile(_tile, [3.1], {"allocated_gib": 20})
    t["label"] = "Free storage"
    item = verdict.resource_items([t])[0]
    assert item["severity"] == "sev3"
    assert item["msg"] == {"key": "resource", "tile": "free_storage", "value": 3.1, "unit": "GiB", "limit": 4.0,
                           "dir": "below", "level": "warn", "value_pct": 15.5, "limit_pct": 20.0,
                           "allocated_gib": 20.0}
    assert item["title"] == ("Free storage 3.1 GiB (15.5 % of 20 GiB), at or below the warn limit 20 % (4 GiB)")


def test_free_storage_falls_back_to_gib_while_the_size_is_unknown():
    for db in (None, {}, {"allocated_gib": None}):
        t = sections.free_storage_tile(_tile, [14.83], db)
        assert (t["basis"], t["warn"], t["level"], t["latest_pct"]) == ("gib", 15.0, "warn", None)


class _Rds:
    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, []

    def describe_db_instances(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("zz refused")
        return {"DBInstances": [{"AllocatedStorage": 20, "MaxAllocatedStorage": 100}]}


def test_the_sampler_reads_the_allocated_storage_once_and_survives_a_refusal():
    ok = _Rds()
    assert sampler.rds_storage("dev", ok) == {"allocated_gib": 20, "max_allocated_gib": 100}
    assert ok.calls == [{"DBInstanceIdentifier": "aa-cis-dev-db"}]
    assert sampler.rds_storage("dev", _Rds(fail=True)) == {"allocated_gib": None, "max_allocated_gib": None}
