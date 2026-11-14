"""SQL of the AA Academy learning API on schema learn (CIS 420, 421; AA-1105). Table and column names: root
docs/architecture/db-schema-reference.md, section ``learn``.

One :class:`PgStore` per request, on a connection of the API pool (``cis_app``: the ``<table>_cis_app_all`` policies)
inside the request's transaction. Every child row carries its learner's tenant (CIS 421 refuses anything else): staff
learners have none, so every write passes the learner's own ``tenant_id``. The unit tests use an in-memory twin with
the same methods (tests/unit/aa1105_learn_fake.py); tests/integration/test_aa1105_learn_store.py runs this one.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

STAFF_SQL = (
    "SELECT id::text AS admin_id, email, display_name, role::text AS db_role FROM shared.admin_users "
    "WHERE cognito_iss = $1 AND cognito_sub = $2 AND is_active")
LEARNER_COLS = "learner_id::text AS learner_id, kind, tenant_id::text AS tenant_id, display_name, is_test"
CERT_COLS = ("certificate_id::text AS certificate_id, learner_id::text AS learner_id, path_id, path_title, status, "
             "issued_at, expires_at, module_versions")


def _row(record: Any) -> Optional[dict]:
    if record is None:
        return None
    row = dict(record)
    for key in ("module_versions", "responses", "card", "result", "context"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


class PgStore:
    """The learn tables for one request's connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # ── who ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def staff_row(self, iss: str, sub: str) -> Optional[dict]:
        return _row(await self.conn.fetchrow(STAFF_SQL, iss, sub))

    async def upsert_learner(self, iss: str, sub: str, admin_id: str, display_name: Optional[str],
                             is_test: bool) -> dict:
        """The staff learner of (iss, sub): created on first call, its directory link, name and test flag refreshed
        and its retention clock stopped on every call (ADR 0022 decision 7)."""
        return _row(await self.conn.fetchrow(
            "INSERT INTO learn.learner (iss, sub, kind, admin_user_id, display_name, is_test) "
            "VALUES ($1, $2, 'staff', $3::uuid, $4, $5) "
            "ON CONFLICT (iss, sub) DO UPDATE SET admin_user_id = EXCLUDED.admin_user_id, "
            "display_name = EXCLUDED.display_name, is_test = EXCLUDED.is_test, last_seen_at = now(), "
            f"inactive_since = NULL RETURNING {LEARNER_COLS}", iss, sub, admin_id, display_name, is_test))

    # ── statements and progress ──────────────────────────────────────────────────────────────────────────────────────
    async def statement(self, learner: dict, *, verb: str, object_type: str, object_id: str,
                        statement_id: Optional[str] = None, body_sha256: str, module_version: Optional[str] = None,
                        bundle_sha: Optional[str] = None, result: Optional[dict] = None,
                        context: Optional[dict] = None) -> str:
        """Append one statement: ``created``, or for a known statement id ``same`` (this learner, same body) or
        ``conflict``."""
        sid = statement_id or str(uuid.uuid4())
        created = await self.conn.fetchval(
            "INSERT INTO learn.statement (statement_id, learner_id, tenant_id, verb, object_type, object_id, "
            "module_version, bundle_sha, result, context, body_sha256) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, "
            "$6, $7, $8, $9::jsonb, $10::jsonb, $11) ON CONFLICT (statement_id) DO NOTHING RETURNING true AS ok",
            sid, learner["learner_id"], learner["tenant_id"], verb, object_type, object_id, module_version,
            bundle_sha, json.dumps(result or {}), json.dumps(context or {}), body_sha256)
        if created:
            return "created"
        same = await self.conn.fetchval(
            "SELECT true AS ok FROM learn.statement WHERE statement_id = $1::uuid AND learner_id = $2::uuid "
            "AND body_sha256 = $3", sid, learner["learner_id"], body_sha256)
        return "same" if same else "conflict"

    async def progress(self, learner_id: str) -> dict[str, dict]:
        rows = await self.conn.fetch(
            "SELECT lesson_id, module_id, module_version, status, lang, completed_at FROM learn.lesson_progress "
            "WHERE learner_id = $1::uuid", learner_id)
        return {r["lesson_id"]: dict(r) for r in rows}

    async def set_progress(self, learner: dict, lesson_id: str, module_id: str, module_version: str, lang: str,
                           completed: bool) -> str:
        """Open (or complete) a lesson; a completed lesson stays completed. Returns the stored status."""
        return await self.conn.fetchval(
            "INSERT INTO learn.lesson_progress (learner_id, tenant_id, lesson_id, module_id, module_version, status, "
            "lang, completed_at) VALUES ($1::uuid, $2::uuid, $3, $4, $5, CASE WHEN $6 THEN 'completed' ELSE 'opened' "
            "END, $7, CASE WHEN $6 THEN now() END) ON CONFLICT (learner_id, lesson_id) DO UPDATE SET "
            "status = CASE WHEN learn.lesson_progress.status = 'completed' OR $6 THEN 'completed' ELSE 'opened' END, "
            "completed_at = COALESCE(learn.lesson_progress.completed_at, CASE WHEN $6 THEN now() END), "
            "module_version = EXCLUDED.module_version, lang = EXCLUDED.lang, updated_at = now() "
            "RETURNING status", learner["learner_id"], learner["tenant_id"], lesson_id, module_id, module_version,
            completed, lang)

    # ── enrollment ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def enrollments(self, learner_id: str) -> dict[str, dict]:
        rows = await self.conn.fetch(
            "SELECT path_id, status, enrolled_at, completed_at FROM learn.enrollment WHERE learner_id = $1::uuid",
            learner_id)
        return {r["path_id"]: dict(r) for r in rows}

    async def enroll(self, learner: dict, path_id: str) -> tuple[dict, bool]:
        """(enrollment, created). A withdrawn enrollment becomes active again."""
        row = await self.conn.fetchrow(
            "INSERT INTO learn.enrollment (learner_id, tenant_id, path_id) VALUES ($1::uuid, $2::uuid, $3) "
            "ON CONFLICT (learner_id, path_id) DO NOTHING RETURNING path_id, status, enrolled_at, completed_at",
            learner["learner_id"], learner["tenant_id"], path_id)
        if row is not None:
            return dict(row), True
        row = await self.conn.fetchrow(
            "UPDATE learn.enrollment SET status = 'active' WHERE learner_id = $1::uuid AND path_id = $2 "
            "AND status = 'withdrawn' RETURNING path_id, status, enrolled_at, completed_at",
            learner["learner_id"], path_id)
        if row is None:
            row = await self.conn.fetchrow(
                "SELECT path_id, status, enrolled_at, completed_at FROM learn.enrollment "
                "WHERE learner_id = $1::uuid AND path_id = $2", learner["learner_id"], path_id)
        return dict(row), False

    async def complete_enrollment(self, learner: dict, path_id: str) -> None:
        await self.conn.execute(
            "INSERT INTO learn.enrollment (learner_id, tenant_id, path_id, status, completed_at) "
            "VALUES ($1::uuid, $2::uuid, $3, 'completed', now()) ON CONFLICT (learner_id, path_id) DO UPDATE SET "
            "status = 'completed', completed_at = COALESCE(learn.enrollment.completed_at, now())",
            learner["learner_id"], learner["tenant_id"], path_id)

    # ── quiz attempts ────────────────────────────────────────────────────────────────────────────────────────────────
    async def submitted_attempts(self, learner_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT scope, target_id, passed, score_pct::float8 AS score_pct, submitted_at, module_versions "
            "FROM learn.quiz_attempt WHERE learner_id = $1::uuid AND status = 'submitted' ORDER BY submitted_at",
            learner_id)
        return [_row(r) for r in rows]

    async def start_attempt(self, learner: dict, scope: str, target_id: str, item_ids: list[str],
                            module_versions: dict, bundle_sha: str, pass_mark_pct: int) -> dict:
        """Expire a stale open attempt of the same target (one open attempt at a time), then open a new one."""
        await self.conn.execute(
            "UPDATE learn.quiz_attempt SET status = 'expired' WHERE learner_id = $1::uuid AND scope = $2 "
            "AND target_id = $3 AND status = 'open'", learner["learner_id"], scope, target_id)
        return _row(await self.conn.fetchrow(
            "INSERT INTO learn.quiz_attempt (learner_id, tenant_id, scope, target_id, item_ids, module_versions, "
            "bundle_sha, pass_mark_pct) VALUES ($1::uuid, $2::uuid, $3, $4, $5::text[], $6::jsonb, $7, $8) "
            "RETURNING attempt_id::text AS attempt_id, started_at", learner["learner_id"], learner["tenant_id"],
            scope, target_id, item_ids, json.dumps(module_versions), bundle_sha, pass_mark_pct))

    async def attempt_for_update(self, learner_id: str, attempt_id: str) -> Optional[dict]:
        return _row(await self.conn.fetchrow(
            "SELECT attempt_id::text AS attempt_id, scope, target_id, item_ids, module_versions, bundle_sha, "
            "pass_mark_pct, status FROM learn.quiz_attempt WHERE attempt_id = $1::uuid AND learner_id = $2::uuid "
            "FOR UPDATE", attempt_id, learner_id))

    async def submit_attempt(self, attempt_id: str, responses: dict, score_pct: float, passed: bool) -> datetime:
        return await self.conn.fetchval(
            "UPDATE learn.quiz_attempt SET status = 'submitted', submitted_at = now(), responses = $2::jsonb, "
            "score_pct = $3, passed = $4 WHERE attempt_id = $1::uuid RETURNING submitted_at",
            attempt_id, json.dumps(responses), score_pct, passed)

    # ── review cards ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def add_review_cards(self, learner: dict, items: list[tuple[str, str]], card: dict, due: datetime) -> None:
        """A missed item becomes a review card once (an existing card keeps its schedule)."""
        for item_id, module_id in items:
            await self.conn.execute(
                "INSERT INTO learn.review_card (learner_id, tenant_id, item_id, module_id, card, due_at) "
                "VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, $6) ON CONFLICT (learner_id, item_id) DO NOTHING",
                learner["learner_id"], learner["tenant_id"], item_id, module_id, json.dumps(card), due)

    async def due_cards(self, learner_id: str, now: datetime, limit: int) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT item_id, module_id, card, due_at FROM learn.review_card WHERE learner_id = $1::uuid "
            "AND due_at <= $2 ORDER BY due_at, item_id LIMIT $3", learner_id, now, limit)
        return [_row(r) for r in rows]

    async def reviews_since(self, learner_id: str, since: datetime) -> int:
        return await self.conn.fetchval(
            "SELECT count(*) AS n FROM learn.statement WHERE learner_id = $1::uuid AND verb = 'reviewed' "
            "AND stored_at >= $2", learner_id, since)

    async def review_card(self, learner_id: str, item_id: str) -> Optional[dict]:
        return _row(await self.conn.fetchrow(
            "SELECT item_id, module_id, card, due_at, last_review_at, reps, lapses FROM learn.review_card "
            "WHERE learner_id = $1::uuid AND item_id = $2 FOR UPDATE", learner_id, item_id))

    async def save_card(self, learner_id: str, item_id: str, card: dict, due: datetime, last: datetime) -> None:
        await self.conn.execute(
            "UPDATE learn.review_card SET card = $3::jsonb, due_at = $4, last_review_at = $5, reps = $6, "
            "lapses = $7, updated_at = now() WHERE learner_id = $1::uuid AND item_id = $2",
            learner_id, item_id, json.dumps(card), due, last, int(card.get("reps", 0)), int(card.get("lapses", 0)))

    # ── labs ─────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def lab_passed(self, learner_id: str, module_id: str) -> bool:
        return bool(await self.conn.fetchval(
            "SELECT true AS ok FROM learn.lab_run WHERE learner_id = $1::uuid AND module_id = $2 "
            "AND status = 'passed' LIMIT 1", learner_id, module_id))

    # ── certificates ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def certificates(self, learner_id: str) -> list[dict]:
        rows = await self.conn.fetch(f"SELECT {CERT_COLS} FROM learn.certificate WHERE learner_id = $1::uuid "
                                     "ORDER BY issued_at", learner_id)
        return [_row(r) for r in rows]

    async def certificate(self, learner_id: str, *, path_id: Optional[str] = None,
                          certificate_id: Optional[str] = None) -> Optional[dict]:
        if certificate_id is not None:
            return _row(await self.conn.fetchrow(
                f"SELECT {CERT_COLS} FROM learn.certificate WHERE certificate_id = $1::uuid AND learner_id = $2::uuid "
                "FOR UPDATE", certificate_id, learner_id))
        return _row(await self.conn.fetchrow(
            f"SELECT {CERT_COLS} FROM learn.certificate WHERE learner_id = $1::uuid AND path_id = $2 FOR UPDATE",
            learner_id, path_id))

    async def issue_certificate(self, learner: dict, path: dict, code_sha: str, expires_at: Optional[datetime],
                                module_versions: dict, final_attempt_id: Optional[str], badge: dict,
                                renewal_reason: Optional[str]) -> dict:
        """Issue, or renew in place (new dates, pinned versions and code; one certificate per learner and path)."""
        return _row(await self.conn.fetchrow(
            "INSERT INTO learn.certificate (learner_id, tenant_id, path_id, path_title, code_sha256, expires_at, "
            "module_versions, final_attempt_id, badge, renewal_reason) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, "
            "$7::jsonb, $8::uuid, $9::jsonb, $10) ON CONFLICT (learner_id, path_id) DO UPDATE SET "
            "path_title = EXCLUDED.path_title, code_sha256 = EXCLUDED.code_sha256, status = 'valid', "
            "issued_at = now(), "
            "expires_at = EXCLUDED.expires_at, module_versions = EXCLUDED.module_versions, "
            "final_attempt_id = EXCLUDED.final_attempt_id, badge = EXCLUDED.badge, "
            f"renewal_reason = EXCLUDED.renewal_reason RETURNING {CERT_COLS}",
            learner["learner_id"], learner["tenant_id"], path["id"], path.get("title") or path["id"], code_sha,
            expires_at, json.dumps(module_versions), final_attempt_id, json.dumps(badge), renewal_reason))

    async def set_code(self, certificate_id: str, code_sha: str) -> None:
        await self.conn.execute("UPDATE learn.certificate SET code_sha256 = $2 WHERE certificate_id = $1::uuid",
                                certificate_id, code_sha)

    async def verify(self, code_sha: str) -> Optional[dict]:
        """The certificate of a verification code with its holder's display name (the staff directory name the
        learner row keeps), or None."""
        return _row(await self.conn.fetchrow(
            "SELECT c.path_id, c.path_title, c.status, c.issued_at, c.expires_at, c.module_versions, "
            "l.display_name AS holder_display_name FROM learn.certificate c JOIN learn.learner l USING (learner_id) "
            "WHERE c.code_sha256 = $1", code_sha))
