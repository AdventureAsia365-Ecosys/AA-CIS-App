"""AA-900 — classify a failed model call by what a retry can change.

A provider refuses a call for one of three reasons the gateway and the job runner treat differently:

- ``access_denied``: the account may not call the model (Bedrock "Error 002: Access to Bedrock models is not allowed
  for this account" since 29/10/2026 on the LLM account, ``AccessDeniedException ... is not available for this
  account``, an IAM ``not authorized to perform``, an OpenAI 401/403). A retry minutes later is refused the same way.
- ``quota_exhausted``: the account has no credit left (OpenAI ``insufficient_quota``, a 429 that is not a rate
  limit). Same as above for a retry.
- ``throttled``: a rate limit (``ThrottlingException``, ``Too many requests``, an OpenAI rate-limit 429). A retry
  with backoff can succeed.

Everything else is ``other`` (outage, timeout, bad reply), and keeps today's behaviour. The first two are the
"account-level" classes: when every model a request tried failed with one of them, the gateway raises
:class:`LLMAccessDenied` and the job runner fails the job at once with the reason ``llm_access_denied`` instead of
burning its retries (``shared/jobs/worker.py``). The admin's LLM availability card reads the same classes
(``api/services/llm_availability.py``).

Pure functions on exception text: no provider call, no import of boto3 / openai here.
"""
from __future__ import annotations

import re
from typing import Iterator, Optional, Union

ACCESS_DENIED = "access_denied"
QUOTA_EXHAUSTED = "quota_exhausted"
THROTTLED = "throttled"
OTHER = "other"

# The job failure reason and the prefix of shared.job.error for a job ended by an account-level denial.
LLM_ACCESS_DENIED_REASON = "llm_access_denied"

ACCOUNT_LEVEL = frozenset({ACCESS_DENIED, QUOTA_EXHAUSTED})

# Order matters: a quota 429 also says "429", so it is checked before throttling.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (ACCESS_DENIED, re.compile(
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
        r"|unsupported_country_region_territory",
        re.IGNORECASE)),
    (QUOTA_EXHAUSTED, re.compile(
        r"insufficient_quota"
        r"|exceeded your current quota"
        r"|billing_hard_limit_reached",
        re.IGNORECASE)),
    (THROTTLED, re.compile(
        r"throttlingexception"
        r"|too many requests"
        r"|rate exceeded"
        r"|ratelimiterror"
        r"|rate_limit_exceeded"
        r"|servicequotaexceededexception",
        re.IGNORECASE)),
)

# The message prefix and class name LLMAccessDenied adds, so a job error written from it classifies again.
_OWN_MARKER = re.compile(r"\bllm_access_denied\b|\bLLMAccessDenied\b")


class LLMAccessDenied(RuntimeError):
    """Every model a request tried was refused at account level (access denied / no credit). A RuntimeError, so a
    caller that already catches RuntimeError behaves as before; the job runner fails the job without a retry.
    ``error_class`` is ACCESS_DENIED, or QUOTA_EXHAUSTED when every refusal was a credit one."""

    reason = LLM_ACCESS_DENIED_REASON

    def __init__(self, message: str, error_class: str = ACCESS_DENIED):
        super().__init__(message, error_class)
        self.llm_error_class = error_class

    def __str__(self) -> str:
        return f"{LLM_ACCESS_DENIED_REASON}: {self.args[0]}"


def classify_text(text: Optional[str]) -> str:
    """The error class of one message (an exception's str, a stored job error)."""
    if not text:
        return OTHER
    for cls, pattern in _PATTERNS:
        if pattern.search(text):
            return cls
    if _OWN_MARKER.search(text):
        return ACCESS_DENIED
    return OTHER


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception, then its explicit causes and implicit contexts (cycle-safe, bounded)."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def classify(error: Union[BaseException, str, None]) -> str:
    """The error class of an exception or a message. For an exception the cause chain is walked (the gateway wraps
    provider errors); the first link that carries ``llm_error_class`` (set by the gateway on every "all models
    failed" error, so a mixed route never reads as denied from its last cause) answers, else the first link whose
    text is not ``other``."""
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


def is_llm_access_denied(error: Optional[BaseException]) -> bool:
    """True when the gateway decided that a retry cannot help: a typed LLMAccessDenied in the cause chain. The job
    runner's retry decision reads only this, never the text (an aggregate message that names one denied model among
    throttled ones must keep its retries)."""
    if error is None:
        return False
    return any(isinstance(link, LLMAccessDenied) for link in _chain(error))


def account_level_class(classes: list[str]) -> Optional[str]:
    """The class to raise LLMAccessDenied with when EVERY attempt (``classes``, one per model tried) was refused at
    account level; None when the list is empty or any attempt failed another way (that model may answer on retry)."""
    if not classes or any(c not in ACCOUNT_LEVEL for c in classes):
        return None
    return QUOTA_EXHAUSTED if all(c == QUOTA_EXHAUSTED for c in classes) else ACCESS_DENIED
