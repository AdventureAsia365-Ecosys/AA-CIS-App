"""Pure rules of the AA Academy learning API (AA-1105; research docs/research/lms §4.3, contract learn.http.v1).

No I/O here: drawing quiz items, grading a submission, the spaced-review card checks, the certificate status and the
verification code. The thresholds (pass marks, items per attempt, the retake delay, the recertification period and
paths) are passed in by the caller, which reads them from the CIS settings registry (``cis.learn.*``).
"""
from __future__ import annotations

import hashlib
import json
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

CODE_BYTES = 16  # technical-constant: 128-bit verification code, 22 URL-safe base64 characters (ADR 0022 d11)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_sha256(body: Any) -> str:
    """SHA-256 (hex) of the canonical JSON of a request body (sorted keys, no spaces): the idempotency fingerprint."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_code() -> tuple[str, str]:
    """(code, sha256 hex of the code). Only the hash is stored."""
    code = secrets.token_urlsafe(CODE_BYTES)
    return code, code_sha256(code)


def code_sha256(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def major(version: Any) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except ValueError:
        return 0


# ── quiz items ───────────────────────────────────────────────────────────────────────────────────────────────────────

def item_kind(item: dict) -> str:
    """The contract's kind: ``single`` or ``multiple`` (the course format says ``multi``)."""
    return "single" if item.get("kind") == "single" else "multiple"


def public_item(item: dict) -> dict:
    """A quiz item as a learner may see it before a submission: never the answer, the explanation or the source."""
    return {"id": item["id"], "kind": item_kind(item), "prompt": str(item.get("prompt", "")),
            "choices": [str(c) for c in item.get("choices", [])]}


def live_items(items: Iterable[dict]) -> list[dict]:
    """Items that may be drawn: not suspended (their cited section changed since the lesson was pinned)."""
    return [i for i in items if not i.get("suspended")]


def draw(items: list[dict], count: int, rng: Optional[random.Random] = None) -> list[dict]:
    """``count`` items at random (all of them when there are fewer), in a random order."""
    pick = rng or random.SystemRandom()
    count = max(0, min(count, len(items)))
    return pick.sample(items, count)


def answer_set(item: dict) -> set[int]:
    return {int(a) for a in item.get("answer", []) if isinstance(a, int) and not isinstance(a, bool)}


def is_correct(item: dict, chosen: Iterable[int]) -> bool:
    return set(chosen) == answer_set(item)


def grade(item_ids: list[str], items_by_id: dict[str, dict], responses: dict[str, list[int]]) -> tuple[float, list]:
    """Score (percent, two decimals) and one graded row per bound item still in the bundle. An item the bundle no
    longer has is left out of the score; an item without a response counts as wrong."""
    graded = []
    for item_id in item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            continue
        chosen = sorted(set(responses.get(item_id, [])))
        graded.append({"item_id": item_id, "correct": is_correct(item, chosen), "chosen": chosen,
                       "correct_choices": sorted(answer_set(item)), "explain": str(item.get("explain", "")),
                       "source_ref": str(item.get("source", ""))})
    if not graded:
        return 0.0, graded
    score = round(100.0 * sum(1 for g in graded if g["correct"]) / len(graded), 2)
    return score, graded


def retake_at(last_failed_at: Optional[datetime], delay_hours: float, now: datetime) -> Optional[datetime]:
    """When the next attempt opens after a failed one, or None when it is open now."""
    if last_failed_at is None:
        return None
    opens = last_failed_at + timedelta(hours=float(delay_hours))
    return opens if opens > now else None


# ── spaced review (ts-fsrs card computed in the browser) ────────────────────────────────────────────────────────────

def new_card(now: datetime) -> dict:
    """A ts-fsrs ``createEmptyCard`` equivalent: due now, state New."""
    return {"due": iso(now), "stability": 0, "difficulty": 0, "elapsed_days": 0, "scheduled_days": 0,
            "learning_steps": 0, "reps": 0, "lapses": 0, "state": 0, "last_review": None}


def parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def card_problem(card: dict, reviewed_at: datetime, stored_due: datetime, stored_last: Optional[datetime],
                 now: datetime, skew_s: float) -> Optional[str]:
    """Why the browser's card state is refused, or None. The stored schedule only moves forward: its due date is not
    earlier than the stored one, its last review is the submitted review time, and that time is neither in the future
    nor before the stored last review (contract learn.http.v1, postReviewResult)."""
    due = parse_ts(card.get("due"))
    last = parse_ts(card.get("last_review"))
    if due is None or last is None:
        return "card due and last_review must be ISO 8601 date-times with a zone"
    if last != reviewed_at:
        return "card last_review must equal reviewed_at"
    if reviewed_at > now + timedelta(seconds=skew_s):
        return "reviewed_at is in the future"
    if stored_last is not None and reviewed_at < stored_last:
        return "reviewed_at is before the last stored review"
    if due < stored_due:
        return "card due may not be earlier than the stored due date"
    return None


# ── certificates ─────────────────────────────────────────────────────────────────────────────────────────────────────

def add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + int(months)
    year, month = value.year + month // 12, month % 12 + 1
    day = min(value.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31,
                          31, 30, 31, 30, 31][month - 1])
    return value.replace(year=year, month=month, day=day)


def certificate_status(row: dict, path: Optional[dict], modules: dict[str, dict], now: datetime) -> str:
    """valid, expired, renewal_due or revoked. renewal_due when the stored status says so, or a required module of
    the path has a higher major version than the pinned one, or a required module was added after issue."""
    if row["status"] == "revoked":
        return "revoked"
    expires = row.get("expires_at")
    if expires is not None and expires <= now:
        return "expired"
    if row["status"] == "renewal_due":
        return "renewal_due"
    pinned = row.get("module_versions") or {}
    if path is not None:
        for entry in path.get("modules", []):
            if not entry.get("required"):
                continue
            module = modules.get(entry["id"])
            if module is None or module.get("status") != "active":
                continue
            if entry["id"] not in pinned or major(module.get("version")) > major(pinned[entry["id"]]):
                return "renewal_due"
    return "valid"


def renewal_reason(row: Optional[dict], path: dict, modules: dict[str, dict], now: datetime) -> Optional[str]:
    """Why an existing certificate is issued again (CHECK certificate_renewal_check), None for a first issue."""
    if row is None:
        return None
    status = certificate_status(row, path, modules, now)
    if status == "expired":
        return "expired"
    pinned = row.get("module_versions") or {}
    required = [e["id"] for e in path.get("modules", []) if e.get("required")]
    if any(m not in pinned for m in required):
        return "module_added"
    return "major_version"


def pinned_list(module_versions: dict) -> list[dict]:
    return [{"module_id": k, "version": str(v)} for k, v in sorted((module_versions or {}).items())]


def badge(path: dict) -> dict:
    """Open Badges 3.0 achievement fields of a path certificate (no personal data)."""
    return {"type": ["Achievement"], "achievement": {"id": path["id"], "name": path.get("title", path["id"]),
                                                     "achievementType": "Certificate"},
            "criteria": "Every required module complete and the path quiz passed (AA Academy).",
            "issuer": "Adventure Asia"}
