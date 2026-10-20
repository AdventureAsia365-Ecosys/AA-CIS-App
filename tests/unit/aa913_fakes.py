"""AA-913 test fakes: an in-memory shared.alert_recipient + acp_shared.audit_log + shared.job (answering exactly the
SQL constants of shared/alert_recipients.py and api/services/alert_recipients.py), on top of the staff directory fake
(tests/unit/aa854_cognito_staff.Directory) so the routes can be called with a real Cognito Bearer. A stub SNS client
keeps the topic's e-mail subscriptions. The topic ARN is a made-up example (AWS doc account id)."""
from __future__ import annotations

import copy
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from api.services import alert_recipients as svc
from shared import alert_recipients as ar
from tests.unit.aa854_cognito_staff import Directory

TOPIC_ARN = "arn:aws:sns:us-west-1:111122223333:aa-cis-dev-acp-alerts"
BOOTSTRAP = "boot@adventure.asia"


def sub_arn(tag: str) -> str:
    return f"{TOPIC_ARN}:{uuid.uuid5(uuid.NAMESPACE_URL, tag)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlertDB(Directory):
    def __init__(self) -> None:
        super().__init__()
        self.recipients: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.locks = 0
        self.fail_reads = False

    # ── helpers for tests ──
    def insert(self, email: str, status: str = "pending", *, arn: Optional[str] = None, reason: Optional[str] = None,
               created_by: str = "admin:seed", sent: Optional[datetime] = None, note: Optional[str] = None) -> str:
        rid = str(uuid.uuid4())
        now = _now()
        self.recipients.append({
            "id": rid, "email": email, "note": note, "status": status,
            "removed_reason": reason if status == "removed" else None, "subscription_arn": arn,
            "created_at": now - timedelta(hours=1) + timedelta(microseconds=len(self.recipients)),
            "created_by": created_by,
            "confirmed_at": now if status == "confirmed" else None,
            "removed_at": now if status == "removed" else None,
            "removed_by": created_by if status == "removed" else None,
            "last_confirmation_sent_at": sent if sent is not None else (now - timedelta(hours=1)),
            "last_seen_state": None, "last_reconciled_at": None, "updated_at": now,
        })
        return rid

    def row(self, rid: str) -> dict[str, Any]:   # type: ignore[override]
        r = next((r for r in self.recipients if r["id"] == rid), None)
        if r is not None:
            return r
        return super().row(rid)

    def audit_at(self, action: str, *, resource_id: Optional[str] = None, ago: timedelta = timedelta(0)) -> None:
        self.audit.append({"tenant_id": None, "actor": "admin:seed", "action": action,
                           "resource_type": ar.RESOURCE_TYPE, "resource_id": resource_id or "x",
                           "details": {}, "created_at": _now() - ago})

    @asynccontextmanager
    async def transaction(self):
        snapshot = (copy.deepcopy(self.recipients), list(self.audit), list(self.jobs))
        try:
            yield
        except BaseException:
            self.recipients, self.audit, self.jobs = snapshot
            raise

    # ── views ──
    def _page(self, r: dict[str, Any]) -> dict[str, Any]:
        names = {f"admin:{a['id']}": a.get("display_name") or a.get("email") or a.get("username") for a in self.rows}
        out = {k: r[k] for k in ("id", "email", "note", "status", "removed_reason", "created_at", "created_by",
                                 "confirmed_at", "removed_at", "removed_by", "last_confirmation_sent_at",
                                 "last_seen_state", "last_reconciled_at")}
        out["has_subscription"] = r["subscription_arn"] is not None
        out["created_by_name"] = names.get(r["created_by"])
        out["removed_by_name"] = names.get(r["removed_by"] or "")
        return out

    def _row5(self, r: dict[str, Any]) -> dict[str, Any]:
        return {k: r[k] for k in ("id", "email", "status", "removed_reason", "subscription_arn", "confirmed_at",
                                  "last_confirmation_sent_at", "created_at")}

    def _active(self) -> list[dict[str, Any]]:
        return sorted((r for r in self.recipients if r["status"] != "removed"), key=lambda r: r["created_at"])

    def _find(self, rid: Any) -> Optional[dict[str, Any]]:
        return next((r for r in self.recipients if r["id"] == str(rid)), None)

    def _recent(self, action: str, resource_type: str, secs: float, resource_id: Optional[str] = None) -> dict:
        now = _now()
        rows = [a for a in self.audit if a["action"] == action and a["resource_type"] == resource_type
                and a["created_at"] > now - timedelta(seconds=secs)
                and (resource_id is None or a["resource_id"] == resource_id)]
        times = [a["created_at"] for a in rows]
        return {"n": len(rows), "first_at": min(times) if times else None, "last_at": max(times) if times else None,
                "now": now}

    # ── asyncpg surface ──
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if self.fail_reads and "alert_recipient" in sql:
            raise OSError("RDS unreachable")
        if sql in (ar.SQL_ACTIVE_FOR_UPDATE, ar.SQL_LATEST_REMOVED, ar.SQL_CONFIRMED_EMAILS, ar.SQL_LIST_ACTIVE,
                   ar.SQL_LIST_HISTORY):
            self.sql.append(sql)
        if sql == ar.SQL_ACTIVE_FOR_UPDATE:
            return [self._row5(r) for r in self._active()]
        if sql == ar.SQL_LATEST_REMOVED:
            latest: dict[str, dict] = {}
            for r in self.recipients:
                if r["status"] == "removed" and (r["email"] not in latest
                                                  or r["removed_at"] > latest[r["email"]]["removed_at"]):
                    latest[r["email"]] = r
            return [self._row5(r) for r in latest.values()]
        if sql == ar.SQL_CONFIRMED_EMAILS:
            return [{"email": r["email"]} for r in self._active() if r["status"] == "confirmed"]
        if sql == ar.SQL_LIST_ACTIVE:
            return [self._page(r) for r in self._active()]
        if sql == ar.SQL_LIST_HISTORY:
            removed = sorted((r for r in self.recipients if r["status"] == "removed"),
                             key=lambda r: r["removed_at"], reverse=True)
            return [self._page(r) for r in removed[:ar.HISTORY_LIMIT]]
        return await super().fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql == ar.SQL_GET_ONE:
            self.sql.append(sql)
            r = self._find(args[0])
            return self._page(r) if r else None
        if sql == svc.SQL_ROW_FOR_UPDATE:
            self.sql.append(sql)
            r = self._find(args[0])
            return {k: r[k] for k in ("id", "email", "status", "subscription_arn")} if r else None
        if sql == svc.SQL_RECENT_ACTION:
            return self._recent(args[0], args[1], args[2])
        if sql == svc.SQL_RECENT_ACTION_FOR:
            return self._recent(args[0], args[1], args[2], args[3])
        if "INSERT INTO shared.job" in sql:
            kind, payload, key = args[0], json.loads(args[1]), args[2]
            if key and any(j["idempotency_key"] == key for j in self.jobs):
                return None
            jid = str(uuid.uuid4())
            self.jobs.append({"id": jid, "kind": kind, "payload": payload, "idempotency_key": key,
                              "created_by": args[5]})
            return {"id": jid}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if sql == svc.SQL_ACTIVE_BY_EMAIL:
            return next((r["id"] for r in self._active() if r["email"] == args[0]), None)
        if sql == svc.SQL_COUNT_ACTIVE:
            return len(self._active())
        if sql == svc.SQL_INSERT:
            email, note, status, arn, created_by = args
            if any(r["email"] == email for r in self._active()):
                raise AssertionError("unique violation: one active row per address")
            rid = self.insert(email, status, arn=arn, created_by=created_by, note=note,
                              sent=_now() if status == "pending" else None)
            row = self._find(rid)
            row["created_at"] = _now()
            if status != "pending":
                row["last_confirmation_sent_at"] = None
            return rid
        if sql.startswith("SELECT id FROM shared.job WHERE idempotency_key"):
            return next(j["id"] for j in self.jobs if j["idempotency_key"] == args[0])
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO acp_shared.audit_log" in sql:
            self.sql.append(sql)
            tenant_id, actor, action, resource_type, resource_id, details = args[:6]
            self.audit.append({"tenant_id": tenant_id, "actor": actor, "action": action,
                               "resource_type": resource_type, "resource_id": resource_id,
                               "details": json.loads(details), "created_at": _now()})
            return "INSERT 0 1"
        self.sql.append(sql)
        if sql == svc.SQL_LOCK:
            self.locks += 1
            return "SELECT 1"
        if sql == ar.SQL_RECONCILE_UPDATE:
            rid, status, reason, removed_by, arn, seen = args
            r = self._find(rid)
            r.update(status=status, removed_reason=reason,
                     removed_at=(r["removed_at"] or _now()) if status == "removed" else None,
                     removed_by=removed_by if status == "removed" else None,
                     subscription_arn=arn or r["subscription_arn"],
                     confirmed_at=(r["confirmed_at"] or _now()) if status == "confirmed" else r["confirmed_at"],
                     last_seen_state=seen, last_reconciled_at=_now(), updated_at=_now())
            return "UPDATE 1"
        if sql == ar.SQL_RECONCILE_SEEN:
            self._find(args[0]).update(last_seen_state=args[1], last_reconciled_at=_now())
            return "UPDATE 1"
        if sql == svc.SQL_MARK_REMOVED:
            self._find(args[0]).update(status="removed", removed_reason="admin", removed_at=_now(),
                                       removed_by=args[1], updated_at=_now())
            return "UPDATE 1"
        if sql == svc.SQL_MARK_RESENT:
            r = self._find(args[0])
            r.update(last_confirmation_sent_at=_now(), subscription_arn=args[1] or r["subscription_arn"])
            return "UPDATE 1"
        return await super().execute(sql, *args)

    def actions(self, prefix: str = "alert_recipient.") -> list[str]:
        return [a["action"] for a in self.audit if a["action"].startswith(prefix)]


class SnsClientError(Exception):
    """botocore-shaped: .response["Error"]["Code"]."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code, message or TOPIC_ARN)   # like botocore, the raw message may carry the ARN
        self.response = {"Error": {"Code": code, "Message": message}}


class StubSns:
    """The alerts topic's e-mail subscriptions: endpoint -> real ARN, or "PendingConfirmation"."""

    def __init__(self, subs: Optional[dict[str, str]] = None, *, list_error: Optional[str] = None,
                 subscribe_error: Optional[str] = None, unsubscribe_error: Optional[str] = None,
                 attributes_error: Optional[str] = None) -> None:
        self.subs: dict[str, str] = dict(subs or {})
        self.pending_arns: dict[str, str] = {}   # ARN returned for a pending subscription -> endpoint
        self.list_error, self.subscribe_error = list_error, subscribe_error
        self.unsubscribe_error, self.attributes_error = unsubscribe_error, attributes_error
        self.list_calls = 0
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def list_subscriptions_by_topic(self, TopicArn: str, NextToken: Optional[str] = None) -> dict:
        assert TopicArn == TOPIC_ARN
        self.list_calls += 1
        if self.list_error:
            raise SnsClientError(self.list_error)
        return {"Subscriptions": [{"SubscriptionArn": arn, "Protocol": "email", "Endpoint": email,
                                   "TopicArn": TopicArn} for email, arn in self.subs.items()]}

    def subscribe(self, TopicArn: str, Protocol: str, Endpoint: str, ReturnSubscriptionArn: bool) -> dict:
        assert TopicArn == TOPIC_ARN and Protocol == "email" and ReturnSubscriptionArn is True
        if self.subscribe_error:
            raise SnsClientError(self.subscribe_error)
        self.subscribed.append(Endpoint)
        existing = self.subs.get(Endpoint)
        if existing and existing.startswith("arn:"):
            return {"SubscriptionArn": existing}
        arn = sub_arn(f"pending:{Endpoint}")
        self.subs[Endpoint] = "PendingConfirmation"
        self.pending_arns[arn] = Endpoint
        return {"SubscriptionArn": arn}

    def confirm(self, endpoint: str) -> str:
        """The recipient clicks the link."""
        arn = next(a for a, e in self.pending_arns.items() if e == endpoint)
        self.subs[endpoint] = arn
        return arn

    def _endpoint(self, arn: str) -> Optional[str]:
        for email, value in self.subs.items():
            if value == arn or (value == "PendingConfirmation" and self.pending_arns.get(arn) == email):
                return email
        return None

    def get_subscription_attributes(self, SubscriptionArn: str) -> dict:
        if self.attributes_error:
            raise SnsClientError(self.attributes_error)
        email = self._endpoint(SubscriptionArn)
        if email is None:
            raise SnsClientError("NotFound")
        return {"Attributes": {"PendingConfirmation": "true" if self.subs[email] == "PendingConfirmation"
                               else "false"}}

    def unsubscribe(self, SubscriptionArn: str) -> dict:
        if self.unsubscribe_error:
            raise SnsClientError(self.unsubscribe_error)
        email = self._endpoint(SubscriptionArn)
        if email is None:
            raise SnsClientError("NotFound")
        del self.subs[email]
        self.unsubscribed.append(SubscriptionArn)
        return {}
