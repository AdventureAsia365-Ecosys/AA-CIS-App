"""The AA Academy learning API logic (AA-1105, root ADR 0022, contract learn.http.v1).

Course content comes from the learn bundle (:mod:`services.learn.bundle`), learner records from the store
(:mod:`services.learn.store`, or its in-memory twin in the unit tests), thresholds from the CIS settings registry
(``cis.learn.*``). The router (``api/routers/learn.py``) only verifies the token, opens the store and maps
:class:`Problem` to the contract's Problem body.

Rules (research docs/research/lms §4.3; ADR 0022 decisions 7 and 10):

- A learner sees the paths and modules of the staff tier. A module with status draft is visible only with the
  ``learn.preview`` capability, marked draft, and never counts toward a certificate; a retired module is gone. A
  prerequisite that is only planned (not in the bundle) never blocks.
- A lesson is complete once a ``completed`` statement was recorded. A module is complete when every lesson is complete
  and its quiz (when it has one) was passed at the current major version. Labs are closed until the lab provisioner
  (P4b): a lab neither blocks completion nor can be run (409 ``lab_closed``).
- Quiz answers never leave the server before a submission; the server grades against the items bound at start. A
  failed attempt opens the next one after ``cis.learn.quiz_retake_delay_hours``. Missed items become review cards.
- A path is complete when every required (active) module is complete and, when the path has a final quiz, that quiz
  is passed; then the path certificate is issued (or renewed in place), pinned to the module versions. Only the paths
  in ``cis.learn.recert_paths`` expire, after ``cis.learn.recert_months``. A revoked certificate is never re-issued by
  a pass (an administrator revoked it).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from shared import settings

from services.learn import bundle as lb
from services.learn import grading as g

STAFF_TIER = "staff"
REVIEW_SKEW_S = 120  # technical-constant: browser clock skew accepted on a review time


class Problem(Exception):
    """A refusal with the contract's error code."""

    def __init__(self, status: int, error: str, message: str, retry_at: Optional[datetime] = None) -> None:
        super().__init__(status, error, message, retry_at)
        self.status, self.error, self.message, self.retry_at = status, error, message, retry_at


def not_found(what: str) -> Problem:
    return Problem(404, "not_found", f"{what} was not found.")


@dataclass
class Ctx:
    """One request: the learner row, whether drafts are visible, the store and the bundle."""

    learner: dict
    preview: bool
    store: Any
    bundle: Optional[lb.Bundle] = None

    @property
    def learner_id(self) -> str:
        return self.learner["learner_id"]


# ── visibility ───────────────────────────────────────────────────────────────────────────────────────────────────────

def module_visible(module: Optional[dict], preview: bool) -> bool:
    if module is None or module.get("tier") != STAFF_TIER:
        return False
    return module.get("status") == "active" or (module.get("status") == "draft" and preview)


def get_module(ctx: Ctx, module_id: str) -> dict:
    module = ctx.bundle.modules.get(module_id)
    if not module_visible(module, ctx.preview):
        raise not_found("The module")
    return module


def get_path(ctx: Ctx, path_id: str) -> dict:
    path = ctx.bundle.paths.get(path_id)
    if path is None or path.get("tier") != STAFF_TIER:
        raise not_found("The path")
    return path


def get_lesson(ctx: Ctx, lesson_id: str) -> tuple[dict, dict]:
    hit = ctx.bundle.lessons.get(lesson_id)
    if hit is None or not module_visible(hit[0], ctx.preview):
        raise not_found("The lesson")
    return hit


# ── learner state ────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class State:
    progress: dict
    attempts: list


async def load_state(ctx: Ctx) -> State:
    return State(progress=await ctx.store.progress(ctx.learner_id),
                 attempts=await ctx.store.submitted_attempts(ctx.learner_id))


def lesson_status(state: State, lesson_id: str) -> str:
    row = state.progress.get(lesson_id)
    return row["status"] if row else "not_started"


def _attempts(state: State, scope: str, target_id: str) -> list[dict]:
    return [a for a in state.attempts if a["scope"] == scope and a["target_id"] == target_id]


def quiz_passed(state: State, module: dict) -> bool:
    current = g.major(module.get("version"))
    return any(a["passed"] and g.major((a.get("module_versions") or {}).get(module["id"])) == current
               for a in _attempts(state, "module", module["id"]))


def module_complete(state: State, module: dict) -> bool:
    if any(lesson_status(state, lesson["id"]) != "completed" for lesson in module.get("lessons", [])):
        return False
    return quiz_passed(state, module) if module.get("quiz") else True


def required_modules(ctx: Ctx, path: dict) -> list[Optional[dict]]:
    return [ctx.bundle.modules.get(e["id"]) for e in path.get("modules", []) if e.get("required")]


def required_complete(ctx: Ctx, state: State, path: dict) -> bool:
    """Every required module is in the bundle, active (a draft never counts) and complete."""
    modules = required_modules(ctx, path)
    return bool(modules) and all(m is not None and m.get("status") == "active" and module_complete(state, m)
                                 for m in modules)


def last_failed_at(state: State, scope: str, target_id: str) -> Optional[datetime]:
    failed = [a["submitted_at"] for a in _attempts(state, scope, target_id) if not a["passed"]]
    passed = [a["submitted_at"] for a in _attempts(state, scope, target_id) if a["passed"]]
    if not failed or (passed and max(passed) > max(failed)):
        return None
    return max(failed)


async def retake_at(state: State, scope: str, target_id: str) -> Optional[datetime]:
    delay = await settings.aget("cis.learn.quiz_retake_delay_hours")
    return g.retake_at(last_failed_at(state, scope, target_id), delay, g.now_utc())


# ── me, paths, enroll ────────────────────────────────────────────────────────────────────────────────────────────

def me(ctx: Ctx) -> dict:
    return {"learner_id": ctx.learner_id, "kind": ctx.learner["kind"], "display_name": ctx.learner["display_name"],
            "preview": ctx.preview, "is_test": bool(ctx.learner["is_test"])}


def _path_module(ctx: Ctx, state: State, entry: dict) -> Optional[dict]:
    module = ctx.bundle.modules.get(entry["id"])
    if module is None:
        return {"id": entry["id"], "title": None, "required": bool(entry.get("required")), "status": "planned",
                "complete": False}
    if not module_visible(module, ctx.preview):
        return None
    return {"id": module["id"], "title": module.get("title"), "required": bool(entry.get("required")),
            "status": module["status"], "complete": module_complete(state, module)}


async def list_paths(ctx: Ctx) -> dict:
    state = await load_state(ctx)
    enrolled = await ctx.store.enrollments(ctx.learner_id)
    certs = {c["path_id"]: c for c in await ctx.store.certificates(ctx.learner_id)}
    now = g.now_utc()
    out = []
    for path in ctx.bundle.paths.values():
        if path.get("tier") != STAFF_TIER:
            continue
        cert = certs.get(path["id"])
        modules = [m for m in (_path_module(ctx, state, e) for e in path.get("modules", [])) if m is not None]
        out.append({
            "id": path["id"], "title": path.get("title") or path["id"], "tier": STAFF_TIER, "modules": modules,
            "planned": [str(p) for p in path.get("planned", [])], "enrolled": path["id"] in enrolled,
            "completed": (enrolled.get(path["id"]) or {}).get("status") == "completed",
            "final_quiz": bool(path.get("final_quiz")),
            "certificate_status": g.certificate_status(cert, path, ctx.bundle.modules, now) if cert else None,
        })
    return {"bundle_sha": ctx.bundle.sha, "paths": out}


async def enroll(ctx: Ctx, path_id: str) -> tuple[dict, bool]:
    path = get_path(ctx, path_id)
    row, created = await ctx.store.enroll(ctx.learner, path["id"])
    if created:
        await ctx.store.statement(ctx.learner, verb="enrolled", object_type="path", object_id=path["id"],
                                  body_sha256=g.canonical_sha256({"verb": "enrolled", "path": path["id"]}),
                                  bundle_sha=ctx.bundle.sha)
    return {"path_id": row["path_id"], "status": row["status"], "enrolled_at": g.iso(row["enrolled_at"]),
            "completed_at": g.iso(row["completed_at"])}, created


# ── module and lessons ─────────────────────────────────────────────────────────────────────────────────────────────

def _prerequisites(ctx: Ctx, state: State, module: dict) -> list[dict]:
    out = []
    for pid in module.get("prerequisites", []):
        other = ctx.bundle.modules.get(pid)
        if other is None or not module_visible(other, ctx.preview):
            out.append({"id": pid, "state": "planned"})
        else:
            out.append({"id": pid, "state": "complete" if module_complete(state, other) else "open"})
    return out


def _lesson_summary(state: State, lesson: dict) -> dict:
    return {"id": lesson["id"], "title": lesson["title"], "type": lesson["type"], "minutes": int(lesson["minutes"]),
            "stale": bool(lesson.get("stale")), "has_video": bool(lesson.get("video")),
            "status": lesson_status(state, lesson["id"])}


async def _quiz_state(ctx: Ctx, state: State, module: dict) -> dict:
    items = g.live_items(await lb.quiz(ctx.bundle, module)) if module.get("quiz") else []
    scores = [a["score_pct"] for a in _attempts(state, "module", module["id"]) if a.get("score_pct") is not None]
    per_attempt = int(await settings.aget("cis.learn.quiz_items_per_attempt"))
    return {"available": bool(items), "items_per_attempt": min(per_attempt, len(items)),
            "pass_mark_pct": int(await settings.aget("cis.learn.quiz_pass_mark_pct")),
            "best_score_pct": max(scores) if scores else None, "passed": quiz_passed(state, module),
            "retake_at": g.iso(await retake_at(state, "module", module["id"]))}


async def get_module_view(ctx: Ctx, module_id: str) -> dict:
    module = get_module(ctx, module_id)
    state = await load_state(ctx)
    lab_present = bool(module.get("lab"))
    return {
        "id": module["id"], "title": module["title"], "version": str(module["version"]), "status": module["status"],
        "draft": module["status"] == "draft", "stale": bool(module.get("stale")),
        "objectives": [str(o) for o in module.get("objectives", [])],
        "prerequisites": _prerequisites(ctx, state, module), "complete": module_complete(state, module),
        "lessons": [_lesson_summary(state, lesson) for lesson in module.get("lessons", [])],
        "quiz": await _quiz_state(ctx, state, module),
        "lab": {"present": lab_present, "state": "closed" if lab_present else "none"},
    }


async def _source(ctx: Ctx, src: dict) -> dict:
    section = ctx.bundle.sections.get(src.get("ref")) or {}
    key = section.get("key")
    available = bool(section.get("available")) and bool(key)
    return {"ref": str(src.get("ref")), "kind": section.get("kind", "doc"), "stale": bool(src.get("stale")),
            "available": available, "body_md": await lb.section_body(ctx.bundle, key) if available else None}


async def get_lesson_view(ctx: Ctx, lesson_id: str, lang: str) -> dict:
    module, lesson = get_lesson(ctx, lesson_id)
    translations = {t["lang"]: t["key"] for t in lesson.get("translations", []) if t.get("lang") in ("vi",)}
    served = lang if lang in translations else "en"
    key = translations[served] if served != "en" else lesson["key"]
    state = await load_state(ctx)
    return {
        "id": lesson["id"], "module_id": module["id"], "module_version": str(module["version"]),
        "title": lesson["title"], "type": lesson["type"], "minutes": int(lesson["minutes"]),
        "objective": lesson["objective"], "lang": served, "languages": ["en", *sorted(translations)],
        "body_md": await lb.lesson_body(ctx.bundle, key), "stale": bool(lesson.get("stale")),
        "draft": module["status"] == "draft", "has_video": bool(lesson.get("video")),
        "status": lesson_status(state, lesson["id"]),
        "sources": [await _source(ctx, s) for s in lesson.get("sources", [])],
    }


async def get_video(ctx: Ctx, lesson_id: str) -> dict:
    """Presigned GET URLs of the lesson video and its captions (decision D8). The academy web fetches the captions
    server side and serves them from its own origin (no bucket CORS); the browser plays the video straight from S3."""
    _, lesson = get_lesson(ctx, lesson_id)
    video = lesson.get("video")
    if not video:
        raise not_found("The lesson video")
    ttl = int(await settings.aget("cis.learn.video_url_ttl_seconds"))
    url = await lb.presign(video["key"], ttl, check=True)
    if url is None:
        raise not_found("The lesson video")
    captions = [{"lang": c["lang"], "url": await lb.presign(c["key"], ttl)} for c in video.get("captions", [])
                if c.get("lang") in ("en", "vi")]
    return {"url": url, "expires_at": g.iso(g.now_utc() + timedelta(seconds=ttl)), "captions": captions}


# ── progress ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def record_progress(ctx: Ctx, body: dict) -> tuple[dict, bool]:
    """(ProgressAccepted, created). Idempotent on statement_id: the same body answers 200, another 409."""
    module, lesson = get_lesson(ctx, body["lesson_id"])
    lang = body.get("lang") or "en"
    outcome = await ctx.store.statement(
        ctx.learner, statement_id=body["statement_id"], verb=body["verb"], object_type="lesson",
        object_id=lesson["id"], body_sha256=g.canonical_sha256(body), module_version=str(module["version"]),
        bundle_sha=ctx.bundle.sha, context={"lang": lang})
    if outcome == "conflict":
        raise Problem(409, "conflict", "This statement id was already used with another body.")
    if outcome == "created":
        await ctx.store.set_progress(ctx.learner, lesson["id"], module["id"], str(module["version"]), lang,
                                     body["verb"] == "completed")
    state = await load_state(ctx)
    complete = module_complete(state, module)
    if outcome == "created" and complete:
        await issue_ready_certificates(ctx, state, module["id"])
    return {"statement_id": body["statement_id"], "lesson_id": lesson["id"],
            "status": "completed" if lesson_status(state, lesson["id"]) == "completed" else "opened",
            "module_complete": complete}, outcome == "created"


# ── certificates (issued here so progress and quiz submissions share one rule) ─────────────────────────────────────

async def issue_certificate(ctx: Ctx, path: dict, final_attempt_id: Optional[str]) -> Optional[dict]:
    """Issue or renew the path certificate; the CertificateCode (shown once), or None when nothing is due: a revoked
    certificate stays revoked and a valid one keeps its code, dates and pinned versions (a pass of the final quiz
    while the certificate is valid issues nothing, the same rule as :func:`issue_ready_certificates`)."""
    existing = await ctx.store.certificate(ctx.learner_id, path_id=path["id"])
    if existing is not None and not _needs_certificate(ctx, existing):   # revoked or valid: nothing is due
        return None
    now = g.now_utc()
    versions = {m["id"]: str(m["version"]) for m in required_modules(ctx, path) if m is not None}
    expires = None
    if path["id"] in (await settings.aget("cis.learn.recert_paths") or []):
        expires = g.add_months(now, int(await settings.aget("cis.learn.recert_months")))
    code, sha = g.new_code()
    row = await ctx.store.issue_certificate(
        ctx.learner, path, sha, expires, versions, final_attempt_id, g.badge(path),
        g.renewal_reason(existing, path, ctx.bundle.modules, now))
    await ctx.store.complete_enrollment(ctx.learner, path["id"])
    await ctx.store.statement(ctx.learner, verb="certified", object_type="certificate",
                              object_id=row["certificate_id"], bundle_sha=ctx.bundle.sha,
                              body_sha256=g.canonical_sha256({"verb": "certified", "path": path["id"],
                                                              "versions": versions, "at": g.iso(now)}),
                              result={"path_id": path["id"], "renewal": existing is not None})
    return {"certificate_id": row["certificate_id"], "path_id": path["id"], "code": code,
            "verify_path": f"/verify/{code}"}


def _needs_certificate(ctx: Ctx, cert: Optional[dict]) -> bool:
    if cert is None:
        return True
    return g.certificate_status(cert, ctx.bundle.paths.get(cert["path_id"]), ctx.bundle.modules,
                                g.now_utc()) in ("expired", "renewal_due")


async def issue_ready_certificates(ctx: Ctx, state: State, module_id: str) -> list[dict]:
    """After a module completed: the certificates of the paths WITHOUT a final quiz that hold this module as required
    and are now complete (a path with a final quiz certifies on that quiz only)."""
    issued = []
    for path in ctx.bundle.paths.values():
        if path.get("tier") != STAFF_TIER or path.get("final_quiz"):
            continue
        if not any(e["id"] == module_id and e.get("required") for e in path.get("modules", [])):
            continue
        if not required_complete(ctx, state, path):
            continue
        code = await issue_certificate(ctx, path, None)     # None when the certificate is still valid
        if code is not None:
            issued.append(code)
    return issued
