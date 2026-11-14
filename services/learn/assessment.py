"""Quizzes, spaced review, labs and certificates of the AA Academy learning API (AA-1105, contract learn.http.v1).

The server draws the items of an attempt and binds them to it (item ids, module versions, bundle sha, pass mark);
the browser gets them without answers. A submission is graded against that bound set only, never the browser's list.
See :mod:`services.learn.academy` for the completion and certificate rules.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional

from services.learn import academy as ac
from services.learn import bundle as lb
from shared import settings
from services.learn import grading as g
from services.learn.academy import Ctx, Problem


async def _items_by_id(ctx: Ctx, modules: list[dict]) -> dict[str, tuple[dict, dict]]:
    """item id -> (item WITH answer, module), for the given modules."""
    out: dict[str, tuple[dict, dict]] = {}
    for module in modules:
        for item in await lb.quiz(ctx.bundle, module):
            out[item["id"]] = (item, module)
    return out


async def _check_retake(state: ac.State, scope: str, target_id: str) -> None:
    opens = await ac.retake_at(state, scope, target_id)
    if opens is not None:
        raise Problem(409, "retake_wait", "The next attempt opens after the retake delay.", retry_at=opens)


def _attempt_body(row: dict, scope: str, target_id: str, pass_mark: int, items: list[dict]) -> dict:
    return {"attempt_id": row["attempt_id"], "scope": scope, "target_id": target_id,
            "started_at": g.iso(row["started_at"]), "pass_mark_pct": pass_mark,
            "items": [g.public_item(i) for i in items]}


async def _open_attempt(ctx: Ctx, scope: str, target_id: str, items: list[dict], versions: dict,
                        pass_mark: int) -> dict:
    if not items:
        raise Problem(409, "conflict", "This quiz has no questions available right now.")
    row = await ctx.store.start_attempt(ctx.learner, scope, target_id, [i["id"] for i in items], versions,
                                        ctx.bundle.sha or "unknown", pass_mark)
    await ctx.store.statement(ctx.learner, verb="attempted", object_type="quiz", object_id=f"{scope}:{target_id}",
                              body_sha256=g.canonical_sha256({"attempt": row["attempt_id"]}),
                              bundle_sha=ctx.bundle.sha, context={"attempt_id": row["attempt_id"]})
    return _attempt_body(row, scope, target_id, pass_mark, items)


async def start_module_quiz(ctx: Ctx, module_id: str) -> dict:
    module = ac.get_module(ctx, module_id)
    if not module.get("quiz"):
        raise ac.not_found("The module quiz")
    state = await ac.load_state(ctx)
    if any(p["state"] == "open" for p in ac._prerequisites(ctx, state, module)):
        raise Problem(409, "prerequisites_open", "Complete the prerequisite modules first.")
    await _check_retake(state, "module", module["id"])
    count = int(await settings.aget("cis.learn.quiz_items_per_attempt"))
    items = g.draw(g.live_items(await lb.quiz(ctx.bundle, module)), count)
    pass_mark = int(await settings.aget("cis.learn.quiz_pass_mark_pct"))
    return await _open_attempt(ctx, "module", module["id"], items, {module["id"]: str(module["version"])}, pass_mark)


async def start_final_quiz(ctx: Ctx, path_id: str) -> dict:
    path = ac.get_path(ctx, path_id)
    if not path.get("final_quiz"):
        raise ac.not_found("The final quiz")
    state = await ac.load_state(ctx)
    if not ac.required_complete(ctx, state, path):
        raise Problem(409, "prerequisites_open", "Complete every required module of the path first.")
    await _check_retake(state, "final", path["id"])
    per_module = int(await settings.aget("cis.learn.final_quiz_items_per_module"))
    modules = [m for m in ac.required_modules(ctx, path) if m is not None]
    items: list[dict] = []
    for module in modules:
        items.extend(g.draw(g.live_items(await lb.quiz(ctx.bundle, module)), per_module))
    items = g.draw(items, len(items))       # mix the modules
    pass_mark = int(await settings.aget("cis.learn.final_quiz_pass_mark_pct"))
    versions = {m["id"]: str(m["version"]) for m in modules}
    return await _open_attempt(ctx, "final", path["id"], items, versions, pass_mark)


def _responses(attempt: dict, submission: list[dict]) -> dict[str, list[int]]:
    bound = set(attempt["item_ids"])
    out: dict[str, list[int]] = {}
    for response in submission:
        item_id = response["item_id"]
        if item_id not in bound:
            raise Problem(422, "invalid", f"Item {item_id} is not part of this attempt.")
        if item_id in out:
            raise Problem(422, "invalid", f"Item {item_id} is answered twice.")
        out[item_id] = sorted(set(response["chosen"]))
    return out


async def _after_pass(ctx: Ctx, attempt: dict, state: ac.State) -> Optional[dict]:
    """The certificate a pass earns (shown once), or None."""
    if attempt["scope"] == "final":
        path = ctx.bundle.paths.get(attempt["target_id"])
        if path is None or not ac.required_complete(ctx, state, path):
            return None
        return await ac.issue_certificate(ctx, path, attempt["attempt_id"])
    module = ctx.bundle.modules.get(attempt["target_id"])
    if module is None or module.get("status") != "active" or not ac.module_complete(state, module):
        return None
    issued = await ac.issue_ready_certificates(ctx, state, module["id"])
    return issued[0] if issued else None


async def submit(ctx: Ctx, attempt_id: str, submission: list[dict]) -> dict:
    attempt = await ctx.store.attempt_for_update(ctx.learner_id, attempt_id)
    if attempt is None:
        raise ac.not_found("The attempt")
    if attempt["status"] != "open":
        raise Problem(409, "attempt_closed", "This attempt is already closed.")
    responses = _responses(attempt, submission)
    modules = [m for m in (ctx.bundle.modules.get(mid) for mid in attempt["module_versions"]) if m is not None]
    bound = await _items_by_id(ctx, modules)
    score, graded = g.grade(attempt["item_ids"], {k: v[0] for k, v in bound.items()}, responses)
    passed = bool(graded) and score >= attempt["pass_mark_pct"]
    submitted_at = await ctx.store.submit_attempt(attempt_id, responses, score, passed)
    now = g.now_utc()
    missed = [(row["item_id"], bound[row["item_id"]][1]["id"]) for row in graded if not row["correct"]]
    await ctx.store.add_review_cards(ctx.learner, missed, g.new_card(now), now)
    await ctx.store.statement(ctx.learner, verb="passed" if passed else "failed", object_type="quiz",
                              object_id=f"{attempt['scope']}:{attempt['target_id']}", bundle_sha=ctx.bundle.sha,
                              body_sha256=g.canonical_sha256({"attempt": attempt_id, "submitted": True}),
                              result={"score_pct": score, "passed": passed}, context={"attempt_id": attempt_id})
    state = await ac.load_state(ctx)
    certificate = await _after_pass(ctx, attempt, state) if passed else None
    target = ctx.bundle.modules.get(attempt["target_id"]) if attempt["scope"] == "module" else None
    delay = await settings.aget("cis.learn.quiz_retake_delay_hours")
    return {"attempt_id": attempt_id, "scope": attempt["scope"], "target_id": attempt["target_id"],
            "score_pct": score, "pass_mark_pct": int(attempt["pass_mark_pct"]), "passed": passed,
            "retake_at": None if passed else g.iso(g.retake_at(submitted_at, delay, now)),
            "module_complete": bool(target and ac.module_complete(state, target)),
            "items": graded, "certificate": certificate}


# ── spaced review ────────────────────────────────────────────────────────────────────────────────────────────────

def _card(raw: dict) -> dict:
    """The stored ts-fsrs card in the contract's shape (unknown keys dropped)."""
    keys = ("due", "stability", "difficulty", "elapsed_days", "scheduled_days", "learning_steps", "reps", "lapses",
            "state", "last_review")
    return {k: raw[k] for k in keys if k in raw}


async def _review_item(ctx: Ctx, item_id: str, module_id: str) -> Optional[dict]:
    module = ctx.bundle.modules.get(module_id)
    if not ac.module_visible(module, ctx.preview):
        return None
    for item in g.live_items(await lb.quiz(ctx.bundle, module)):
        if item["id"] == item_id:
            return item
    return None


async def due_reviews(ctx: Ctx) -> dict:
    cap = int(await settings.aget("cis.learn.review_max_cards_per_day"))
    now = g.now_utc()
    midnight = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    left = max(0, cap - int(await ctx.store.reviews_since(ctx.learner_id, midnight)))
    cards = []
    for row in await ctx.store.due_cards(ctx.learner_id, now, left) if left else []:
        item = await _review_item(ctx, row["item_id"], row["module_id"])
        if item is not None:
            cards.append({"module_id": row["module_id"], "item": g.public_item(item), "card": _card(row["card"])})
    return {"daily_cap": cap, "target_retention": float(await settings.aget("cis.learn.review_target_retention")),
            "cards": cards}


async def review_result(ctx: Ctx, item_id: str, body: dict) -> dict:
    stored = await ctx.store.review_card(ctx.learner_id, item_id)
    item = await _review_item(ctx, item_id, stored["module_id"]) if stored else None
    if stored is None or item is None:
        raise ac.not_found("The review card")
    reviewed_at = g.parse_ts(body["reviewed_at"])
    if reviewed_at is None:
        raise Problem(422, "invalid", "reviewed_at must be an ISO 8601 date-time with a zone.")
    problem = g.card_problem(body["card"], reviewed_at, stored["due_at"], stored.get("last_review_at"), g.now_utc(),
                             ac.REVIEW_SKEW_S)
    if problem:
        raise Problem(422, "invalid", problem)
    correct = g.is_correct(item, body["chosen"])
    await ctx.store.save_card(ctx.learner_id, item_id, body["card"], g.parse_ts(body["card"]["due"]), reviewed_at)
    await ctx.store.statement(ctx.learner, verb="reviewed", object_type="item", object_id=item_id,
                              body_sha256=g.canonical_sha256({"item": item_id, **body}), bundle_sha=ctx.bundle.sha,
                              result={"correct": correct})
    return {"item_id": item_id, "correct": correct, "correct_choices": sorted(g.answer_set(item)),
            "explain": str(item.get("explain", "")), "card": _card(body["card"])}


# ── labs ─────────────────────────────────────────────────────────────────────────────────────────────────────────

async def lab_check(ctx: Ctx, module_id: str) -> dict:
    module = ac.get_module(ctx, module_id)
    if not module.get("lab"):
        raise ac.not_found("The module lab")
    raise Problem(409, "lab_closed", "Labs open when the lab environments are ready.")


# ── certificates ─────────────────────────────────────────────────────────────────────────────────────────────────

def _certificate_body(row: dict, bundle: Optional[lb.Bundle]) -> dict:
    path = bundle.paths.get(row["path_id"]) if bundle else None
    modules = bundle.modules if bundle else {}
    return {"path_id": row["path_id"], "path_title": row["path_title"], "issued_at": g.iso(row["issued_at"]),
            "expires_at": g.iso(row.get("expires_at")),
            "status": g.certificate_status(row, path, modules, g.now_utc()),
            "module_versions": g.pinned_list(row.get("module_versions"))}


async def list_certificates(ctx: Ctx) -> dict:
    rows = await ctx.store.certificates(ctx.learner_id)
    return {"certificates": [{"certificate_id": r["certificate_id"], **_certificate_body(r, ctx.bundle)}
                             for r in rows]}


async def rotate_code(ctx: Ctx, certificate_id: str) -> dict:
    row = await ctx.store.certificate(ctx.learner_id, certificate_id=certificate_id)
    if row is None:
        raise ac.not_found("The certificate")
    if _certificate_body(row, ctx.bundle)["status"] != "valid":
        raise Problem(409, "conflict", "Only a valid certificate gets a new verification code.")
    code, sha = g.new_code()
    await ctx.store.set_code(certificate_id, sha)
    return {"certificate_id": certificate_id, "path_id": row["path_id"], "code": code,
            "verify_path": f"/verify/{code}"}


async def verify(store, code: str, bundle: Optional[lb.Bundle]) -> dict:
    """The public verify view; 404 for an unknown or rotated code or a learner the retention job deleted."""
    row = await store.verify(g.code_sha256(code))
    if row is None:
        raise ac.not_found("The certificate")
    return {**_certificate_body(row, bundle), "holder_display_name": row.get("holder_display_name")}
