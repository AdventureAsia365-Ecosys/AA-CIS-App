"""S279 thresholds L1 — the two settings e-mails of shared/ops_alerts.py: a self-approved high-impact change (the tech
lead's second pair of eyes, dev-phase rule Q2) and a stored value the reader refused. Rendering, the idempotency keys
(one e-mail per change; one per key and UTC day), the worker's renderer table, and nothing queued when e-mail is not
configured. No DB, no SES: fakes only."""
from datetime import datetime, timezone

import pytest

from services.jobs import ops_alert_email_job
from shared import ops_alerts


def test_renderers_are_registered():
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_SETTING_SELF_APPROVED] is \
        ops_alerts.render_setting_self_approved
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_SETTING_OUT_OF_BOUNDS] is \
        ops_alerts.render_setting_out_of_bounds


def test_self_approved_message_names_the_change():
    msg = ops_alerts.render_setting_self_approved(
        {"key": "cis.publish.daily_limit_default", "label": "Daily publish limit", "before": 20, "after": 12,
         "actor": "admin:zz", "reason": "zz lower the default for the trial", "effective_from": None},
        to="ops@example.test", base_url="https://admin.example.test", environment="dev")
    assert msg.subject == "[AA-CIS dev] Setting self-approved: cis.publish.daily_limit_default"
    assert "Before: 20" in msg.text and "After: 12" in msg.text and "zz lower the default" in msg.text
    assert "https://admin.example.test/admin/platform/settings" in msg.text and "<table" in msg.html


def test_out_of_bounds_message():
    msg = ops_alerts.render_setting_out_of_bounds(
        {"key": "cis.t9.max_attempts", "problem": "value is above the maximum 3", "value": "9", "version": 4,
         "period": "2026-11-13"}, to="ops@example.test", base_url="https://admin.example.test", environment="dev")
    assert "cis.t9.max_attempts" in msg.subject and "above the maximum 3" in msg.text and "default" in msg.text


class _Queue:
    def __init__(self):
        self.calls = []

    async def enqueue(self, conn, kind, payload, **kw):
        self.calls.append((kind, payload, kw))
        return "job-1", True


@pytest.mark.asyncio
async def test_enqueue_keys(monkeypatch):
    from shared.jobs import queue
    fake = _Queue()
    monkeypatch.setattr(queue, "enqueue", fake.enqueue)
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: object())
    assert await ops_alerts.enqueue_setting_self_approved_email(None, details={"key": "cis.a.b", "value_id": "v1"}) \
        == "job-1"
    day = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)
    assert await ops_alerts.enqueue_setting_alert_email(None, details={"key": "cis.a.b"}, now=day) == "job-1"
    keys = [c[2]["idempotency_key"] for c in fake.calls]
    assert keys[0].endswith(":setting_self_approved:v1")
    assert keys[1].endswith(":setting_out_of_bounds:cis.a.b:2026-11-13")
    monkeypatch.setattr(ops_alerts, "sender_config", lambda: None)
    assert await ops_alerts.enqueue_setting_self_approved_email(None, details={"key": "cis.a.b"}) is None
    assert await ops_alerts.enqueue_setting_alert_email(None, details={"key": "cis.a.b"}) is None
