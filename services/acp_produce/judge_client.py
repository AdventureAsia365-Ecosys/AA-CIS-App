"""
services.acp_produce.judge_client — F8/F9 cross-weight judge. Production default is
GPT-4.1 direct OpenAI as of AA-518 (02/09/2026) — see that section at the bottom of this
docstring before assuming Nova Pro is still what runs live.

ADR-2026-014/ADR-2026-027 (L3): the judge must run on a different model/vendor
than the writer AND must never see the writer's generation prompt — only
(piece text + rubric + corpus). This is a separate file from
services/acp_produce/generation.py on purpose: this module never imports
from generation.py (or from services/content_generation/s1_from_atom.py, the
writer used elsewhere in this repo), and nothing here accepts a
system_prompt/user_prompt built for a writer call. A reviewer — or a future
CI import-graph check — can verify context isolation by reading imports, not
just by trusting a docstring.

Model verified live (24/07/2026, real Bedrock invoke_model call, acc2
005097885195, confirmed via `aws bedrock list-inference-profiles`):
us.amazon.nova-pro-v1:0. Request/response shape is Bedrock's Converse-style
body — a THIRD distinct shape in this repo, neither Anthropic's
content[0].text (Claude satellite, shared/llm_client/bedrock_satellite.py)
nor the OpenAI-compatible choices[0].message.content shape (Palmyra,
services/content_generation/s1_from_atom.py::_call_palmyra):
  request:  {"system": [{"text": ...}], "messages": [{"role": "user",
             "content": [{"text": ...}]}], "inferenceConfig": {...}}
  response: {"output": {"message": {"content": [{"text": ...}]}}, "usage": {...}}

AA-351 (16/08/2026) — added invoke_judge_gpt56() as a SECOND, feature-flagged judge
backend (GPT-5.6 Sol, via the acc3 Bedrock satellite) alongside Nova Pro, to test
whether a judge from a third vendor family (neither Claude/writer nor Nova) reduces
the "moving target" flagging AA-382 confirmed with real data (docs/implementation-
notes/AA-382-repair-rubric-context.md — same phrase re-flagged as violating across
repair rounds after being fixed, one case re-reading as compliant with the judge's
own GOOD anchor). Selection is env var JUDGE_MODEL ("nova_pro" default | "gpt56"),
read per-call (not at import) so it can be overridden per-invocation for a side-by-
side comparison script without mutating process-wide state. Nova Pro's code path is
UNCHANGED — this is additive, not a replacement; every existing caller (gates.py's 3
call sites) is untouched and still defaults to Nova Pro in production.

AA-351-03 (16/08/2026) — added invoke_judge_gpt41() as a THIRD feature-flagged judge
backend (GPT-4.1, direct OpenAI API, no Bedrock/AWS at all), while GPT-5.6 Sol
(AA-351-02) stayed blocked on an AWS-side access denial ~50+ min after the agreement
was accepted (see docs/implementation-notes/AA-351-gpt56-judge-trial.md) — GPT-4.1 was
already live infrastructure (services/content_generation/judge_node.py, ADR-2026-031
T3) so this backend needed no new AWS setup and could run immediately. JUDGE_MODEL
accepted "nova_pro" (default at the time) | "gpt56" | "gpt41".

AA-518 (02/09/2026) — a real STEP0 audit re-confirmed GPT-5.6 Sol is STILL AccessDenied
on all 3 accounts, unchanged since AA-351 (see docs/implementation-notes/AA-518.md +
memory project_aa519_llm_api_audit_02sep). Rather than keep N7's F8/F9 judge on a
different backend than S1's judge (Nova Pro here, GPT-4.1 there) for no reason other
than historical accident, invoke_judge()'s DEFAULT changed "nova_pro" -> "gpt41" —
now both pipelines' judges run the same real backend. ECS task def sets JUDGE_MODEL=
gpt41 explicitly (AA-CIS-Infra) as the actual production pin; the code default is
belt-and-suspenders for anywhere that env var isn't set (local dev, tests that don't
override it). Nova Pro's code path (invoke_judge() proper) is completely untouched —
model="nova_pro" or JUDGE_MODEL=nova_pro still routes there exactly as before, it is
simply no longer the default. THIS IS A DELIBERATE TEMPORARY CHOICE, not a final
architecture decision — re-evaluate once GPT-5.6's access gate actually clears
(AA-351). gates.py's 3 call sites needed no changes; they never passed model=
explicitly and still don't.
"""
from __future__ import annotations

import json
import os

import boto3
import structlog
from json_repair import repair_json

logger = structlog.get_logger()

AWS_REGION = "us-west-1"
NOVA_PRO_MODEL_ID = "us.amazon.nova-pro-v1:0"

# AA-351 — GPT-5.6 Sol, accepted (create-foundation-model-agreement) on acc3 ONLY
# (786888028788, profile nghiep_aa365) 16/08/2026 — NOT accepted on acc1/acc2, verified
# via list-foundation-model-agreement-offers before/after on all 3. Only Sol was
# accepted (Terra/Luna are separate agreements/rate cards, NOT the same offer as
# survey AA-351-gpt-bedrock-3accounts.md originally assumed — confirmed distinct
# offerIds/pricing during this session) — Nghiep's explicit choice, trial scope only.
# "global." prefix (not "us.") matches the existing Claude satellite convention
# (bedrock_satellite.py's INFERENCE_PROFILE_SONNET/HAIKU — AA-397) and is ~10%
# cheaper per the survey's rate card. GPT models on Bedrock only support Converse,
# never invoke_model with a raw JSON body (confirmed by the AA-351 survey's real
# invoke tests) — invoke_judge_gpt56() below uses client.converse(), not
# invoke_model(), unlike the Nova Pro path in invoke_judge() proper.
GPT56_SOL_INFERENCE_PROFILE = (
    "arn:aws:bedrock:us-west-1:786888028788:inference-profile/global.openai.gpt-5.6-sol"
)

# AA-351-03 (16/08/2026) — GPT-4.1, direct OpenAI API, NOT Bedrock/AWS at all. Reuses the
# exact client pattern services/content_generation/judge_node.py already runs in production
# for S1's brand-fit judge (openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"]), ADR-2026-031
# T3) — same env var, same SDK call shape. Deliberately NOT routed through
# shared/llm_client/client.py's LLMClient.generate() (that class's T1-T3 chain tries Bedrock
# Sonnet/Haiku FIRST and only reaches GPT-4.1 as a last resort) — this backend is a direct,
# single-shot OpenAI call every time it's selected, no Bedrock fallback attempted first, the
# same "no fallback, just this one backend" shape invoke_judge_gpt56() above already uses for
# its satellite call. Real OpenAI pricing verified 16/08/2026 (not guessed): $2.00/1M input,
# $8.00/1M output — matches this repo's own shared/llm_client/client.py::COST_TABLE["gpt-4.1"]
# exactly (0.002/0.008 per 1K tokens).
GPT41_MODEL = "gpt-4.1"


def _invoke_judge_via_route(system_prompt: str, user_prompt: str, max_tokens: int, stage: str) -> dict:
    """AA-659 / ADR 0006 — the judge model comes from the stage route (shared.llm_role_config:
    model + fallbacks + shadow), not from JUDGE_MODEL. temperature=0 is still requested; the
    catalog drops it for models that do not accept it."""
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    resp = LLMClient().generate(LLMRequest(
        system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens,
        temperature=0, stage=stage,
    ))
    return {
        "text": resp.content,
        "model_used": resp.model_used,
        "provider": resp.provider,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "stop_reason": resp.stop_reason,
        "account": resp.satellite_account,
        "fallback_used": resp.fallback_used,
        "cost_usd": resp.cost_usd,
    }


def invoke_judge(
    system_prompt: str, user_prompt: str, max_tokens: int = 2048, model: str | None = None,
    stage: str | None = None,
) -> dict:
    """AA-659: with `stage` (every production call site) and no explicit `model`, the call goes
    through the stage route — see _invoke_judge_via_route(). The historical docstring below
    describes the explicit-`model` / JUDGE_MODEL path, kept for comparison scripts."""
    if model is None and stage is not None:
        return _invoke_judge_via_route(system_prompt, user_prompt, max_tokens, stage)
    return _invoke_judge_legacy(system_prompt, user_prompt, max_tokens, model)


def _invoke_judge_legacy(
    system_prompt: str, user_prompt: str, max_tokens: int = 2048, model: str | None = None,
) -> dict:
    """One seam, mirrors the writer's generate_draft() seam
    (services/content_generation/s1_from_atom.py) — deliberately duplicated
    rather than shared, so no future refactor can accidentally merge the
    writer and judge call paths into one function that some caller then
    reuses for both roles. Returns {text, model_used, provider, input_tokens,
    output_tokens, stop_reason}  (stop_reason added AA-493).

    `model`: explicit override for AA-351's comparison script ("nova_pro" |
    "gpt56" | "gpt41"). None (the default — every gates.py call site) falls
    back to the JUDGE_MODEL env var.

    AA-518 (02/09/2026) — default changed "nova_pro" -> "gpt41". GPT-5.6 Sol
    (the intended long-term judge, AA-351) is still AccessDenied on all 3
    accounts (control-plane agreement accepted, InvokeModel still blocked —
    see docs/implementation-notes/AA-351-gpt56-judge-trial.md and the STEP0
    audit this task followed up on). GPT-4.1 direct OpenAI is a DELIBERATE
    TEMPORARY stand-in, not a final decision — same backend judge_node.py
    (S1's judge) already uses in production, and unblocked again as of this
    task (see [[project_aa351_gpt41_openai_zero_credits]] update). Re-evaluate
    once AA-351's GPT-5.6 access gate actually clears. Nova Pro's code path
    (invoke_judge() proper, below) is untouched -- override model="nova_pro"
    still works for anyone who needs it back.
    ECS task def sets JUDGE_MODEL=gpt41 explicitly (AA-CIS-Infra) so this
    default is belt-and-suspenders, not the only thing pinning production."""
    model = model or os.environ.get("JUDGE_MODEL", "gpt41")
    if model == "gpt56":
        return invoke_judge_gpt56(system_prompt, user_prompt, max_tokens)
    if model == "gpt41":
        return invoke_judge_gpt41(system_prompt, user_prompt, max_tokens)

    client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    body = {
        "system": [{"text": system_prompt}],
        "messages": [{"role": "user", "content": [{"text": user_prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    resp = client.invoke_model(
        modelId=NOVA_PRO_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    text = payload["output"]["message"]["content"][0]["text"]
    usage = payload.get("usage", {})
    # AA-493: Bedrock Converse API (this is invoke_model + a "message"/"output" wrapper shape,
    # NOT the Converse API proper, but Nova models return their own top-level `stopReason`
    # either way — same camelCase key the real Converse API uses) confirmed against this live
    # response shape while building this fix.
    stop_reason = payload.get("stopReason")
    logger.info("judge_llm_success", model=NOVA_PRO_MODEL_ID, provider="bedrock-acc2",
                in_tokens=usage.get("inputTokens", 0), out_tokens=usage.get("outputTokens", 0),
                stop_reason=stop_reason)
    return {
        "text": text,
        "model_used": NOVA_PRO_MODEL_ID,
        "provider": "bedrock-acc2",
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "stop_reason": stop_reason,
    }


def invoke_judge_gpt56(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict:
    """AA-351 trial judge backend — GPT-5.6 Sol via the acc3 Bedrock satellite.
    Reuses shared/llm_client/bedrock_satellite.py's AssumeRole session cache
    (get_satellite_client, account="acc3") — the SAME session mechanism the
    Claude satellite writer already uses, not a new AssumeRole path (one
    fewer thing to get wrong/duplicate). Deliberately NOT imported by
    anything in generation.py/content_generation — this module's own
    isolation guarantee (see module docstring, ADR-2026-014/027 L3) applies
    equally to this second judge backend."""
    from shared.llm_client.bedrock_satellite import get_satellite_client

    client = get_satellite_client("bedrock-runtime", account="acc3")
    resp = client.converse(
        modelId=GPT56_SOL_INFERENCE_PROFILE,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    usage = resp.get("usage", {})
    # AA-493: the real Converse API's top-level `stopReason` field (documented, unlike Nova's
    # invoke_model shape above which happens to match it).
    stop_reason = resp.get("stopReason")
    logger.info("judge_llm_success", model=GPT56_SOL_INFERENCE_PROFILE,
                provider="bedrock-satellite-acc3",
                in_tokens=usage.get("inputTokens", 0), out_tokens=usage.get("outputTokens", 0),
                stop_reason=stop_reason)
    return {
        "text": text,
        "model_used": GPT56_SOL_INFERENCE_PROFILE,
        "provider": "bedrock-satellite-acc3",
        "input_tokens": usage.get("inputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "stop_reason": stop_reason,
    }


def invoke_judge_gpt41(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict:
    """AA-351-03 trial judge backend — GPT-4.1, direct OpenAI API (no Bedrock, no AWS
    account/IAM chain at all). Uses the same `openai.OpenAI(api_key=os.environ
    ["OPENAI_API_KEY"])` client construction services/content_generation/judge_node.py
    already runs in production for S1's brand-fit judge -- not a new client library, not
    a new env var. `temperature=0` matches the deterministic-judge convention the Nova
    Pro and GPT-5.6 backends above both already use (their `inferenceConfig`); OpenAI's
    Chat Completions API takes it as a top-level kwarg instead. Deliberately NOT imported
    by anything in generation.py/content_generation — same isolation guarantee (module
    docstring, ADR-2026-014/027 L3) the other two judge backends already honor."""
    import openai

    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=GPT41_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    text = resp.choices[0].message.content
    usage = resp.usage
    in_tok = usage.prompt_tokens if usage else 0
    out_tok = usage.completion_tokens if usage else 0
    stop_reason = resp.choices[0].finish_reason  # AA-493: OpenAI's field name for the same concept
    logger.info("judge_llm_success", model=GPT41_MODEL, provider="openai",
                in_tokens=in_tok, out_tokens=out_tok, stop_reason=stop_reason)
    return {
        "text": text,
        "model_used": GPT41_MODEL,
        "provider": "openai",
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "stop_reason": stop_reason,
    }


def parse_judge_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        salvaged = repair_json(raw, return_objects=True)
        if isinstance(salvaged, dict):
            return salvaged
        raise
