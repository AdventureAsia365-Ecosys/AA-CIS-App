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
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import boto3
import structlog
from botocore.exceptions import ClientError

from . import connections

logger = structlog.get_logger()

# ---------------------------------------------------------------- config
# AA-1002 (S3): which accounts, roles, ExternalIds and regions exist is DATA — shared.llm_provider_connection (kind
# bedrock, auth_mode assume_role), read through connections.connection_or_builtin(). The hard-coded account table that
# lived here (_SATELLITE_ACCOUNTS / _INFERENCE_PROFILES) is gone: a satellite "account" is the key of an assume-role
# Bedrock connection ("acc3" today), and the STS session cache is keyed by (connection_key, role_arn, external_id,
# region), so a changed role or ExternalId never reuses a session assumed for the old one. When the registry cannot be
# read, connections.BUILTIN_CONNECTIONS (the 304 seed: acc3 = AA3-Bedrock-Invoker, aa296-satellite-bedrock-acc3,
# us-west-1) keeps today's path working.
#
# AA-902 — the satellite account ID comes from the task environment (AA-CIS-Infra sets it on the api and worker task
# definitions; this repo is public and carries no account ID): the acc3 connection names the env var
# BEDROCK_ACCT_PRIMARY (account_id_env). Unset → no role ARN and the satellite path fails loudly at call time.
# bedrock_batch.py and cost_explorer.py still import ACC3_ACCOUNT_ID (their own acc3-only roles).
ACC3_ACCOUNT_ID = os.environ.get("BEDROCK_ACCT_PRIMARY", "")

_ACC3 = connections.BUILTIN_CONNECTIONS["acc3"]
ACC3_ROLE_ARN = f"arn:aws:iam::{ACC3_ACCOUNT_ID}:role/{_ACC3.role_name}"
ACC3_EXTERNAL_ID = _ACC3.external_id
ACC3_REGION = _ACC3.region

# The default satellite connection (the key of the acc3 assume-role connection).
DEFAULT_SATELLITE_ACCOUNT = "acc3"

# AA-348 — the legacy chain (client._legacy_chain, the `sonnet` / `haiku` keys) invokes Sonnet 4-6
# (`claude-sonnet-4-6`), unlike shared/llm_client/pricing.py's BEDROCK_SONNET constant (4-5-20250929), which names the
# acc2-native branch (T1) only. Deliberate, verified 16/07/2026 — do not "align" the version here from the pricing
# constant name: changing the invoked model is an IAM/availability change. Migration 304 copied these into the
# deployments sonnet@acc3 / haiku@acc3 (origin legacy_constant); the alias path (S3) calls the deployment's wire_id.
_PROFILE_ID_SONNET = "global.anthropic.claude-sonnet-4-6"
_PROFILE_ID_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
_LEGACY_PROFILE_IDS = {"sonnet": _PROFILE_ID_SONNET, "haiku": _PROFILE_ID_HAIKU}


def _inference_profile_arn(account_id: str, region: str, profile_id: str) -> str:
    return f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{profile_id}"


# The acc3 ARNs as built at import (kept for readers of the constant; the call path builds them per connection).
INFERENCE_PROFILE_SONNET = _inference_profile_arn(ACC3_ACCOUNT_ID, ACC3_REGION, _PROFILE_ID_SONNET)
INFERENCE_PROFILE_HAIKU = _inference_profile_arn(ACC3_ACCOUNT_ID, ACC3_REGION, _PROFILE_ID_HAIKU)

ANTHROPIC_VERSION = "bedrock-2023-05-31"

# session cache — avoid AssumeRole on every request (role MaxSessionDuration=3600s). AA-1002 (S3): keyed by
# (connection_key, role_arn, external_id, region) — each connection keeps its own session/expiry (AA-397), and an
# edited role / ExternalId / region is a new key, never the old session.
_cached_sessions: dict[tuple, boto3.Session] = {}
_cached_session_expiry: dict[tuple, float] = {}
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


def satellite_connection(account: str) -> connections.ProviderConnection:
    """The assume-role Bedrock connection named ``account`` (registry row, else its built-in mirror). Raises ValueError
    before any AWS call for anything else (an unknown key such as a retired account, a task-role or vendor connection),
    so a stale value can never reach STS."""
    conn = connections.connection_or_builtin(account) if isinstance(account, str) and account else None
    if conn is None or conn.kind != "bedrock" or conn.auth_mode != "assume_role":
        known = sorted(k for k, c in {**connections.BUILTIN_CONNECTIONS, **connections.registry_rows_sync()}.items()
                       if c.kind == "bedrock" and c.auth_mode == "assume_role")
        raise ValueError(f"Unknown satellite account: {account!r} (valid: {known})")
    return conn


def satellite_region(account: str) -> str:
    return satellite_connection(account).region or ACC3_REGION


def inference_profile_arn(account: str, profile_id: str) -> str:
    """The inference-profile ARN of ``profile_id`` in the satellite connection's account and region (the InvokeModel
    modelId of the legacy body)."""
    conn = satellite_connection(account)
    return _inference_profile_arn(conn.resolved_account_id() or "", conn.region or ACC3_REGION, profile_id)


def legacy_inference_profile(account: str, model: str) -> str:
    """The legacy chain's inference-profile ARN for ``model`` ("sonnet" | "haiku") on ``account``."""
    return inference_profile_arn(account, _LEGACY_PROFILE_IDS[model])


def _get_satellite_session(account: str = DEFAULT_SATELLITE_ACCOUNT) -> boto3.Session:
    """STS AssumeRole into the satellite connection ``account``, cached until 5 minutes before expiry. AA-1002 (S3):
    role, ExternalId and region come from the connection registry; the cache key is (connection_key, role_arn,
    external_id, region)."""
    conn = satellite_connection(account)
    account_id = conn.resolved_account_id() or ""
    role_arn = f"arn:aws:iam::{account_id}:role/{conn.role_name}"
    region = conn.region or ACC3_REGION
    key = (conn.connection_key, role_arn, conn.external_id, region)

    now = time.time()
    cached = _cached_sessions.get(key)
    if cached is not None and now < _cached_session_expiry.get(key, 0.0):
        return cached

    sts = boto3.client("sts")  # dùng identity ECS task role hiện tại (acc2)
    try:
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"aa-cis-ecs-{account}-{int(now)}",
            ExternalId=conn.external_id,
            DurationSeconds=3600,
        )
    except ClientError as e:
        raise BedrockUnavailable(f"AssumeRole to satellite ({account}) failed: {e}") from e

    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    _cached_sessions[key] = session
    _cached_session_expiry[key] = creds["Expiration"].timestamp() - _SESSION_REFRESH_MARGIN_SECONDS
    return session


def get_satellite_client(service_name: str = "bedrock-runtime", account: str = DEFAULT_SATELLITE_ACCOUNT):
    """Client boto3 bất kỳ (satellite, account chỉ định qua tham số `account`)
    qua session AssumeRole dùng chung (cache TTL) — service_name mặc định
    "bedrock-runtime" (InvokeModel, như invoke_claude() đang dùng) hoặc
    "bedrock" (control-plane, vd CreateModelInvocationJob cho Batch API,
    AA-302). `account` = the key of an assume-role Bedrock connection (acc3 by default, AA-998 / AA-1002)."""
    session = _get_satellite_session(account)
    config = connection_config(satellite_connection(account)) if service_name == "bedrock-runtime" else None
    if config is None:
        return session.client(service_name, region_name=satellite_region(account))
    return session.client(service_name, region_name=satellite_region(account), config=config)


# AA-642 A3: botocore makes ONE attempt per request on every runtime client the gateway builds.
SDK_MAX_ATTEMPTS = 1


# AA-642 A3 (review S263): the read timeout of the acc3 InvokeModel client (invoke_claude) when the connection's
# ``timeout_s`` is NULL: LLMClient._bedrock's 300 s, so one long non-streamed reply is not cut at botocore's 60 s
# default (which, with one attempt, would fail it outright). Connect timeout 10 s, as native_runtime.
INVOKE_READ_TIMEOUT_S = 300
INVOKE_CONNECT_TIMEOUT_S = 10


def connection_config(conn: connections.ProviderConnection, *, default_read_timeout: Optional[int] = None,
                      connect_timeout: Optional[int] = None):
    """AA-1018: a botocore Config from the connection's ``timeout_s`` (read timeout; ``default_read_timeout`` when the
    column is NULL, botocore's own when that is None too). AA-642 A3 (root ADR 0009 decision 5, "the gateway owns
    retries"): ONE attempt per request (``total_max_attempts`` 1, standard mode) whatever the connection's
    ``max_retries`` says: a throttle or an outage moves on to the route's next deployment / model at once (the
    gateway's own attempt count, ``errors.MAX_CHAIN_ATTEMPTS``), it is never repeated inside botocore. Runtime clients
    only, never the Batch control plane."""
    from botocore.config import Config
    kw: dict = {"retries": {"total_max_attempts": SDK_MAX_ATTEMPTS, "mode": "standard"}}
    read_timeout = conn.timeout_s if conn.timeout_s is not None else default_read_timeout
    if read_timeout is not None:
        kw["read_timeout"] = int(read_timeout)
    if connect_timeout is not None:
        kw["connect_timeout"] = int(connect_timeout)
    return Config(**kw)


_native_runtimes: dict[tuple, object] = {}
_native_lock = threading.Lock()


def native_runtime(conn: connections.ProviderConnection, *, read_timeout: int = 300,
                   max_attempts: Optional[int] = None):
    """AA-1002 (S3): a bedrock-runtime client on the task role (a ``task_role`` Bedrock connection, acc2), one per
    (connection, region, timeout, attempts). The default read timeout is LLMClient._bedrock's (300 s). AA-1018: the
    connection's ``timeout_s`` replaces the caller's value when the column is set. AA-642 A3: one attempt per request
    (``SDK_MAX_ATTEMPTS``; the gateway owns retries), the connection's ``max_retries`` is not read; ``max_attempts``
    is accepted for old callers and capped at that one attempt."""
    if conn.kind != "bedrock" or conn.auth_mode != "task_role":
        raise ValueError(f"connection {conn.connection_key!r} is not a task-role Bedrock connection")
    if conn.timeout_s is not None:
        read_timeout = int(conn.timeout_s)
    max_attempts = SDK_MAX_ATTEMPTS
    region = conn.region or os.environ.get("BEDROCK_REGION", "us-west-1")
    key = (conn.connection_key, region, read_timeout, max_attempts)
    with _native_lock:
        client = _native_runtimes.get(key)
        if client is None:
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(read_timeout=read_timeout, connect_timeout=10,
                                                retries={"total_max_attempts": max_attempts, "mode": "standard"}))
            _native_runtimes[key] = client
        return client


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
    profile_id: Optional[str] = None,
) -> BedrockInvokeResult:
    """
    model: "sonnet" (editorial, S1 rewrite) | "haiku" (schema/fast tasks)
    account: the key of an assume-role Bedrock connection ("acc3", AA-998 / AA-1002); anything else raises ValueError.
    profile_id: AA-1002 (S3) — the inference profile to invoke (a deployment's wire_id, the alias path); None = the
      legacy constant of `model` (_PROFILE_ID_SONNET / _PROFILE_ID_HAIKU). The ARN is built in the connection's
      account and region either way.
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
    inference_profile = inference_profile_arn(account, profile_id or _LEGACY_PROFILE_IDS[model])  # ValueError first
    model_label = "sonnet-4-6" if model == "sonnet" else "haiku-4-5"
    region = satellite_region(account)

    t0 = time.time()
    try:
        session = _get_satellite_session(account)
        # AA-642 A3 (review S263): the same one-attempt Config as every other runtime client the gateway builds
        # (connection_config). Without it botocore's legacy defaults applied (up to 5 attempts per InvokeModel), so
        # one gateway attempt on the legacy chain, an S3 alias step or an anthropic_native acc3 deployment could be
        # five provider calls.
        bedrock_rt = session.client("bedrock-runtime", region_name=region, config=connection_config(
            satellite_connection(account), default_read_timeout=INVOKE_READ_TIMEOUT_S,
            connect_timeout=INVOKE_CONNECT_TIMEOUT_S))

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
