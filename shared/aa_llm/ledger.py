"""One row of the shared model-call ledger, ledger v2 (root ADR 0009 decision 8, CIS migration 319).

``shared.llm_call_log`` holds one row per attempt (CIS and TripPlanner): a billed reply, a refused or failed
attempt (cost 0) and its outcome. ``LedgerRow`` is that row with every column of the v2 INSERT of AA-CIS-App
``shared/llm_client/call_log.py``; ``insert_sql()`` is the statement and ``LedgerRow.args()`` its arguments in the
same order. The v2 values are normalised the way the CIS writer does (``_v2_values``): status derived when the
caller is silent, values outside the migration 319 CHECKs written NULL, so a row is never refused by a CHECK.
``tests/unit/test_aa642_s8_aa_llm_parity.py`` in AA-CIS-App pins the column list and the normalisation to the CIS
writer.

The CRM (AA-Partner) does not write this table (root ADR 0008a): it vendors this module for the constants and the
row shape only and keeps ``crm.llm_call``.

Pure: no database driver here. The caller's ledger sink (``ports.LedgerSink``) executes ``insert_sql()`` with
``row.args()`` and decides what a failed write means.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from .errors import (
    ACCESS_DENIED,
    CREDENTIAL,
    LEDGER_ERROR_CLASSES,
    OTHER,
    QUOTA_EXHAUSTED,
    THROTTLED,
    TRUNCATED,
    classify,
)
from .pricing import PRICING_MODE_RE

TABLE = "shared.llm_call_log"

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"
STATUS_BILLED_UNUSABLE = "billed_unusable"
STATUS_TRUNCATED = "truncated"
STATUSES = (STATUS_OK, STATUS_REFUSED, STATUS_ERROR, STATUS_BILLED_UNUSABLE, STATUS_TRUNCATED)
ERROR_CLASSES = LEDGER_ERROR_CLASSES
# A billed answer cut at its token limit (Anthropic / Bedrock max_tokens, OpenAI length): status 'truncated'.
TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "length", "model_context_window_exceeded"})
# llm_call_log.role (migration 319 CHECK).
ROLES = ("writer", "judge", "validate", "embed", "decide", "data", "guard")

# The columns of the CIS v1 row, then the S6 three (migration 312), then the v2 seventeen (migration 319), in the
# order of the CIS INSERT.
BASE_COLUMNS = (
    "tenant_id",
    "stage",
    "role",
    "model",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "quality_signal",
    "content_piece_id",
    "angle_gate_request_id",
    "stop_reason",
    "account",
    "fallback_used",
    "provider",
    "job_id",
)
S6_COLUMNS = ("cache_read_tokens", "cache_write_tokens", "pricing_mode")
V2_COLUMNS = (
    "app",
    "status",
    "error_class",
    "latency_ms",
    "connection_key",
    "deployment_wire_id",
    "attempt_no",
    "request_id",
    "tour_id",
    "reasoning_tokens",
    "effort",
    "is_shadow",
    "is_experiment",
    "prompt_hash",
    "prompt_version",
    "units",
    "unit_kind",
)
COLUMNS = BASE_COLUMNS + S6_COLUMNS + V2_COLUMNS
_CASTS = {
    "tenant_id": "::uuid",
    "content_piece_id": "::uuid",
    "angle_gate_request_id": "::uuid",
    "job_id": "::uuid",
    "tour_id": "::uuid",
    "quality_signal": "::jsonb",
}
_PRICING_MODE = re.compile(PRICING_MODE_RE)


def insert_sql(table: str = TABLE) -> str:
    """The INSERT of one ledger row: every column of ``COLUMNS``, one placeholder each (uuid / jsonb cast)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", table):
        raise ValueError(f"not a schema-qualified table name: {table!r}")
    cols = ", ".join(COLUMNS)
    vals = ", ".join(f"${i}{_CASTS.get(c, '')}" for i, c in enumerate(COLUMNS, start=1))
    return f"INSERT INTO {table} ({cols}) VALUES ({vals})"  # noqa: S608 — fixed identifiers, values are parameters


def _text(value, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _uuid_or_none(value) -> str | None:
    if value is None or str(value) in ("", "None"):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def derive_status(
    status: str | None = None, *, stop_reason: str | None = None, quality_signal: dict | None = None
) -> str:
    """The row's status: the caller's when it is one of STATUSES, else billed_unusable for a billed-unusable signal,
    truncated for a reply cut at its token limit, else ok."""
    if status in STATUSES:
        return status
    qs = quality_signal if isinstance(quality_signal, dict) else {}
    if qs.get("billed_unusable") is True:
        return STATUS_BILLED_UNUSABLE
    if isinstance(stop_reason, str) and stop_reason in TRUNCATED_STOP_REASONS:
        return STATUS_TRUNCATED
    return STATUS_OK


# The classes of an attempt the provider (or the gateway) said no to: status 'refused'. Anything else that raised with
# nothing billed (outage, timeout, a bad reply) is 'error' with class 'other' (the CIS client's attempt_outcome).
REFUSED_CLASSES = frozenset({ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED, CREDENTIAL})


def failed_outcome(error: BaseException | str | None) -> tuple[str, str]:
    """(status, error_class) of an attempt that raised with nothing billed: ('refused', its class) for an access,
    credit, credential or rate-limit refusal, else ('error', 'other')."""
    cls = classify(error)
    if cls not in REFUSED_CLASSES:
        return STATUS_ERROR, OTHER
    return STATUS_REFUSED, cls


@dataclass
class LedgerRow:
    """One attempt. ``app`` is the writing app (``cis``, ``tripplanner``); ``status`` / ``error_class`` are derived
    when left None. A failed or refused attempt carries cost 0 and its ``error_class``."""

    app: str
    stage: str
    role: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    quality_signal: dict = field(default_factory=dict)
    tenant_id: str | None = None
    content_piece_id: str | None = None
    angle_gate_request_id: str | None = None
    stop_reason: str | None = None
    account: str | None = None
    fallback_used: bool | None = None
    provider: str | None = None
    job_id: str | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    pricing_mode: str | None = None
    status: str | None = None
    error_class: str | None = None
    latency_ms: int | None = None
    connection_key: str | None = None
    deployment_wire_id: str | None = None
    attempt_no: int | None = None
    request_id: str | None = None
    tour_id: str | None = None
    reasoning_tokens: int | None = None
    effort: str | None = None
    is_shadow: bool | None = None
    is_experiment: bool | None = None
    prompt_hash: str | None = None
    prompt_version: str | None = None
    units: int | None = None
    unit_kind: str | None = None

    def v2_values(self) -> tuple:
        """The 17 values of V2_COLUMNS, normalised like the CIS writer: a value of the wrong type or outside the
        migration 319 CHECKs is written NULL; ``is_shadow`` / ``is_experiment`` default from the signal."""
        qs = self.quality_signal if isinstance(self.quality_signal, dict) else {}
        status = derive_status(self.status, stop_reason=self.stop_reason, quality_signal=qs)
        error_class = self.error_class if self.error_class in ERROR_CLASSES else None
        if error_class is None and status == STATUS_TRUNCATED:
            error_class = TRUNCATED
        latency = _int_or_none(self.latency_ms)
        attempt = _int_or_none(self.attempt_no)
        shadow, experiment = self.is_shadow, self.is_experiment
        return (
            _text(self.app, 40),
            status,
            error_class,
            latency if latency is not None and latency >= 0 else None,
            _text(self.connection_key, 100),
            _text(self.deployment_wire_id, 300),
            attempt if attempt is not None and attempt >= 1 else None,
            _text(self.request_id, 200),
            _uuid_or_none(self.tour_id),
            _int_or_none(self.reasoning_tokens),
            _text(self.effort, 40),
            shadow if isinstance(shadow, bool) else qs.get("shadow") is True,
            experiment if isinstance(experiment, bool) else "eval_run" in qs,
            _text(self.prompt_hash, 128),
            _text(self.prompt_version, 100),
            _int_or_none(self.units),
            _text(self.unit_kind, 40),
        )

    def s6_values(self) -> tuple:
        """cache_read_tokens, cache_write_tokens, pricing_mode: non-negative ints and a mode the CHECK accepts."""
        cr, cw = (
            v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
            for v in (self.cache_read_tokens, self.cache_write_tokens)
        )
        mode = self.pricing_mode if isinstance(self.pricing_mode, str) else None
        if mode is not None and not _PRICING_MODE.match(mode):
            mode = None
        return (cr, cw, mode)

    def base_values(self) -> tuple:
        return (
            _uuid_or_none(self.tenant_id),
            self.stage,
            self.role,
            self.model,
            self.tokens_in,
            self.tokens_out,
            self.cost_usd,
            json.dumps(self.quality_signal if isinstance(self.quality_signal, dict) else {}, default=str),
            _uuid_or_none(self.content_piece_id),
            _uuid_or_none(self.angle_gate_request_id),
            self.stop_reason,
            self.account,
            self.fallback_used,
            self.provider,
            _uuid_or_none(self.job_id),
        )

    def args(self) -> tuple:
        """The arguments of ``insert_sql()``, in COLUMNS order."""
        return self.base_values() + self.s6_values() + self.v2_values()
