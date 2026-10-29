"""AA-1002 (gateway slice S2) — the Anthropic Messages API adapter on a fake HTTP transport (no network, no key).

What reaches the wire (shape, headers, no sampling / forced tool / fallbacks), the credential and endpoint policy
(checked before any request), per-connection timeout and retries, the stream and usage mapping, and the error
classes the gateway and the job runner read (AA-900).
"""
import pytest

from shared.llm_client import connections, errors
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.connections import ConnectionCredentialError, ConnectionPolicyError
from tests.unit.aa1002_fake_anthropic import (
    FAKE_KEY,
    FakeAnthropic,
    anthropic_connection,
    error_reply,
    install,
    stream_reply,
)

SCHEMA = {"name": "verdict", "schema": {
    "type": "object",
    "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 10},
                   "tags": {"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 3},
                   "detail": {"type": "object", "properties": {"why": {"type": "string"}}}},
    "required": ["score"]}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnthropic()
    f.reader = install(monkeypatch, f)
    yield f
    am._clients.clear()
    connections._credentials.clear()


def _send(conn=None, **kw):
    args = dict(connection=conn or anthropic_connection(), wire_id="claude-sonnet-5-5", system="sys", user="usr",
                max_tokens=256, stage="t2_judge")
    args.update(kw)
    return am.send(**args)


# ── what reaches the wire ──────────────────────────────────────────────────────────────────────────────────────────

def test_request_shape_headers_and_key_from_the_secret(fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    out = _send()
    req = fake.requests[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"          # explicit base_url beats the env
    assert req["headers"]["authorization"] == f"Bearer {FAKE_KEY}"       # the aa-llm/ secret, not the env key
    assert "sk-from-env-must-not-be-used" not in repr(req["headers"])
    assert req["headers"]["anthropic-version"] == "2026-10-21"
    assert "anthropic-beta" not in req["headers"]
    body = req["body"]
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config", "stream"}
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 256 and body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "usr"}] and body["system"] == "sys"
    assert body["output_config"] == {"effort": "high"}
    for forbidden in ("temperature", "top_p", "top_k", "tools", "tool_choice", "thinking", "fallbacks"):
        assert forbidden not in body
    fake.reader.assert_called_once_with("aa-llm/dev/anthropic-direct")
    assert out.text == "hello" and out.provider_model == "claude-sonnet-5-5" and out.request_id == "req_test"


@pytest.mark.parametrize("wire_id,effort", [("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "medium"),
                                            ("claude-haiku-5-5", "medium")])
def test_structured_output_uses_output_config_format_never_a_forced_tool(fake, wire_id, effort):
    fake.replies.append(stream_reply('{"score": 7}', model=wire_id))
    out = _send(wire_id=wire_id, json_schema=SCHEMA)
    body = fake.bodies[0]
    assert "tools" not in body and "tool_choice" not in body
    assert body["output_config"]["effort"] == effort                   # explicit, = the model's own default
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    sent = fmt["schema"]
    assert sent["additionalProperties"] is False
    assert sent["properties"]["detail"]["additionalProperties"] is False
    assert "minimum" not in sent["properties"]["score"] and "maximum" not in sent["properties"]["score"]
    assert "maxItems" not in sent["properties"]["tags"] and "maxLength" not in sent["properties"]["tags"]["items"]
    assert SCHEMA["schema"]["properties"]["score"]["minimum"] == 0       # the caller's schema is not mutated
    assert out.structured is True and out.text == '{"score": 7}'


def test_a_schema_the_api_cannot_express_is_asked_in_the_system_prompt(fake):
    open_map = {"name": "counts", "schema": {"type": "object", "additionalProperties": {"type": "integer"}}}
    out = _send(json_schema=open_map)
    body = fake.bodies[0]
    assert "format" not in body["output_config"]
    assert body["system"].startswith("sys\n\n") and "'counts'" in body["system"]
    assert out.structured is False


def test_effort_per_stage_then_per_deployment_then_family_default():
    defaults = {"effort": "low", "effort_by_stage": {"t10_judge": "max"}}
    assert am.resolve_effort("claude-sonnet-5-5", "t10_judge", defaults) == "max"
    assert am.resolve_effort("claude-sonnet-5-5", "t2_judge", defaults) == "low"
    assert am.resolve_effort("claude-sonnet-5-5", "t2_judge", {}) == "high"
    assert am.resolve_effort("claude-haiku-5-5", None, None) == "medium"
    assert am.resolve_effort("claude-sonnet-5-5", "x", {"effort": "turbo"}) == "high"       # invalid -> default
    assert am.resolve_effort("claude-haiku-4-5-20251001", "x", {"effort": "low"}) is None  # takes no effort
    assert am.resolve_effort("claude-sonnet-4-5", "x", {}) is None
    assert am.resolve_effort("claude-new-family", "x", {}) is None                          # unknown, unset


def test_haiku_4_5_request_carries_no_output_config(fake):
    _send(wire_id="claude-haiku-4-5-20251001")
    assert "output_config" not in fake.bodies[0]


def test_cache_breakpoint_on_the_system_prompt_only_when_the_deployment_asks(fake):
    _send(request_defaults={"cache_system_prompt": True})
    assert fake.bodies[0]["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]


# ── stream and usage ───────────────────────────────────────────────────────────────────────────────────────────────

def test_stream_deltas_reach_the_callback_and_usage_keeps_the_cache_parts(fake):
    fake.replies.append(stream_reply(["Hel", "lo"], input_tokens=40, output_tokens=12, cache_read=900,
                                     cache_write=60, model="claude-sonnet-5-5-20261001"))
    seen = []
    out = _send(on_delta=seen.append)
    assert seen == ["Hel", "lo"] and out.text == "Hello"
    assert (out.input_tokens, out.cache_read_tokens, out.cache_write_tokens, out.output_tokens) == (40, 900, 60, 12)
    assert out.provider_model == "claude-sonnet-5-5-20261001"          # what the API says answered


def test_refusal_is_returned_with_its_category(fake):
    fake.replies.append(stream_reply("", stop_reason="refusal",
                                     stop_details={"type": "refusal", "category": "cyber", "explanation": None}))
    out = _send()
    assert out.stop_reason == "refusal" and out.stop_details["category"] == "cyber"


# ── credential and endpoint policy: refused before any request ────────────────────────────────────────────────────

@pytest.mark.parametrize("conn,exc", [
    (anthropic_connection(enabled=False), ConnectionCredentialError),
    (anthropic_connection(secret_name="aa-cis/dev/rds"), ConnectionCredentialError),
    (anthropic_connection(base_url="https://evil.example.com"), ConnectionCredentialError),
    (anthropic_connection(kind="openai", base_url="https://api.openai.com/v1"), ConnectionCredentialError),
    # the key comes from the aa-llm/ secret only: an env-held key (even an allowed one of another kind) is refused
    (anthropic_connection(secret_name=None, credential_env="OPENAI_API_KEY"), ConnectionCredentialError),
    (anthropic_connection(credential_env="OPENAI_API_KEY"), ConnectionCredentialError),
])
def test_unusable_connections_never_reach_the_network_or_the_secret(fake, conn, exc, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-leave")
    with pytest.raises(exc):
        _send(conn)
    assert fake.requests == []
    fake.reader.assert_not_called()


def test_policy_ties_a_credential_env_to_its_kind():
    assert set(connections.CREDENTIAL_ENV_KIND) == set(connections.CREDENTIAL_ENVS)
    problems = anthropic_connection(secret_name=None, credential_env="OPENAI_API_KEY").policy_problems()
    assert any("not a 'anthropic' key" in p for p in problems)
    openai = connections.ProviderConnection(connection_key="openai-direct", kind="openai", auth_mode="api_key",
                                            label="x", data_scope="vendor_direct", enabled=True,
                                            credential_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1")
    assert openai.policy_problems() == []


def test_default_base_url_is_the_allowlisted_api(fake):
    _send(anthropic_connection(base_url=None))
    assert fake.requests[0]["url"].startswith("https://api.anthropic.com/")


def test_base_url_check_runs_in_the_adapter_too(monkeypatch, fake):
    monkeypatch.setattr(am, "DEFAULT_BASE_URL", "https://api.anthropic.com.evil.example")
    with pytest.raises(ConnectionPolicyError):
        _send(anthropic_connection(base_url=None))
    assert fake.requests == []


# ── timeout and retries per connection ─────────────────────────────────────────────────────────────────────────────

def test_timeout_and_retries_come_from_the_connection_never_the_sdk_default(fake):
    _send(anthropic_connection(timeout_s=45, max_retries=0))
    client = next(iter(am._clients.values()))[1]   # (key, client)
    assert client.timeout.read == 45.0 and client.timeout.connect == 10.0 and client.max_retries == 0
    am._clients.clear()
    _send(anthropic_connection(timeout_s=None, max_retries=None))
    client = next(iter(am._clients.values()))[1]   # (key, client)
    assert client.timeout.read == am.DEFAULT_TIMEOUT_S == 300 and client.max_retries == am.DEFAULT_MAX_RETRIES == 1


def test_a_429_is_retried_per_connection_and_honours_retry_after(fake):
    fake.replies += [error_reply(429, "rate_limit_error", headers={"retry-after-ms": "1"}), stream_reply("ok")]
    out = _send(anthropic_connection(max_retries=1))
    assert out.text == "ok" and len(fake.requests) == 2


def test_no_retry_when_the_connection_says_zero(fake):
    fake.replies += [error_reply(429, "rate_limit_error", headers={"retry-after": "7"})]
    with pytest.raises(am.AnthropicAPIError) as e:
        _send(anthropic_connection(max_retries=0))
    assert len(fake.requests) == 1
    assert e.value.llm_error_class == errors.THROTTLED and e.value.retry_after_s == 7.0
    assert e.value.status_code == 429 and errors.classify(e.value) == errors.THROTTLED


# ── error classes (AA-900) ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,etype,message,cls", [
    (429, "rate_limit_error", "slow down", errors.THROTTLED),
    (529, "overloaded_error", "Overloaded", errors.THROTTLED),
    (401, "authentication_error", "invalid x-api-key", errors.ACCESS_DENIED),
    (403, "permission_error", "not allowed", errors.ACCESS_DENIED),
    (402, "billing_error", "payment required", errors.QUOTA_EXHAUSTED),
    (400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.",
     errors.QUOTA_EXHAUSTED),
    (400, "invalid_request_error", "You have reached your specified workspace API usage limits. You will regain "
     "access on 2026-11-01 at 00:00 UTC.", errors.QUOTA_EXHAUSTED),
    (400, "invalid_request_error", "messages: field required", errors.OTHER),
    (500, "api_error", "Internal server error", errors.OTHER),
    # status first: a rate limit whose message mentions billing / usage limits stays retryable
    (429, "rate_limit_error", "This request would exceed your rate limit; see usage limits and billing.",
     errors.THROTTLED),
    (529, "overloaded_error", "Overloaded (billing unaffected)", errors.THROTTLED),
    (500, "api_error", "billing service hiccup", errors.OTHER),
])
def test_status_errors_carry_the_aa900_class(fake, status, etype, message, cls):
    fake.replies.append(error_reply(status, etype, message))
    with pytest.raises(am.AnthropicAPIError) as e:
        _send()
    assert e.value.llm_error_class == cls and errors.classify(e.value) == cls
    # a stored job error (text only) classifies the same way
    assert errors.classify_text(str(e.value)) == cls


def test_a_401_drops_the_cached_key_so_a_rotated_key_is_read(fake):
    _send()
    assert fake.reader.call_count == 1
    _send()
    assert fake.reader.call_count == 1                                    # cached
    fake.replies.append(error_reply(401, "authentication_error", "invalid x-api-key"))
    with pytest.raises(am.AnthropicAPIError):
        _send()
    assert am._clients == {}
    _send()
    assert fake.reader.call_count == 2                                    # read again after the 401


def test_connection_failure_is_other(monkeypatch, fake):
    import httpx2

    def boom(request):
        raise httpx2.ConnectError("refused", request=request)
    monkeypatch.setattr(fake, "handler", boom)
    am._clients.clear()
    with pytest.raises(am.AnthropicAPIError) as e:
        _send()
    assert e.value.llm_error_class == errors.OTHER


# ── schema helper ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_api_schema_closes_objects_and_refuses_open_ones():
    assert am.api_schema({"type": "object", "properties": {"a": {"type": "string"}}}) == {
        "type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
    assert am.api_schema({"type": "object"}) is None
    assert am.api_schema({"type": "object", "properties": {}, "additionalProperties": True}) is None
    nested = {"type": "object", "properties": {"x": {"anyOf": [{"type": "object"}, {"type": "null"}]}}}
    assert am.api_schema(nested) is None
    arr = am.api_schema({"type": "object", "properties": {"l": {"type": "array", "minItems": 3, "items": {
        "type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]}}}})
    assert "minItems" not in arr["properties"]["l"]
    assert arr["properties"]["l"]["items"]["additionalProperties"] is False
