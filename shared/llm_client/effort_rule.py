"""AA-1053 (W1 contradiction #14) — one effort rule per stage, mapped explicitly for each provider kind.

The rule: a stage's reasoning effort on a catalog model is the ``effort`` of its ``shared.stage_model_params`` row
(``stage_params.effort_for``; migration 381 sets t9_write medium and five validate stages low). Nothing else sets a
stage's effort. This module is the only place that turns that value into what a provider path sends, so the same
(stage, model) asks the same effort on every way in that can carry it, and a way that cannot says so:

==========================  =================================================================================
provider kind (api style)   what the stage's effort becomes
==========================  =================================================================================
Anthropic Messages API      ``output_config.effort`` (every effort; the deployment's ``effort_by_stage`` is
                            overridden for the stage, ``adapters.anthropic_messages.resolve_effort`` sends it, or
                            nothing on a model that takes no effort parameter)
OpenAI / Azure Chat         ``reasoning_effort`` through ``OPENAI_EFFORTS`` (low / medium / high as is; xhigh and
                            max as high, the highest value every GPT-5.x / 6.x model takes), on a reasoning model
                            only (``shared/aa_llm/tokens.reasoning_capable``)
Bedrock Converse            ``additionalModelRequestFields`` in the form the catalog row's
                            ``capabilities.converse_effort`` names: ``output_config`` (Claude:
                            ``{"output_config": {"effort": e}}``) or ``reasoning_effort`` (GPT:
                            ``{"reasoning_effort": OPENAI_EFFORTS[e]}``). No row states it today (AA-803: a Converse
                            field is set only after a probe has shown the provider takes it), so nothing is sent and
                            the request stays byte-identical; ``stage_params_not_applied`` says so once.
==========================  =================================================================================

``EffortPlan.sent`` is the value that went on the wire (None = none, the model's default). The paths put it on
``LLMResponse.effort`` (Anthropic: the adapter's own resolved value, which includes the model family default), so the
ledger's ``effort`` column and the route trace rows of the request (``call_log.billed_fields``) show the effort each
attempt actually ran at. Never raises: a read problem means "nothing sent", today's behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from . import stage_params

logger = structlog.get_logger()

PATH_ANTHROPIC = "anthropic_messages"
PATH_OPENAI = "openai_chat"
PATH_CONVERSE = "converse"
PATHS = (PATH_ANTHROPIC, PATH_OPENAI, PATH_CONVERSE)

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# The Chat Completions ``reasoning_effort`` each stage effort maps to. xhigh / max are not taken by every GPT model (a
# 400 would cost the attempt), so they ask the highest value every reasoning model takes.
OPENAI_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

# The Converse request key the effort goes under (client._call_bedrock_converse merges EffortPlan.wire into it).
CONVERSE_FIELD = "additionalModelRequestFields"
# The catalog capability naming the Converse form of the effort field, and the forms the gateway knows.
CAP_CONVERSE_EFFORT = "converse_effort"
CONVERSE_OUTPUT_CONFIG = "output_config"
CONVERSE_REASONING_EFFORT = "reasoning_effort"
CONVERSE_FORMS = (CONVERSE_OUTPUT_CONFIG, CONVERSE_REASONING_EFFORT)

# Why a stage's effort was not sent (EffortPlan.reason).
NOT_SENT_NOT_REASONING = "model does not reason"
NOT_SENT_CONVERSE_UNPROBED = "converse field not stated (capabilities.converse_effort)"


@dataclass(frozen=True)
class EffortPlan:
    """What a provider path does with a stage's effort on one model."""
    path: str
    stage_effort: Optional[str] = None      # the rule's value (stage x model row), None = no rule
    sent: Optional[str] = None              # the value put on the wire, None = nothing sent
    reason: Optional[str] = None            # why stage_effort was not sent (None when sent or no rule)
    wire: dict = field(default_factory=dict)   # the fields the path merges into its request


def stage_effort(stage: Optional[str], model_key: str) -> Optional[str]:
    """The stage's effort on ``model_key`` (the stage x model row), or None. The one source of a stage's effort."""
    try:
        effort = stage_params.effort_for(stage, model_key)
    except Exception as e:  # noqa: BLE001 — a parameter read never breaks the call
        logger.warning("llm_effort_rule_read_failed", stage=stage, model=model_key, error=str(e)[:200])
        return None
    return effort if effort in EFFORTS else None


def _caps(model: Any) -> dict:
    caps = getattr(model, "capabilities", None)
    return caps if isinstance(caps, dict) else {}


def anthropic_request_defaults(stage: Optional[str], model: Any, request_defaults: Optional[dict]) -> Optional[dict]:
    """The deployment's ``request_defaults`` with the stage's effort as ``effort_by_stage[stage]`` (it beats the
    deployment's own value for that stage), or the deployment's own object unchanged when there is no rule (today's
    request). ``adapters.anthropic_messages.resolve_effort`` then sends it (or nothing on a model without effort)."""
    effort = stage_effort(stage, getattr(model, "model_key", "") or "")
    if not effort or not stage:
        return request_defaults
    base = request_defaults if isinstance(request_defaults, dict) else {}
    by_stage = base.get("effort_by_stage")
    return {**base, "effort_by_stage": {**(by_stage if isinstance(by_stage, dict) else {}), stage: effort}}


def plan_openai(stage: Optional[str], model: Any) -> EffortPlan:
    """The ``reasoning_effort`` of (stage, catalog model) on the OpenAI / Azure Chat Completions path."""
    effort = stage_effort(stage, getattr(model, "model_key", "") or "")
    if effort is None:
        return EffortPlan(PATH_OPENAI)
    try:
        from shared.aa_llm import tokens

        key = getattr(model, "model_key", "") or ""
        reasons = tokens.reasoning_capable(key, _caps(model) or None)
    except Exception as e:  # noqa: BLE001 — a parameter read never breaks the call
        logger.warning("llm_effort_rule_reasoning_check_failed", stage=stage, error=str(e)[:200])
        reasons = False
    if not reasons:
        return EffortPlan(PATH_OPENAI, stage_effort=effort, reason=NOT_SENT_NOT_REASONING)
    sent = OPENAI_EFFORTS[effort]
    return EffortPlan(PATH_OPENAI, stage_effort=effort, sent=sent, wire={"reasoning_effort": sent})


def plan_converse(stage: Optional[str], model: Any) -> EffortPlan:
    """The ``additionalModelRequestFields`` of (stage, catalog model) on Bedrock Converse: the form the catalog row
    states in ``capabilities.converse_effort``, else nothing (today: no row states one)."""
    effort = stage_effort(stage, getattr(model, "model_key", "") or "")
    if effort is None:
        return EffortPlan(PATH_CONVERSE)
    form = _caps(model).get(CAP_CONVERSE_EFFORT)
    if form == CONVERSE_OUTPUT_CONFIG:
        return EffortPlan(PATH_CONVERSE, stage_effort=effort, sent=effort,
                          wire={"output_config": {"effort": effort}})
    if form == CONVERSE_REASONING_EFFORT:
        sent = OPENAI_EFFORTS[effort]
        return EffortPlan(PATH_CONVERSE, stage_effort=effort, sent=sent, wire={"reasoning_effort": sent})
    if form is not None:
        logger.warning("llm_effort_rule_converse_form_invalid", model=getattr(model, "model_key", None), form=form,
                       known=list(CONVERSE_FORMS))
    return EffortPlan(PATH_CONVERSE, stage_effort=effort, reason=NOT_SENT_CONVERSE_UNPROBED)


__all__ = ["CAP_CONVERSE_EFFORT", "CONVERSE_FIELD", "CONVERSE_FORMS", "CONVERSE_OUTPUT_CONFIG",
           "CONVERSE_REASONING_EFFORT", "EFFORTS", "EffortPlan", "NOT_SENT_CONVERSE_UNPROBED", "NOT_SENT_NOT_REASONING",
           "OPENAI_EFFORTS", "PATHS", "PATH_ANTHROPIC", "PATH_CONVERSE", "PATH_OPENAI", "anthropic_request_defaults",
           "plan_converse", "plan_openai", "stage_effort"]
