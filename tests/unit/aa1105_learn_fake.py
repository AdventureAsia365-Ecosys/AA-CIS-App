"""AA-1105 unit-test helpers: an invented learn bundle, an in-memory twin of services.learn.store.PgStore, a local
WORKFORCE issuer wired into the academy verifier, and a TestClient on the learn router. No network, no AWS, no DB.
Every name and text here is invented (CI golden-set text guard)."""
from __future__ import annotations

import copy
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import identity_config, learn_identity
from api.core.identity_verifier import MultiIssuerVerifier
from api.routers import learn as learn_router
from services.learn import bundle as lb
from tests.unit.test_identity_verifier import FakeFetcher, LocalIssuer

WORKFORCE = identity_config.DEV_WORKFORCE_ISSUER
ACADEMY = learn_identity.DEV_ACADEMY_WEB_CLIENT_ID
SUB = "1105aaaa-0000-4000-8000-000000000001"
ADMIN_ID = "1105bbbb-0000-4000-8000-000000000001"
SECTION = "adr:0099#sample-rule"


def _q(item_id: str, answer: list[int], suspended: bool = False, kind: str = "single") -> dict:
    return {"id": item_id, "kind": kind, "prompt": f"Which option is right for {item_id}?",
            "choices": ["Option one", "Option two", "Option three"], "answer": answer, "source": SECTION,
            "explain": f"Because of the sample rule ({item_id}).", "suspended": suspended}


def _lesson(lesson_id: str, n: int, video: Optional[dict] = None, vi: bool = False) -> dict:
    key = f"lessons/{lesson_id[:2]}-sample/0{n}-sample-lesson.md"
    return {"id": lesson_id, "title": f"Sample lesson {lesson_id}", "type": "reading", "minutes": 5,
            "objective": "Explain the sample rule in one sentence.", "key": key,
            "translations": [{"lang": "vi", "key": key[:-3] + ".vi.md"}] if vi else [], "stale": False,
            "sources": [{"ref": SECTION, "key": "sections/adr/0099/sample-rule.md", "pinned": "0" * 16,
                         "current": "0" * 16, "stale": False}], "video": video}


def _module(module_id: str, lessons: list[dict], status: str = "active", prereq: Optional[list] = None,
            quiz_items: int = 0, version: str = "1.0.0", lab: Optional[str] = None) -> dict:
    return {"id": module_id, "title": f"Sample module {module_id}", "tier": "staff", "status": status,
            "version": version, "owner": "tech-lead", "last_reviewed": "2026-11-01", "review_every_days": 90,
            "languages": ["en"], "objectives": ["Name the sample rule and apply it."], "prerequisites": prereq or [],
            "lab": lab, "stale": False, "lessons": lessons,
            "quiz": {"key": f"quizzes/{module_id}.json", "items": quiz_items, "suspended": 0} if quiz_items else None}


def manifest() -> dict:
    video = {"key": "learn-media/Z9/01-sample-lesson.mp4",
             "captions": [{"lang": "en", "key": "learn-media/Z9/01-sample-lesson.en.vtt"}]}
    return {
        "format": 1, "generated_at": "2026-11-15T00:00:00Z",
        "source": {"repo": "sample", "sha": "f" * 40, "ref": "master"}, "counts": {},
        "paths": [
            {"id": "sample-path", "title": "Sample path", "tier": "staff", "audience": "Everyone", "owner": "tech-lead",
             "modules": [{"id": "Z9", "required": True}, {"id": "D5", "required": False}],
             "planned": ["Z7"], "final_quiz": True},
            {"id": "solo-path", "title": "Solo path", "tier": "staff", "audience": "Everyone", "owner": "tech-lead",
             "modules": [{"id": "Z8", "required": True}], "planned": [], "final_quiz": False},
            {"id": "agency-path", "title": "Agency path", "tier": "partner", "audience": "Agencies",
             "owner": "tech-lead", "modules": [], "planned": [], "final_quiz": False},
        ],
        "modules": [
            _module("Z9", [_lesson("Z9-01", 1, video=video, vi=True), _lesson("Z9-02", 2)], prereq=["Z7"],
                    quiz_items=4),
            _module("Z8", [_lesson("Z8-01", 1)], quiz_items=2, lab="lab.yaml"),
            _module("D5", [_lesson("D5-01", 1)], status="draft", quiz_items=1),
        ],
        "sections": [{"ref": SECTION, "kind": "adr", "name": "0099", "anchor": "sample-rule", "path": "x.md",
                      "key": "sections/adr/0099/sample-rule.md", "sha": "0" * 16, "tier": "staff", "redactions": 0,
                      "available": True}],
    }


QUIZZES = {
    "Z9": [_q("Z9-Q1", [0]), _q("Z9-Q2", [1]), _q("Z9-Q3", [0, 2], kind="multi"), _q("Z9-Q4", [2], suspended=True)],
    "Z8": [_q("Z8-Q1", [1]), _q("Z8-Q2", [0])],
    "D5": [_q("D5-Q1", [0])],
}


def install_bundle(data: Optional[dict] = None) -> lb.Bundle:
    lb.clear_cache()
    bundle = lb.index(copy.deepcopy(data or manifest()))
    lb.install(bundle)
    for module in bundle.modules.values():
        for lesson in module["lessons"]:
            lb.install_body(bundle, lesson["key"], f"# {lesson['title']}\n\nRead the sample rule.\n")
            for t in lesson["translations"]:
                lb.install_body(bundle, t["key"], f"# {lesson['title']}\n\nBản dịch mẫu.\n")
        if module["quiz"]:
            lb.install_body(bundle, module["quiz"]["key"], {"module": module["id"], "version": module["version"],
                                                            "items": copy.deepcopy(QUIZZES[module["id"]])})
    lb.install_body(bundle, "sections/adr/0099/sample-rule.md", "## Sample rule\n\nAlways do the sample thing.\n")
    return bundle


class FakeStore:
    """In-memory twin of PgStore (same method names and return shapes)."""

    def __init__(self) -> None:
        self.staff: dict[tuple[str, str], dict] = {(WORKFORCE, SUB): {
            "admin_id": ADMIN_ID, "email": "learner@example.invalid", "display_name": "Sample Learner",
            "db_role": "content"}}
        self.learners: dict[tuple[str, str], dict] = {}
        self.statements: dict[str, dict] = {}
        self.progress_rows: dict[tuple[str, str], dict] = {}
        self.enrollment_rows: dict[tuple[str, str], dict] = {}
        self.attempts: dict[str, dict] = {}
        self.cards: dict[tuple[str, str], dict] = {}
        self.certs: dict[str, dict] = {}
        self.labs: set[tuple[str, str]] = set()

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    async def staff_row(self, iss, sub):
        row = self.staff.get((iss, sub))
        return dict(row) if row else None

    async def upsert_learner(self, iss, sub, admin_id, display_name, is_test):
        row = self.learners.setdefault((iss, sub), {"learner_id": str(uuid.uuid4()), "kind": "staff",
                                                     "tenant_id": None})
        row.update(display_name=display_name, is_test=is_test, admin_id=admin_id)
        return {k: row[k] for k in ("learner_id", "kind", "tenant_id", "display_name", "is_test")}

    async def statement(self, learner, *, verb, object_type, object_id, statement_id=None, body_sha256,
                        module_version=None, bundle_sha=None, result=None, context=None):
        sid = statement_id or str(uuid.uuid4())
        old = self.statements.get(sid)
        if old is not None:
            same = old["learner_id"] == learner["learner_id"] and old["body_sha256"] == body_sha256
            return "same" if same else "conflict"
        self.statements[sid] = {"learner_id": learner["learner_id"], "verb": verb, "object_type": object_type,
                                "object_id": object_id, "body_sha256": body_sha256, "stored_at": self.now(),
                                "result": result or {}}
        return "created"

    async def progress(self, learner_id):
        return {k[1]: dict(v) for k, v in self.progress_rows.items() if k[0] == learner_id}

    async def set_progress(self, learner, lesson_id, module_id, module_version, lang, completed):
        key = (learner["learner_id"], lesson_id)
        row = self.progress_rows.setdefault(key, {"lesson_id": lesson_id, "module_id": module_id, "status": "opened",
                                                  "completed_at": None})
        if completed and row["status"] != "completed":
            row.update(status="completed", completed_at=self.now())
        row.update(module_version=module_version, lang=lang)
        return row["status"]

    async def enrollments(self, learner_id):
        return {k[1]: dict(v) for k, v in self.enrollment_rows.items() if k[0] == learner_id}

    async def enroll(self, learner, path_id):
        key = (learner["learner_id"], path_id)
        if key in self.enrollment_rows:
            row = self.enrollment_rows[key]
            if row["status"] == "withdrawn":
                row["status"] = "active"
            return dict(row), False
        self.enrollment_rows[key] = {"path_id": path_id, "status": "active", "enrolled_at": self.now(),
                                     "completed_at": None}
        return dict(self.enrollment_rows[key]), True

    async def complete_enrollment(self, learner, path_id):
        row = self.enrollment_rows.setdefault((learner["learner_id"], path_id), {
            "path_id": path_id, "enrolled_at": self.now(), "completed_at": None})
        row.update(status="completed", completed_at=row.get("completed_at") or self.now())

    async def submitted_attempts(self, learner_id):
        rows = [a for a in self.attempts.values() if a["learner_id"] == learner_id and a["status"] == "submitted"]
        return [dict(a) for a in sorted(rows, key=lambda a: a["submitted_at"])]

    async def start_attempt(self, learner, scope, target_id, item_ids, module_versions, bundle_sha, pass_mark_pct):
        for a in self.attempts.values():
            if (a["learner_id"], a["scope"], a["target_id"], a["status"]) == (learner["learner_id"], scope,
                                                                                target_id, "open"):
                a["status"] = "expired"
        aid = str(uuid.uuid4())
        self.attempts[aid] = {"attempt_id": aid, "learner_id": learner["learner_id"], "scope": scope,
                              "target_id": target_id, "item_ids": list(item_ids),
                              "module_versions": dict(module_versions), "bundle_sha": bundle_sha,
                              "pass_mark_pct": pass_mark_pct, "status": "open", "started_at": self.now(),
                              "submitted_at": None, "passed": None, "score_pct": None}
        return {"attempt_id": aid, "started_at": self.attempts[aid]["started_at"]}

    async def attempt_for_update(self, learner_id, attempt_id):
        a = self.attempts.get(attempt_id)
        return dict(a) if a and a["learner_id"] == learner_id else None

    async def submit_attempt(self, attempt_id, responses, score_pct, passed):
        a = self.attempts[attempt_id]
        a.update(status="submitted", submitted_at=self.now(), responses=responses, score_pct=score_pct, passed=passed)
        return a["submitted_at"]

    async def add_review_cards(self, learner, items, card, due):
        for item_id, module_id in items:
            self.cards.setdefault((learner["learner_id"], item_id), {
                "item_id": item_id, "module_id": module_id, "card": dict(card), "due_at": due,
                "last_review_at": None, "reps": 0, "lapses": 0})

    async def due_cards(self, learner_id, now, limit):
        rows = [dict(c) for (lid, _), c in self.cards.items() if lid == learner_id and c["due_at"] <= now]
        return sorted(rows, key=lambda c: (c["due_at"], c["item_id"]))[:limit]

    async def reviews_since(self, learner_id, since):
        return sum(1 for s in self.statements.values()
                   if s["learner_id"] == learner_id and s["verb"] == "reviewed" and s["stored_at"] >= since)

    async def review_card(self, learner_id, item_id):
        row = self.cards.get((learner_id, item_id))
        return dict(row) if row else None

    async def save_card(self, learner_id, item_id, card, due, last):
        self.cards[(learner_id, item_id)].update(card=dict(card), due_at=due, last_review_at=last,
                                                 reps=card.get("reps", 0), lapses=card.get("lapses", 0))

    async def lab_passed(self, learner_id, module_id):
        return (learner_id, module_id) in self.labs

    async def certificates(self, learner_id):
        return [dict(c) for c in self.certs.values() if c["learner_id"] == learner_id]

    async def certificate(self, learner_id, *, path_id=None, certificate_id=None):
        for c in self.certs.values():
            if c["learner_id"] == learner_id and (c["certificate_id"] == certificate_id or c["path_id"] == path_id):
                return dict(c)
        return None

    async def issue_certificate(self, learner, path, code_sha, expires_at, module_versions, final_attempt_id, badge,
                                renewal_reason):
        old = next((c for c in self.certs.values()
                    if c["learner_id"] == learner["learner_id"] and c["path_id"] == path["id"]), None)
        cid = old["certificate_id"] if old else str(uuid.uuid4())
        self.certs[cid] = {"certificate_id": cid, "learner_id": learner["learner_id"], "path_id": path["id"],
                           "path_title": path.get("title") or path["id"], "status": "valid", "issued_at": self.now(),
                           "expires_at": expires_at, "module_versions": dict(module_versions),
                           "code_sha256": code_sha, "renewal_reason": renewal_reason, "badge": badge}
        return dict(self.certs[cid])

    async def set_code(self, certificate_id, code_sha):
        self.certs[certificate_id]["code_sha256"] = code_sha

    async def verify(self, code_sha):
        for c in self.certs.values():
            if c["code_sha256"] == code_sha:
                name = next(v["display_name"] for v in self.learners.values() if v["learner_id"] == c["learner_id"])
                return {**{k: c[k] for k in ("path_id", "path_title", "status", "issued_at", "expires_at",
                                             "module_versions")}, "holder_display_name": name}
        return None


class Harness:
    """The learn router on a bare FastAPI app with a fake store, a local WORKFORCE issuer and the sample bundle."""

    def __init__(self) -> None:
        self.issuer = LocalIssuer(WORKFORCE)
        self.fetcher = FakeFetcher(self.issuer)
        learn_identity.set_academy_verifier(MultiIssuerVerifier([learn_identity.academy_issuer_config()],
                                                                self.fetcher))
        self.store = FakeStore()
        self.bundle = install_bundle()
        self.redis = None

        @asynccontextmanager
        async def fake_store(_request):
            yield self.store

        self._saved = learn_router.open_store
        learn_router.open_store = fake_store
        learn_router._window.clear()
        app = FastAPI()
        app.include_router(learn_router.router)
        app.include_router(learn_router.public_router)
        app.add_exception_handler(learn_router.ac.Problem, learn_router.problem_handler)
        app.state.redis = None
        self.app = app
        self.client = TestClient(app)

    def close(self) -> None:
        learn_router.open_store = self._saved
        learn_identity.set_academy_verifier(None)
        lb.clear_cache()

    def token(self, **claims: Any) -> str:
        claims.setdefault("client_id", ACADEMY)
        claims.setdefault("sub", SUB)
        claims.setdefault("cognito:groups", ["cis-content"])
        claims.setdefault("email", "learner@example.invalid")
        return self.issuer.token(**claims)

    def headers(self, **claims: Any) -> dict:
        return {"Authorization": f"Bearer {self.token(**claims)}"}

    def get(self, path: str, **claims: Any):
        return self.client.get(f"/learn{path}", headers=self.headers(**claims))

    def post(self, path: str, body: Any = None, **claims: Any):
        return self.client.post(f"/learn{path}", headers=self.headers(**claims), json=body)
