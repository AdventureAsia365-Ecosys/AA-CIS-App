"""
shared/llm_client/bedrock_satellite.py — Bedrock satellite client (acc3).

The api/worker task role on acc2 assumes `AA3-Bedrock-Invoker` on acc3 (ExternalId
`aa296-satellite-bedrock-acc3`) and invokes Claude through a `global.` inference profile.
acc3 is the ONLY satellite account: the former cross-account fallback account left the
ecosystem in AA-998 (S255). When acc3 fails there is no cross-account retry; the caller
falls back to another model on the same account (client.py legacy chain, route
fallback_model_ids) or the job retries.

═══════════════════════════════════════════════════════════════════════════
IAM NOTE (found 16/07/2026, AA-296) — READ BEFORE CHANGING MODELS HERE:

When bedrock:InvokeModel runs on an ASSUMED-ROLE session (this module's STS
AssumeRole), Bedrock IAM evaluation also uses the foundation-model ARN WITHOUT
a region:
    arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6
...besides the regional form CloudTrail/Console show:
    arn:aws:bedrock:us-west-1::foundation-model/anthropic.claude-sonnet-4-6

⇒ The invoker role's permission policy must list BOTH forms for every model.
  When adding a model, add both ARNs, not only the regional one.

⚠️ Do not trust the free-text errorMessage of an AccessDeniedException through
  an assumed role; read resources[].ARN in CloudTrail instead.
═══════════════════════════════════════════════════════════════════════════

- acc3's role has InvokeModel + InvokeModelWithResponseStream; Bedrock Batch
  (CreateModelInvocationJob/PassRole) is granted separately (bedrock_batch.py).
- Response schema Anthropic-on-Bedrock: content[0].text (not OpenAI choices[]).
- invoke_claude() logs AccessDeniedException separately (satellite_access_denied)
  so a missing Marketplace subscription is told apart from IAM/network errors.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger()

# ---------------------------------------------------------------- config
# AA-902 — the satellite account ID comes from the task environment (AA-CIS-Infra sets it on the api and
# worker task definitions; this repo is public and carries no account ID). Same name as TripPlanner:
# BEDROCK_ACCT_PRIMARY = acc3. Every other module builds its ARNs from it (cost_explorer.py,
# bedrock_batch.py import it). Unset → "" and the satellite path fails loudly at call time
# (AccessDenied on a malformed ARN) instead of silently using a stale account.
# AA-998: acc3 is the only satellite account (the fallback account was removed).
ACC3_ACCOUNT_ID = os.environ.get("BEDROCK_ACCT_PRIMARY", "")

ACC3_ROLE_ARN = f"arn:aws:iam::{ACC3_ACCOUNT_ID}:role/AA3-Bedrock-Invoker"
ACC3_EXTERNAL_ID = "aa296-satellite-bedrock-acc3"
ACC3_REGION = "us-west-1"

# The only satellite account. The session cache and invoke_claude() look accounts up here, so an
# unknown account (e.g. the retired fallback account) raises ValueError instead of reaching AWS.
DEFAULT_SATELLITE_ACCOUNT = "acc3"
_SATELLITE_ACCOUNTS = {
    "acc3": {"role_arn": ACC3_ROLE_ARN, "external_id": ACC3_EXTERNAL_ID, "region": ACC3_REGION},
}

# AA-348 — the satellite invokes Sonnet 4-6 (`claude-sonnet-4-6`), unlike
# shared/llm_client/pricing.py's BEDROCK_SONNET constant (4-5-20250929), which names the
# acc2-native branch (T1) only. Deliberate, verified 16/07/2026 — do not "align" the version
# here from the pricing constant name: changing the invoked model is an IAM/availability change.
_PROFILE_ID_SONNET = "global.anthropic.claude-sonnet-4-6"
_PROFILE_ID_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _inference_profile_arn(account_id: str, region: str, profile_id: str) -> str:
    return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{profile_id}"


INFERENCE_PROFILE_SONNET = _inference_profile_arn(ACC3_ACCOUNT_ID, ACC3_REGION, _PROFILE_ID_SONNET)
INFERENCE_PROFILE_HAIKU = _inference_profile_arn(ACC3_ACCOUNT_ID, ACC3_REGION, _PROFILE_ID_HAIKU)

_INFERENCE_PROFILES = {
    "acc3": {"sonnet": INFERENCE_PROFILE_SONNET, "haiku": INFERENCE_PROFILE_HAIKU},
}

ANTHROPIC_VERSION = "bedrock-2023-05-31"

# session cache — avoid AssumeRole on every request (role MaxSessionDuration=3600s).
# Keyed by account (AA-397), each account keeps its own session/expiry.
_cached_sessions: dict[str, boto3.Session] = {}
_cached_session_expiry: dict[str, float] = {}
_SESSION_REFRESH_MARGIN_SECONDS = 300  # refresh 5 phút trước khi hết hạn thật


class BedrockUnavailable(Exception):
    """The satellite call failed (AssumeRole, InvokeModel, network...). LLMClient, the only caller,
    re-raises it as that model's failure; the stage route's fallback chain (shared.llm_role_config)
    decides what is tried next. No fixed fallback model is assumed here."""
    pass


@dataclass
class BedrockInvokeResult:
    text: str
    model_used: str          # "sonnet-4-6" | "haiku-4-5"
    latency_ms: float
    usage: dict
    # AA-493: Anthropic's stop_reason ("end_turn" | "max_tokens" | "stop_sequence" | ...) from
    # the non-streaming invoke_model() response payload — a top-level field on that JSON, unlike
    # client.py's streaming _call_bedrock (there it arrives on the message_delta event).
    stop_reason: Optional[str] = None


def _get_satellite_session(account: str = DEFAULT_SATELLITE_ACCOUNT) -> boto3.Session:
    """STS AssumeRole vào satellite account chỉ định, cache theo TTL (AA-397:
    cache riêng từng account, không dùng chung 1 session global nữa)."""
    if account not in _SATELLITE_ACCOUNTS:
        raise ValueError(f"Unknown satellite account: {account!r} (valid: {list(_SATELLITE_ACCOUNTS)})")
    cfg = _SATELLITE_ACCOUNTS[account]

    now = time.time()
    cached = _cached_sessions.get(account)
    if cached is not None and now < _cached_session_expiry.get(account, 0.0):
        return cached

    sts = boto3.client("sts")  # dùng identity ECS task role hiện tại (acc2)
    try:
        resp = sts.assume_role(
            RoleArn=cfg["role_arn"],
            RoleSessionName=f"aa-cis-ecs-{account}-{int(now)}",
            ExternalId=cfg["external_id"],
            DurationSeconds=3600,
        )
    except ClientError as e:
        raise BedrockUnavailable(f"AssumeRole to satellite ({account}) failed: {e}") from e

    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=cfg["region"],
    )
    _cached_sessions[account] = session
    _cached_session_expiry[account] = creds["Expiration"].timestamp() - _SESSION_REFRESH_MARGIN_SECONDS
    return session


def get_satellite_client(service_name: str = "bedrock-runtime", account: str = DEFAULT_SATELLITE_ACCOUNT):
    """Client boto3 bất kỳ (satellite, account chỉ định qua tham số `account`)
    qua session AssumeRole dùng chung (cache TTL) — service_name mặc định
    "bedrock-runtime" (InvokeModel, như invoke_claude() đang dùng) hoặc
    "bedrock" (control-plane, vd CreateModelInvocationJob cho Batch API,
    AA-302). `account` defaults to acc3, the only satellite account (AA-998)."""
    session = _get_satellite_session(account)
    region = _SATELLITE_ACCOUNTS[account]["region"]
    return session.client(service_name, region_name=region)


def _invoke_streaming(bedrock_rt, inference_profile: str, body: str, model_label: str, t0: float,
                      on_delta: Callable[[str], None]) -> BedrockInvokeResult:
    """AA-637 — invoke_model_with_response_stream, accumulating the same fields the non-streaming
    payload carries (text, usage incl. cache tokens, stop_reason) and forwarding each text delta."""
    resp = bedrock_rt.invoke_model_with_response_stream(
        modelId=inference_profile, body=body,
        contentType="application/json", accept="application/json",
    )
    parts: list[str] = []
    usage: dict = {}
    stop_reason = None
    for event in resp["body"]:
        chunk = json.loads(event["chunk"]["bytes"])
        ctype = chunk.get("type")
        if ctype == "message_start":
            usage.update(chunk.get("message", {}).get("usage", {}) or {})
        elif ctype == "content_block_delta":
            d = chunk.get("delta", {})
            if d.get("type") == "text_delta":
                t = d.get("text", "")
                parts.append(t)
                on_delta(t)
        elif ctype == "message_delta":
            out = chunk.get("usage", {}).get("output_tokens")
            if out is not None:
                usage["output_tokens"] = out
            stop_reason = chunk.get("delta", {}).get("stop_reason", stop_reason)
    return BedrockInvokeResult(
        text="".join(parts), model_used=model_label,
        latency_ms=round((time.time() - t0) * 1000, 1), usage=usage, stop_reason=stop_reason,
    )


def invoke_claude(
    prompt: str,
    model: str = "sonnet",
    max_tokens: int = 4096,
    system: Optional[str] = None,
    account: str = DEFAULT_SATELLITE_ACCOUNT,
    on_delta: Optional[Callable[[str], None]] = None,
    temperature: Optional[float] = None,
) -> BedrockInvokeResult:
    """
    model: "sonnet" (editorial, S1 rewrite) | "haiku" (schema/fast tasks)
    account: "acc3" — the only satellite account (AA-998); anything else raises ValueError.
    system: system prompt riêng (brand rules, JSON-schema instructions...).
      QUAN TRỌNG — AA-296 review (16/07/2026): field này BẮT BUỘC phải truyền
      khi gọi từ pipeline S1 rewrite thật. _call_bedrock (acc2, T1) gửi
      system tách biệt qua build_cached_system_prompt — nếu invoke_claude()
      không nhận và forward đúng field system của Anthropic Messages API,
      brand rules sẽ ÂM THẦM BỊ MẤT khi nhánh satellite (T1.5) kích hoạt,
      KHÔNG có lỗi nào báo — chỉ là output tệ đi không rõ nguyên nhân.
      Không nối system vào đầu prompt (user message) — Anthropic xử lý
      system khác ưu tiên/ngữ nghĩa so với user turn, nối vào sẽ giảm độ
      tuân thủ brand rules so với cách acc2/T1 đang làm.
    Raise BedrockUnavailable khi lỗi — LLMClient (the only caller) re-raises it as that model's
    failure; the stage route's fallback chain decides what is tried next.
    """
    if account not in _SATELLITE_ACCOUNTS:
        raise ValueError(f"Unknown satellite account: {account!r} (valid: {list(_SATELLITE_ACCOUNTS)})")
    inference_profile = _INFERENCE_PROFILES[account][model]
    model_label = "sonnet-4-6" if model == "sonnet" else "haiku-4-5"
    region = _SATELLITE_ACCOUNTS[account]["region"]

    t0 = time.time()
    try:
        session = _get_satellite_session(account)
        bedrock_rt = session.client("bedrock-runtime", region_name=region)

        body_dict = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            # S224: before this, a temperature set on an LLMRequest never reached Claude on the
            # satellite path (the S1 writer always ran at the Bedrock default 1.0).
            body_dict["temperature"] = temperature
        if system:
            # AA-324: was a plain string -- Anthropic's Bedrock InvokeModel body (this
            # exact "anthropic_version": "bedrock-2023-05-31" shape, NOT the Converse API)
            # supports the identical prompt-caching contract as the direct Anthropic API:
            # `system` as a list of content blocks, each optionally carrying
            # `cache_control: {"type": "ephemeral"}` -- confirmed via AWS's own Bedrock
            # prompt-caching docs, this is NOT a Bedrock/cross-account platform limitation
            # (the issue's own original title hypothesis). A plain string `system` is valid
            # Anthropic Messages API shape too, but Bedrock silently never caches it -- no
            # error, exactly the "code looks right, cache_read/write stay 0" symptom AA-324
            # found. Reuses build_cached_system_prompt() -- the SAME L1-cache helper
            # _call_bedrock() (acc2-native T1 path) already uses -- so satellite (T1.5/T2.5,
            # the path essentially all real Sonnet/Haiku calls fall through to today, acc2
            # native being blocked for channel-program accounts, AA-291/AA-329) gets the
            # identical cache contract instead of silently never caching.
            from .prompt_cache import build_cached_system_prompt
            body_dict["system"] = build_cached_system_prompt(system)
        body = json.dumps(body_dict)
        if on_delta is not None:
            # AA-637 — live-progress path: same request, streamed. The acc3 role allows
            # InvokeModelWithResponseStream.
            return _invoke_streaming(bedrock_rt, inference_profile, body, model_label, t0, on_delta)
        resp = bedrock_rt.invoke_model(
            modelId=inference_profile,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        latency_ms = (time.time() - t0) * 1000
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"]
        usage = payload.get("usage", {})

        return BedrockInvokeResult(
            text=text,
            model_used=model_label,
            latency_ms=round(latency_ms, 1),
            usage=usage,
            stop_reason=payload.get("stop_reason"),
        )
    except BedrockUnavailable:
        raise  # đã đúng loại exception, propagate thẳng
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        # AA-397: chỉ thêm log phân loại, KHÔNG đổi luồng try/except gốc — vẫn
        # luôn raise BedrockUnavailable ở dưới bất kể loại lỗi nào. Mục đích là
        # phân biệt "model access chưa enable trên account này" (thường tự hết
        # sau khi Marketplace subscription active — xem AA-397) khỏi lỗi khác
        # (network, IAM sai, model id sai...) khi đọc CloudWatch sau này.
        if "AccessDeniedException" in type(e).__name__:
            logger.warning("satellite_access_denied", account=account, model=model_label,
                           latency_ms=round(latency_ms, 1), error=str(e))
        else:
            logger.warning("satellite_invoke_failed", account=account, model=model_label,
                           latency_ms=round(latency_ms, 1), error_type=type(e).__name__, error=str(e))
        # TODO: emit CloudWatch metric t3_fallback_used=1 ở đây trước khi raise
        raise BedrockUnavailable(
            f"Satellite Bedrock invoke failed ({model_label}, account={account}): {type(e).__name__}: {e}"
        ) from e
