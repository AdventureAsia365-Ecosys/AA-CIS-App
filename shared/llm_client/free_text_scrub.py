"""AA-1113 (root ADR 0009 decision 10, accepted S283): the free-text PII scrub of the gateway, fail closed.

A stage that carries tenant free text (:data:`FREE_TEXT_STAGES`) never sends a prompt to a model before this scrub has
run on it. ``LLMClient._generate_with`` (and the admin ``probe``) call :func:`scrub_for_stage` after the switch and
budget gates and before the first provider attempt, so every path that follows (the stage route, a legacy key, the
truncation retry, the A/B shadow, the cassette) only ever sees the scrubbed request. Any other stage gets the very same
request object back (no copy, no change: the catalogue stages ``s1_*``, ``a3_*`` and ``tp_compose`` carry no tenant
text, S283 measure-first).

**Fail closed.** When the scrub cannot run (any error inside it), or its result still holds an e-mail address or a
``+``-prefixed phone number, :class:`FreeTextScrubFailed` is raised: the gateway writes its 0 USD ledger row (status
``refused``, error_class ``other``, ``quality_signal.pii_scrub.status = failed``; the CHECK of
``shared.llm_call_log.error_class`` has no scrub class yet) and the call is refused. Nothing is ever sent unscrubbed.

**What is redacted** (the CRM's log scrub, AA-Partner ``api/app/observability/scrub.py`` ``scrub_text``, AA-871 and
AA-938, ported, never imported): JWTs, Bearer / Basic credentials, key-like ``name=value`` parameters, Postgres key
details, e-mail addresses and ``+``-prefixed phone numbers, then the F3 detectors of
:mod:`shared.llm_client.pii_patterns` (signed links, cards, Vietnamese CCCD numbers, passports, phones without a
``+``, OTPs after a code cue). Dates are left alone (a prompt is full of trip dates). A value becomes a bracket label
(``[email]``, ``[phone]``, ...); nothing is restored in the reply. Free-text names cannot be found by a pattern: the
CRM's known-value and detector passes (its own gateway, root ADR 0008a section 5) are not part of this port.

**Receipt.** Counts per label only (``{"version": ..., "redacted": {"[email]": 1}}``), set on the request's attempt
scope (``quality_signal.pii_scrub`` of the gateway's own rows) and logged as ``llm_free_text_scrubbed``; never a value.

**Which stages.** A code allow-list for this slice: ``shared.llm_role_config.pii_class`` (CIS 373) has ``content`` and
``pii`` only, and a ``pii`` stage is a different rule (US AWS deployments only, no scrub: it needs the data). Moving the
flag into the stage row is a migration (ADR 0009 decision 10: "a scope change happens only by migration"); see
docs/implementation-notes/AA-1113.md.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

import structlog

from . import pii_patterns as patterns
from .errors import OTHER

logger = structlog.get_logger()

# The rules of the scrub: bumped whenever the same prompt can scrub to a different text.
VERSION = "aa1113-1"
# The stages whose prompts carry tenant free text (S283 measure-first, root
# docs/audits/2026-11-17-S283-session/aa1113-provider-stage-30d.psv; tech lead decision s283-d10).
FREE_TEXT_STAGES = frozenset({"t9_write", "t2_generate", "t8_angle_gen"})
SCRUB_STATUS_FAILED = "failed"
SCRUB_STATUS_OK = "scrubbed"

FILTERED = "[Filtered]"

# Starts only where a local part can start: a long run without "@" is scanned once, not once per character.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,18}\d")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# Key-like query parameters, plus key=value pairs in text.
_SECRET_PARAM = re.compile(
    r"(?i)\b((?:api_?key|apikey|access_token|refresh_token|id_token|token|key|password|secret|code)=)[^&\s'\"]+"
)
# Postgres unique-violation detail: Key (email)=(a@b.c) already exists.
_PG_DETAIL = re.compile(r"(?i)\(((?:[a-z_]*(?:email|phone|name|address)[a-z_]*)(?:,\s*[a-z_]+)*)\)=\([^)]*\)")

# Kinds of pii_patterns the scrub redacts (dates are left alone).
_F3_KINDS = {
    "LINK": "[link]",
    "SECRET": "[jwt]",
    "CARD": "[card]",
    "NATIONAL_ID": "[national_id]",
    "PASSPORT": "[passport]",
    "PHONE": "[phone]",
    "OTP": "[otp]",
}
_HAS_DIGIT_OR_LINK = re.compile(r"\d|https?://", re.IGNORECASE)


class FreeTextScrubFailed(RuntimeError):
    """The free-text scrub of ``stage`` could not run, or left an e-mail / phone in the prompt: the gateway refused
    the call before any provider attempt (fail closed). The message names the stage and the failure type only, never
    the text. Not account-level: a job retries it with its normal backoff (0 USD each time)."""

    llm_error_class = OTHER

    def __init__(self, stage: Optional[str], reason: str):
        super().__init__(stage, reason)    # every arg, so pickle / copy rebuild it (B042)
        self.stage = stage
        self.reason = reason

    def __str__(self) -> str:
        return (f"free-text PII scrub failed for stage {self.stage!r} ({self.reason}): the call was refused, "
                "nothing was sent")


def is_free_text_stage(stage: Optional[str]) -> bool:
    return isinstance(stage, str) and stage in FREE_TEXT_STAGES


def _sub(rx: re.Pattern[str], repl, text: str, label: str, counts: Counter) -> str:
    text, n = rx.subn(repl, text)
    if n:
        counts[label] += n
    return text


def scrub_text(text: str, counts: Optional[Counter] = None) -> str:
    """Redact personal data and credentials inside one string (the CRM log scrub's ``scrub_text``, same order).
    ``counts`` (optional) gains one per redaction, keyed by label."""
    counts = counts if counts is not None else Counter()
    if not text:
        return text
    text = _sub(_JWT, "[jwt]", text, "[jwt]", counts)
    text = _sub(_BEARER, lambda m: f"{m.group(1)} {FILTERED}", text, FILTERED, counts)
    text = _sub(_SECRET_PARAM, lambda m: f"{m.group(1)}***", text, "***", counts)
    text = _sub(_PG_DETAIL, lambda m: f"({m.group(1)})=({FILTERED})", text, FILTERED, counts)
    text = _sub(_EMAIL, "[email]", text, "[email]", counts)
    text = _sub(_PHONE, "[phone]", text, "[phone]", counts)
    return _scrub_f3(text, counts)


def _scrub_f3(text: str, counts: Counter) -> str:
    if not _HAS_DIGIT_OR_LINK.search(text):
        return text
    out, pos = [], 0
    for span in patterns.find(text):
        label = _F3_KINDS.get(span.kind)
        if label is None:
            continue
        out.append(text[pos : span.start])
        out.append(label)
        counts[label] += 1
        pos = span.end
    out.append(text[pos:])
    return "".join(out)


def _scrub_value(value: Any, counts: Counter, depth: int = 0) -> Any:
    """Strings inside a few-shot message (dicts, lists, tuples) scrubbed; other types unchanged."""
    if depth > 12:
        raise ValueError("few-shot structure nested too deep to scrub")
    if isinstance(value, str):
        return scrub_text(value, counts)
    if isinstance(value, dict):
        return {k: _scrub_value(v, counts, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v, counts, depth + 1) for v in value)
    return value


def _residual(texts: list[str]) -> bool:
    """True when a scrubbed text still holds an e-mail or a ``+`` phone (the self-check of the fail-closed rule)."""
    return any(_EMAIL.search(t) or _PHONE.search(t) for t in texts if t)


def scrub_request(request):
    """``(scrubbed copy of request, counts per label)``: system prompt, user prompt and few-shot strings. Raises
    whatever the scrub raises (the caller turns it into :class:`FreeTextScrubFailed`)."""
    counts: Counter = Counter()
    system = scrub_text(request.system_prompt, counts)
    user = scrub_text(request.user_prompt, counts)
    shots = [_scrub_value(s, counts) for s in (request.few_shots or [])]
    if _residual([system, user, *(str(s) for s in shots)]):
        raise ValueError("residual e-mail or phone after the scrub")
    return request.model_copy(update={"system_prompt": system, "user_prompt": user, "few_shots": shots}), counts


def _note(scope, receipt: dict) -> None:
    """The receipt on the request's attempt scope (its quality_signal extras), when there is one."""
    extra = getattr(scope, "extra", None)
    if isinstance(extra, dict):
        extra["pii_scrub"] = receipt


def scrub_for_stage(request, scope=None):
    """The request a free-text stage may send: a scrubbed copy, or ``request`` itself (same object) for any other
    stage. Raises :class:`FreeTextScrubFailed` (fail closed) when the scrub cannot run; the receipt goes on ``scope``
    either way. Never logs a value."""
    stage = getattr(request, "stage", None)
    if not is_free_text_stage(stage):
        return request
    try:
        scrubbed, counts = scrub_request(request)
    except Exception as e:  # noqa: BLE001 — any failure refuses the call (fail closed)
        _note(scope, {"version": VERSION, "status": SCRUB_STATUS_FAILED, "error_type": type(e).__name__})
        logger.warning("llm_free_text_scrub_failed", stage=stage, error_type=type(e).__name__, version=VERSION)
        raise FreeTextScrubFailed(stage, type(e).__name__) from e
    receipt = {"version": VERSION, "status": SCRUB_STATUS_OK, "redacted": dict(sorted(counts.items()))}
    _note(scope, receipt)
    if counts:
        logger.info("llm_free_text_scrubbed", stage=stage, version=VERSION, redacted=receipt["redacted"])
    return scrubbed
