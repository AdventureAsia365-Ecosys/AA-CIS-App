"""
shared/llm_client/bedrock_satellite.py — AA-296 Satellite Bedrock client
ADDITIVE: Bedrock satellite là primary writer, GPT-4.1 giữ nguyên làm fallback.
KHÔNG xoá/sửa code GPT-4.1 hiện tại — chỉ thêm nhánh mới trước nó, caller quyết định fallback.

STATUS: VERIFIED THẬT qua terminal độc lập 16/07/2026 — cả Sonnet 4.6 lẫn Haiku 4.5
invoke thành công qua satellite chain. Đã dọn trust policy (bỏ statement debug tạm).

═══════════════════════════════════════════════════════════════════════════
⚠️ BUG QUAN TRỌNG ĐÃ PHÁT HIỆN (16/07/2026) — ĐỌC TRƯỚC KHI SỬA FILE NÀY:

Khi gọi bedrock:InvokeModel qua một ASSUMED-ROLE session (STS AssumeRole,
chính là cơ chế satellite này dùng), Bedrock IAM evaluation dùng dạng ARN
foundation-model KHÔNG CÓ REGION:
    arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6
                     ^^ region rỗng, 3 dấu : liền nhau

...KHÁC với dạng CÓ region mà CloudTrail/Console luôn hiển thị:
    arn:aws:bedrock:us-west-1::foundation-model/anthropic.claude-sonnet-4-6

Không có tài liệu AWS chính thức nào mô tả rõ hành vi này. Phát hiện được
bằng thực nghiệm: gọi trực tiếp từ terminal (ngoài ECS/container), test cả
2 dạng ARN riêng lẻ rồi cùng lúc — chỉ khi policy có ĐỦ CẢ 2 dạng thì
invoke mới thành công nhất quán.

⇒ IAM permission policy trên role AA-Bedrock-Invoker (acc1) PHẢI có cả 2
  dạng ARN cho mỗi model, không chỉ 1. Nếu sau này thêm model mới (không
  chỉ Sonnet 4.6/Haiku 4.5), nhớ thêm CẢ 2 dạng ARN vào policy, không chỉ
  dạng có region như trực giác thường làm.

⚠️ Đừng tin errorMessage (free text) khi debug AccessDeniedException từ
  Bedrock qua assumed-role — nó có thể hiển thị SAI dạng ARN đang thực sự
  được đánh giá (đã quan sát: cùng policy, cùng request, error text đổi
  qua lại giữa 2 dạng không theo quy luật). Tin resources[].ARN trong
  CloudTrail (structured field) nếu cần đối chiếu, không tin câu chữ.
═══════════════════════════════════════════════════════════════════════════

STEP 0 verified (16/07/2026):
- Role acc1: arn:aws:iam::867490540162:role/AA-Bedrock-Invoker
  trust: CHỈ arn:aws:iam::005097885195:role/aa-cis-dev-ecs-task-role,
         Condition StringEquals sts:ExternalId=aa296-satellite-bedrock
  permission (policy InvokeApprovedClaudeModelsOnly): bedrock:InvokeModel +
  InvokeModelWithResponseStream, scoped 6 Resource ARN (2 model × 3 dạng:
  inference-profile, foundation-model+region, foundation-model region-rỗng)
- Role acc2: aa-cis-dev-ecs-task-role có inline policy RIÊNG
  (aa-cis-dev-ecs-assume-bedrock-invoker, KHÔNG đụng policy cũ
  aa-cis-dev-ecs-task-policy) cho phép sts:AssumeRole đúng role acc1 trên.
- Inference profile prefix Claude = "global." (KHÔNG phải "us." như ghi cũ
  trong skill/memory trước AA-296 — đã xác nhận qua Console, tài liệu skill
  cần update).
- Response schema Anthropic-on-Bedrock: content[0].text, KHÔNG phải
  choices[].message.content như Writer/Palmyra (OpenAI-compatible) — nếu
  sau này thêm Palmyra vào cùng client, cần parser riêng theo provider.

TODO còn lại trước khi coi AA-296 hoàn tất production-ready:
[x] AssumeRole chain verify thật (qua S3, qua terminal độc lập)
[x] Invoke Sonnet 4.6 + Haiku 4.5 thành công (terminal độc lập)
[x] Cache SessionToken theo TTL trong code thật (không AssumeRole mỗi request)
[ ] CloudWatch metric t3_fallback_used khi rơi về GPT-4.1
[ ] Unit test: mock STS + Bedrock, verify fallback path khi satellite lỗi
[x] Tích hợp vào S1 rewrite node thật (services/content_generation/ hoặc
    tương đương) — file này mới chỉ là client, chưa gọi từ pipeline
[ ] Update skill aa-ecosys-repos / ai-nghiep với bài học "2-dạng ARN" này

═══════════════════════════════════════════════════════════════════════════
AA-397/AA-398 — acc3 (786888028788) thêm vào làm satellite chính, acc1 lùi
xuống fallback (2026-08-12):

- Cả session cache VÀ hàm public đều nhận tham số `account: str` ("acc1" |
  "acc3", mặc định "acc1" — giữ nguyên hành vi cũ cho caller chưa cập nhật,
  vd api/routers/v1_atoms.py's Batch control-plane client, xem dưới).
- acc3's role `AA3-Bedrock-Invoker` CHỈ có `bedrock:InvokeModel` +
  `InvokeModelWithResponseStream` (accounts/aa365/bedrock_satellite_assume.tf
  + accounts/acc3-bedrock/main.tf, AA-CIS-Infra, cả 2 đã apply thật) — KHÔNG
  có Batch permissions (CreateModelInvocationJob/PassRole), quyết định rõ
  trong AA-CIS-Infra's docs/implementation-notes/AA-397.md Bước 1 Decisions.
  ⇒ get_satellite_client("bedrock", account="acc3") cho Batch control-plane
  (AA-302, api/routers/v1_atoms.py) SẼ AccessDenied — cố ý KHÔNG đổi call đó
  sang acc3, giữ default account="acc1".
- Invoke-model test thật (qua ECS exec, đúng chain production) xác nhận cả 2
  model pass trên acc3 sau khi Nghiep enable AWS Marketplace subscription cho
  Haiku 4.5 (Sonnet 4.6 đã có access từ trước) — xem AA-CIS-App's
  docs/implementation-notes/AA-397.md.
- Thêm log phân loại AccessDeniedException riêng trong invoke_claude() (không
  đổi luồng try/except gốc) — để phân biệt "model access chưa enable trên
  account này" (transient, có thể tự khắc phục qua Marketplace) khỏi lỗi khác
  (network, IAM sai, model id sai...) khi đọc CloudWatch log sau này.
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

import boto3
import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger()

# ---------------------------------------------------------------- config
ACC1_ROLE_ARN = "arn:aws:iam::867490540162:role/AA-Bedrock-Invoker"
ACC1_EXTERNAL_ID = "aa296-satellite-bedrock"
ACC1_REGION = "us-west-1"

ACC3_ROLE_ARN = "arn:aws:iam::786888028788:role/AA3-Bedrock-Invoker"
ACC3_EXTERNAL_ID = "aa296-satellite-bedrock-acc3"
ACC3_REGION = "us-west-1"

# AA-397 — 1 entry per satellite account, session cache + invoke_claude() đều
# tra cứu qua dict này (thêm account mới chỉ cần thêm 1 entry, không sửa logic).
_SATELLITE_ACCOUNTS = {
    "acc1": {"role_arn": ACC1_ROLE_ARN, "external_id": ACC1_EXTERNAL_ID, "region": ACC1_REGION},
    "acc3": {"role_arn": ACC3_ROLE_ARN, "external_id": ACC3_EXTERNAL_ID, "region": ACC3_REGION},
}

# Giữ tên cũ (không xoá — vẫn export, phòng import khác trong repo) trỏ về acc1,
# đồng thời thêm bảng tra cứu account-aware cho account mới.
#
# AA-348 — cả 2 satellite account đều invoke Sonnet 4-6 (`claude-sonnet-4-6`), KHÁC với
# shared/llm_client/pricing.py's BEDROCK_SONNET constant (4-5-20250929) — constant đó chỉ đặt tên
# đúng cho nhánh acc2-native (T1); nhánh satellite (T1.5a/b, file này) CỐ Ý invoke version mới hơn
# (verified thật 16/07/2026, xem STATUS ở đầu file) — không phải lỗi copy-paste, đã xác nhận lại
# qua investigation của AA-348. Đừng "sửa cho khớp" version ở đây nếu chỉ dựa vào tên hằng số ở
# pricing.py — đổi model thật invoke ở đây là một thay đổi hành vi/infra thật (IAM ARN scoping +
# availability check trên cả 2 satellite account), ngoài phạm vi 1 việc đổi tên/comment.
INFERENCE_PROFILE_SONNET = "arn:aws:bedrock:us-west-1:867490540162:inference-profile/global.anthropic.claude-sonnet-4-6"
INFERENCE_PROFILE_HAIKU = (
    "arn:aws:bedrock:us-west-1:867490540162:inference-profile/"
    "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)

_INFERENCE_PROFILES = {
    "acc1": {"sonnet": INFERENCE_PROFILE_SONNET, "haiku": INFERENCE_PROFILE_HAIKU},
    "acc3": {
        "sonnet": "arn:aws:bedrock:us-west-1:786888028788:inference-profile/global.anthropic.claude-sonnet-4-6",
        "haiku": (
            "arn:aws:bedrock:us-west-1:786888028788:inference-profile/"
            "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
    },
}

ANTHROPIC_VERSION = "bedrock-2023-05-31"

# session cache — tránh gọi AssumeRole mỗi request (STS session mặc định 1h,
# nhưng role có MaxSessionDuration=3600s — xem Console AA-Bedrock-Invoker).
# AA-397: keyed theo account, mỗi account giữ session/expiry riêng.
_cached_sessions: dict[str, boto3.Session] = {}
_cached_session_expiry: dict[str, float] = {}
_SESSION_REFRESH_MARGIN_SECONDS = 300  # refresh 5 phút trước khi hết hạn thật


class BedrockUnavailable(Exception):
    """Caller (pipeline node) bắt exception này → gọi fallback GPT-4.1 hiện có.
    KHÔNG để lỗi satellite (AssumeRole fail, InvokeModel fail, network...)
    làm crash request người dùng — luôn có đường lui GPT-4.1."""
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


def _get_satellite_session(account: str = "acc1") -> boto3.Session:
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


def get_satellite_client(service_name: str = "bedrock-runtime", account: str = "acc1"):
    """Client boto3 bất kỳ (satellite, account chỉ định qua tham số `account`)
    qua session AssumeRole dùng chung (cache TTL) — service_name mặc định
    "bedrock-runtime" (InvokeModel, như invoke_claude() đang dùng) hoặc
    "bedrock" (control-plane, vd CreateModelInvocationJob cho Batch API,
    AA-302). `account` mặc định "acc1" — GIỮ NGUYÊN hành vi cũ cho caller
    chưa cập nhật. AA-397: acc3's role KHÔNG có Batch permissions (chỉ
    InvokeModel/InvokeModelWithResponseStream) — KHÔNG gọi account="acc3" cho
    service_name="bedrock" (control-plane), sẽ AccessDenied."""
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
    account: str = "acc1",
    on_delta: Optional[Callable[[str], None]] = None,
) -> BedrockInvokeResult:
    """
    model: "sonnet" (editorial, S1 rewrite) | "haiku" (schema/fast tasks)
    account: "acc1" | "acc3" (AA-397, mặc định "acc1" — GIỮ NGUYÊN hành vi cũ
      cho caller chưa cập nhật tham số này).
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
    Raise BedrockUnavailable khi lỗi — caller ở lớp trên (pipeline node)
    chịu trách nhiệm bắt exception này và gọi fallback GPT-4.1 hiện có
    (KHÔNG sửa code GPT-4.1 đang chạy, chỉ thêm nhánh gọi hàm này trước).
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
            # AA-637 — live-progress path: same request, streamed. The acc1/acc3 roles already
            # allow InvokeModelWithResponseStream (see get_satellite_client docstring).
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
