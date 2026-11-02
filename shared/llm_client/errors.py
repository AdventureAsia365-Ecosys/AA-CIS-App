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

AA-1024 A10 (root ADR 0009 decision 13): the classes, the text patterns, ``classify`` and ``LLMAccessDenied`` are the
shared core's (``shared/aa_llm/errors.py``, the module TripPlanner and the CRM vendor), imported here, so CIS and the
other apps classify a failed call with one code. This module adds the CIS gateway's own exceptions (route deadline,
attempt cap, served role, stage kill switch) and the kill-switch helpers of the job runner.

Pure functions on exception text: no provider call, no import of boto3 / openai here.
"""

from __future__ import annotations

from typing import Optional

# AA-1024 A10: the shared core. Every name below is the core's own object (tests/unit/test_aa1024_a10_core_identity.py).
# Re-exported: the gateway, the job runner and the admin import these names from here.
from shared.aa_llm.errors import (  # noqa: F401
    _OWN_MARKER,
    _PATTERNS,
    _STAGE_DISABLED,
    _TRUNCATED_ERROR,
    ACCESS_DENIED,
    ACCOUNT_ID_MARKER,
    ACCOUNT_LEVEL,
    ARN_MARKER,
    BUDGET,
    CREDENTIAL,
    DEADLINE,
    DISABLED,
    LEDGER_ERROR_CLASSES,
    LLM_ACCESS_DENIED_REASON,
    OTHER,
    QUOTA_EXHAUSTED,
    ROLE_NOT_SERVED,
    THROTTLED,
    TRUNCATED,
    TRUNCATED_STOP_REASONS,
    LLMAccessDenied,
    _chain,
    account_level_class,
    classify,
    classify_text,
    disabled_stage_of,
    is_llm_access_denied,
    is_truncated,
    is_truncated_error,
    redact_ids,
    reply_class,
)


class RouteDeadlineExceeded(RuntimeError):
    """AA-1018 part 2: the stage's route deadline (``llm_role_config.route_deadline_s``) passed before the next model
    or deployment could be tried. Raised by the gateway without a provider call; ``llm_error_class`` = DEADLINE (ledger
    error_class 'deadline', status 'error'). Not account-level: a job retries it with its normal backoff."""

    llm_error_class = DEADLINE


# AA-642 A3 (root ADR 0009 decision 5, gap RW-11): "the gateway owns retries (SDK retries off, at most six attempts per
# chain)". The attempts of one gateway request (every model of the route, every deployment of a model, every legacy
# tier) are counted; the seventh is never started.
MAX_CHAIN_ATTEMPTS = 6


class RouteAttemptCapReached(RouteDeadlineExceeded):
    """AA-642 A3: the request already made ``MAX_CHAIN_ATTEMPTS`` provider attempts, so the gateway starts no other
    one. A route bound like the deadline (same handling, ledger error_class 'deadline', 0 USD, no provider call), so
    every path that stops on a passed deadline stops here too."""


class RouteRoleNotServed(RuntimeError):
    """AA-642 A5 (review S263): ``model_key`` was not called because the only deployments left for it are on connections
    whose ``served_roles`` do not include the stage's ``role`` (``role_skips``: the connection keys). Raised by the
    gateway without a provider call (ledger: status refused, error_class ``role_not_served``, 0 USD). A route whose
    every tried model ended this way raises LLMAccessDenied with that class (``account_level_class``): a retry cannot
    help until an admin changes the route or the connection, so the job runner fails the job at once and the job error
    names the cause."""

    llm_error_class = ROLE_NOT_SERVED

    def __init__(self, model_key: str, role: Optional[str], role_skips):
        role_skips = tuple(role_skips)
        super().__init__(model_key, role, role_skips)    # every arg, so pickle / copy rebuild it (B042)
        self.model_key = model_key
        self.role = role
        self.connection_keys = tuple(dict.fromkeys(k for k, _ in role_skips))

    def __str__(self) -> str:
        conns = ", ".join(self.connection_keys) or "none"
        return (f"Model {self.model_key!r} not called: no usable deployment serves the role {self.role!r} "
                f"(connection {conns} does not serve it; see llm_provider_connection.served_roles)")


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
