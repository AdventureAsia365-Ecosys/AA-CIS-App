"""AA-900 — classify a failed model call by what a retry can change.

A provider refuses a call for one of three reasons the gateway and the job runner treat differently:

- ``access_denied``: the account may not call the model (Bedrock "Error 002: Access to Bedrock models is not allowed
  for this account" since 29/10/2026 on the LLM account, ``AccessDeniedException ... is not available for this
  account``, an IAM ``not authorized to perform``, an OpenAI 401/403). A retry minutes later is refused the same way.
- ``quota_exhausted``: the account has no credit left (OpenAI ``insufficient_quota``, a 429 that is not a rate
  limit). Same as above for a retry.
- ``throttled``: a rate limit (``ThrottlingException``, ``Too many requests``, an OpenAI rate-limit 429, an
  Anthropic API 429 ``rate_limit_error`` or 529 ``overloaded_error``). A retry with backoff can succeed.

AA-1002 (S2): the Anthropic API adapter sets ``llm_error_class`` on its own errors from the HTTP status (429 / 529
throttled first, 402 / credit or spend-limit wording quota, 401 / 403 access denied); the text patterns below also
carry the Anthropic error types and the credit / spend-limit wording (the adapter reads that wording through
``classify_text`` too), so a stored job error classifies the same way.

Everything else is ``other`` (outage, timeout, bad reply), and keeps today's behaviour. AA-1017 adds ``truncated``
for an ANSWERED reply cut at its token limit (``reply_class``): recorded, never a failure. The first two are the
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
# AA-1017 (design D6): a reply cut at its token limit. Not a failure: the call is billed and its content is returned
# (the caller decides), so it never counts on a circuit and never raises. It is recorded: llm_call_log status and
# error_class 'truncated' (call_log, migration 319) and llm_provider_health may hold it as a class (migration 320).
TRUNCATED = "truncated"
# The stop reasons that mean "cut at the token limit": Anthropic / Bedrock max_tokens, OpenAI length, Anthropic
# model_context_window_exceeded.
TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "length", "model_context_window_exceeded"})
# AA-1018 part 2: a provider that REFUSES to return a reply cut at its token limit (OpenAI 400 "Could not finish the
# message because max_tokens or model output limit was reached", seen live in the S262 drill on 31/10/2026 and logged
# as 'other'). Same class as a cut reply: the request asked for too few tokens, the deployment is healthy, so the
# breaker does not count it and the ledger row says 'truncated'.
_TRUNCATED_ERROR = re.compile(
    r"max_tokens or model output limit was reached"
    r"|could not parse response content as the length limit was reached",   # openai LengthFinishReasonError
    re.IGNORECASE)
# AA-1018 part 2: the route's wall-time budget (llm_role_config.route_deadline_s) ran out before the next attempt.
DEADLINE = "deadline"

# The job failure reason and the prefix of shared.job.error for a job ended by an account-level denial.
LLM_ACCESS_DENIED_REASON = "llm_access_denied"
# AA-642 A1 (migration 326): the ledger class of a call the stage kill switch refused (llm_role_config.is_active false).
DISABLED = "disabled"
_STAGE_DISABLED = re.compile(r"\bstage '([a-z0-9][a-z0-9_]*)' is switched off\b")

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
        r"|unsupported_country_region_territory"
        r"|\bauthentication_error\b"         # AA-1002 S2: Anthropic API 401 / 403 error types
        r"|\bpermission_error\b",
        re.IGNORECASE)),
    (QUOTA_EXHAUSTED, re.compile(
        r"insufficient_quota"
        r"|exceeded your current quota"
        r"|billing_hard_limit_reached"
        r"|\bbilling_error\b"                # AA-1002 S2: Anthropic API 402
        r"|credit balance is too low"        # AA-1002 S2: Anthropic API 400, no prepaid credit left
        r"|\bapi usage limits?\b"            # AA-1002 S2: Anthropic API 400, workspace / org spend limit reached
        r"|\bspend(ing)? limit\b"
        r"|\benforced_spend_limit_reached\b"  # AA-642 S0: Anthropic API 429, the usage tier's monthly spend cap
        r"|you have no credits remaining"   # S259: OpenAI 429 on a prepaid org with no credit (not a rate limit)
        r"|add credits to continue",
        re.IGNORECASE)),
    (THROTTLED, re.compile(
        r"throttlingexception"
        r"|too many requests"
        r"|rate exceeded"
        r"|ratelimiterror"
        r"|rate_limit_exceeded"
        r"|servicequotaexceededexception"
        r"|\brate_limit_error\b"             # AA-1002 S2: Anthropic API 429
        r"|\boverloaded_error\b",            # AA-1002 S2: Anthropic API 529 (retry with backoff)
        re.IGNORECASE)),
)

# The message prefix and class name LLMAccessDenied adds, so a job error written from it classifies again.
_OWN_MARKER = re.compile(r"\bllm_access_denied\b|\bLLMAccessDenied\b")


class RouteDeadlineExceeded(RuntimeError):
    """AA-1018 part 2: the stage's route deadline (``llm_role_config.route_deadline_s``) passed before the next model
    or deployment could be tried. Raised by the gateway without a provider call; ``llm_error_class`` = DEADLINE (ledger
    error_class 'deadline', status 'error'). Not account-level: a job retries it with its normal backoff."""

    llm_error_class = DEADLINE


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


class LLMStageDisabled(LLMAccessDenied):
    """AA-642 A1: the stage is switched off (``llm_role_config.is_active = false``, the admin kill switch), so the
    gateway refused the call before any provider attempt (ledger: status refused, error_class ``disabled``, 0 USD).
    An LLMAccessDenied on purpose: a retry cannot help until someone switches the stage on, so the job runner fails
    the job at once (``llm_access_denied: stage '<stage>' is switched off ...``). The T2 graph / rewrite re-raise it as
    an account refusal; the T9 write re-raises it by ``stage_disabled_in`` (review S263: it used to mark the piece
    failed). The T2 / T9 ``on_terminal`` hooks leave the version / piece to do for such a job
    (``is_stage_switch_hold``), and the bulk re-queue ("Re-queue refused jobs", prefix ``llm_access_denied``) holds
    it while its stage is still off (``shared/jobs/queue.llm_hold``), then writes it. That is the declared degrade
    mode ``queue_retry``."""

    def __init__(self, stage: str, reason: Optional[str] = None):
        why = f": {reason.strip()[:300]}" if isinstance(reason, str) and reason.strip() else ""
        super().__init__(f"stage {stage!r} is switched off by an admin{why}", DISABLED)
        self.stage = stage


def disabled_stage_of(text: Optional[str]) -> Optional[str]:
    """The stage a stored refusal of the kill switch names (``LLMStageDisabled``'s message), else None. Pure."""
    m = _STAGE_DISABLED.search(text or "")
    return m.group(1) if m else None


def stage_disabled_in(exc: Optional[BaseException]) -> Optional[LLMStageDisabled]:
    """The ``LLMStageDisabled`` in ``exc`` or its explicit cause chain, else None (review S263: the T-stage pipelines
    re-raise it instead of turning it into a failed piece)."""
    if exc is None:
        return None
    for link in _chain(exc, implicit=False):
        if isinstance(link, LLMStageDisabled):
            return link
    return None


def is_stage_switch_hold(job_row: Optional[dict]) -> bool:
    """True for a job the stage kill switch failed (status ``failed``, its error names a switched-off stage). The T2 /
    T9 ``on_terminal`` hooks leave its version / piece pending so the re-queue after the stage is switched on writes
    it; an admin Cancel ends it ``cancelled`` and the hook marks it failed as for any other stop. Pure."""
    if not job_row or job_row.get("status") != "failed":
        return False
    return disabled_stage_of(job_row.get("error")) is not None


def is_truncated(stop_reason: Optional[str]) -> bool:
    """AA-1017: True when a reply's stop reason says it was cut at its token limit (the TRUNCATED class)."""
    return isinstance(stop_reason, str) and stop_reason in TRUNCATED_STOP_REASONS


def reply_class(stop_reason: Optional[str]) -> Optional[str]:
    """AA-1017: the class of a reply that was answered: TRUNCATED when it was cut at its token limit, else None (a
    complete answer). The ledger writes it as status / error_class 'truncated'; the call still counts as a success."""
    return TRUNCATED if is_truncated(stop_reason) else None


def is_truncated_error(text: Optional[str]) -> bool:
    """AA-1018 part 2: True when a provider error says the reply hit its token limit (``_TRUNCATED_ERROR``)."""
    return bool(text) and _TRUNCATED_ERROR.search(text) is not None


def classify_text(text: Optional[str]) -> str:
    """The error class of one message (an exception's str, a stored job error). AA-1018 part 2: a refusal to return a
    reply cut at its token limit is TRUNCATED (checked first: the OpenAI 400 also carries "invalid_request_error")."""
    if not text:
        return OTHER
    if disabled_stage_of(text):          # AA-642 A1: first, the admin's reason text may contain any word
        return DISABLED
    if is_truncated_error(text):
        return TRUNCATED
    for cls, pattern in _PATTERNS:
        if pattern.search(text):
            return cls
    if _OWN_MARKER.search(text):
        return ACCESS_DENIED
    return OTHER


# AA-1018: account IDs and ARNs (an IAM / STS refusal names the caller's role and the target role) are masked before a
# provider message leaves the platform in an alert (bell payload, ops e-mail). Same 12-digit rule as the CI
# resource-ID guard (.github/scripts/check_resource_ids.py) and api/services/docs_reader.py.
_ARN = re.compile(r"\barn:aws[a-z-]*:[^\s,;)'\"]*", re.IGNORECASE)
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN_MARKER = "arn:aws:<redacted>"
ACCOUNT_ID_MARKER = "<account-id>"


def redact_ids(text: Optional[str]) -> Optional[str]:
    """``text`` with every ARN replaced by ``ARN_MARKER`` and every other 12-digit run (an AWS account ID) by
    ``ACCOUNT_ID_MARKER``. None stays None. Pure."""
    if not text:
        return text
    return _ACCOUNT_ID.sub(ACCOUNT_ID_MARKER, _ARN.sub(ARN_MARKER, text))


def _chain(exc: BaseException, *, implicit: bool = True) -> Iterator[BaseException]:
    """The exception, then its explicit causes and (when ``implicit``) implicit contexts (cycle-safe, bounded)."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < 16:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or (cur.__context__ if implicit else None)


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
    # Explicit causes only (lessons: job runner, NodeCancelledError): an unrelated error raised while handling a
    # refusal must not end its job without a retry.
    return any(isinstance(link, LLMAccessDenied) for link in _chain(error, implicit=False))


def account_level_class(classes: list[str]) -> Optional[str]:
    """The class to raise LLMAccessDenied with when EVERY attempt (``classes``, one per model tried) was refused at
    account level; None when the list is empty or any attempt failed another way (that model may answer on retry)."""
    if not classes or any(c not in ACCOUNT_LEVEL for c in classes):
        return None
    return QUOTA_EXHAUSTED if all(c == QUOTA_EXHAUSTED for c in classes) else ACCESS_DENIED
