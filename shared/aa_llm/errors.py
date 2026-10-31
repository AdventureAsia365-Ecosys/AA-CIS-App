"""Error classes of a failed model call, by what a retry can change (root ADR 0009 decision 6).

Same rules as AA-CIS-App ``shared/llm_client/errors.py`` (AA-900, AA-1002 S2, AA-1018), copied here as the shared
core (AA-642 S8, ADR 0009 decision 13); ``tests/unit/test_aa642_s8_aa_llm_parity.py`` in AA-CIS-App proves both
classify every case of its table the same way until the CIS module re-exports this one.

- ``access_denied``: the account may not call the model (a Bedrock account-level refusal, an IAM "not authorized
  to perform", an OpenAI 401/403, an Anthropic API 401/403). A retry minutes later is refused the same way.
- ``quota_exhausted``: no credit left or a spend limit reached (OpenAI ``insufficient_quota``, Anthropic credit or
  spend-limit wording). Same as above for a retry.
- ``throttled``: a rate limit (``ThrottlingException``, "Too many requests", an Anthropic 429 / 529). A retry with
  backoff can succeed.
- ``other``: everything else (outage, timeout, bad reply).

The ledger (``shared.llm_call_log.error_class``, migration 319) also stores ``credential``, ``deadline``, ``budget``
and ``truncated``; those are set by the caller that knows them, never derived from text here.

Pure functions on exception text: no provider SDK, no I/O, standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

ACCESS_DENIED = "access_denied"
QUOTA_EXHAUSTED = "quota_exhausted"
THROTTLED = "throttled"
OTHER = "other"
# Ledger-only classes (set by the caller, never by classify): a missing or rejected credential, the route deadline,
# a budget refusal, a reply cut at its token limit.
CREDENTIAL = "credential"
DEADLINE = "deadline"
BUDGET = "budget"
TRUNCATED = "truncated"

# Every value shared.llm_call_log.error_class accepts (migration 319 CHECK).
LEDGER_ERROR_CLASSES = (ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED, CREDENTIAL, OTHER, DEADLINE, BUDGET, TRUNCATED)

# The job failure reason and the prefix of a job error for a job ended by an account-level denial.
LLM_ACCESS_DENIED_REASON = "llm_access_denied"

ACCOUNT_LEVEL = frozenset({ACCESS_DENIED, QUOTA_EXHAUSTED})

# Order matters: a quota 429 also says "429", so it is checked before throttling.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        ACCESS_DENIED,
        re.compile(
            r"access to bedrock models is not allowed"
            r"|\berror 002\b"
            r"|accessdeniedexception"
            r"|\(accessdenied\)"
            r"|is not available for this account"
            r"|not authorized to perform"
            r"|you don't have access to the model"
            r"|permissiondeniederror"
            r"|authenticationerror"
            r"|invalid_api_key"
            r"|unsupported_country_region_territory"
            r"|\bauthentication_error\b"
            r"|\bpermission_error\b",
            re.IGNORECASE,
        ),
    ),
    (
        QUOTA_EXHAUSTED,
        re.compile(
            r"insufficient_quota"
            r"|exceeded your current quota"
            r"|billing_hard_limit_reached"
            r"|\bbilling_error\b"
            r"|credit balance is too low"
            r"|\bapi usage limits?\b"
            r"|\bspend(ing)? limit\b"
            r"|\benforced_spend_limit_reached\b"
            r"|you have no credits remaining"
            r"|add credits to continue",
            re.IGNORECASE,
        ),
    ),
    (
        THROTTLED,
        re.compile(
            r"throttlingexception"
            r"|too many requests"
            r"|rate exceeded"
            r"|ratelimiterror"
            r"|rate_limit_exceeded"
            r"|servicequotaexceededexception"
            r"|\brate_limit_error\b"
            r"|\boverloaded_error\b",
            re.IGNORECASE,
        ),
    ),
)

# The message prefix and class name LLMAccessDenied adds, so a job error written from it classifies again.
_OWN_MARKER = re.compile(r"\bllm_access_denied\b|\bLLMAccessDenied\b")


class LLMAccessDenied(RuntimeError):
    """Every model a request tried was refused at account level (access denied / no credit). A RuntimeError, so a
    caller that already catches RuntimeError behaves as before. ``llm_error_class`` is ACCESS_DENIED, or
    QUOTA_EXHAUSTED when every refusal was a credit one."""

    reason = LLM_ACCESS_DENIED_REASON

    def __init__(self, message: str, error_class: str = ACCESS_DENIED):
        super().__init__(message, error_class)
        self.llm_error_class = error_class

    def __str__(self) -> str:
        return f"{LLM_ACCESS_DENIED_REASON}: {self.args[0]}"


def classify_text(text: str | None) -> str:
    """The error class of one message (an exception's str, a stored job error)."""
    if not text:
        return OTHER
    for cls, pattern in _PATTERNS:
        if pattern.search(text):
            return cls
    if _OWN_MARKER.search(text):
        return ACCESS_DENIED
    return OTHER


# Account IDs and ARNs are masked before a provider message leaves the platform (an alert, a log line).
_ARN = re.compile(r"\barn:aws[a-z-]*:[^\s,;)'\"]*", re.IGNORECASE)
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN_MARKER = "arn:aws:<redacted>"
ACCOUNT_ID_MARKER = "<account-id>"


def redact_ids(text: str | None) -> str | None:
    """``text`` with every ARN replaced by ``ARN_MARKER`` and every other 12-digit run (an AWS account ID) by
    ``ACCOUNT_ID_MARKER``. None stays None."""
    if not text:
        return text
    return _ACCOUNT_ID.sub(ACCOUNT_ID_MARKER, _ARN.sub(ARN_MARKER, text))


def _chain(exc: BaseException, *, implicit: bool = True) -> Iterator[BaseException]:
    """The exception, then its explicit causes and (when ``implicit``) implicit contexts (cycle-safe, bounded)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or (cur.__context__ if implicit else None)


def classify(error: BaseException | str | None) -> str:
    """The error class of an exception or a message. For an exception the cause chain is walked; the first link that
    carries ``llm_error_class`` answers (unless an earlier link already classified), else the first link whose text
    is not ``other``."""
    if error is None:
        return OTHER
    if isinstance(error, str):
        return classify_text(error)
    found = OTHER
    for link in _chain(error):
        decided = getattr(link, "llm_error_class", None)
        if decided:
            return decided if found == OTHER else found
        cls = classify_text(f"{type(link).__name__}: {link}")
        if found == OTHER and cls != OTHER:
            found = cls
    return found


def is_llm_access_denied(error: BaseException | None) -> bool:
    """True when a typed LLMAccessDenied is in the explicit cause chain (never decided from text)."""
    if error is None:
        return False
    return any(isinstance(link, LLMAccessDenied) for link in _chain(error, implicit=False))


def account_level_class(classes: list[str]) -> str | None:
    """The class to raise LLMAccessDenied with when EVERY attempt (``classes``, one per model tried) was refused at
    account level; None when the list is empty or any attempt failed another way."""
    if not classes or any(c not in ACCOUNT_LEVEL for c in classes):
        return None
    return QUOTA_EXHAUSTED if all(c == QUOTA_EXHAUSTED for c in classes) else ACCESS_DENIED
