"""AA-1002 (S2) test helper — a fake Anthropic Messages API on an httpx2 MockTransport (no network, no key, $0).

``FakeAnthropic`` records every request the SDK sends (URL, headers, JSON body) and answers from a queue of replies:
``stream_reply(...)`` (an SSE stream like the real API's) or ``error_reply(status, type, message)``. ``install``
points the adapter at it and gives the connections module a fake secret reader.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import anthropic
import httpx2

from shared.llm_client import connections
from shared.llm_client.adapters import anthropic_messages

FAKE_KEY = "sk-ant-test-fake-key"


def stream_events(text_chunks, *, model="claude-sonnet-5-5", stop_reason="end_turn", stop_details=None,
                  input_tokens=100, output_tokens=20, cache_read=0, cache_write=0):
    events = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 1, "cache_read_input_tokens": cache_read,
                  "cache_creation_input_tokens": cache_write}}}]
    events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    for chunk in text_chunks:
        events.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}})
    events.append({"type": "content_block_stop", "index": 0})
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    events.append({"type": "message_delta", "delta": delta, "usage": {"output_tokens": output_tokens}})
    events.append({"type": "message_stop"})
    return events


def stream_reply(text="hello", **kw) -> httpx2.Response:
    chunks = text if isinstance(text, list) else [text]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in stream_events(chunks, **kw))
    return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_test"},
                           content=body.encode())


def error_reply(status: int, error_type: str, message: str = "nope", headers: Optional[dict] = None):
    return httpx2.Response(status, headers={"content-type": "application/json", **(headers or {})},
                           json={"type": "error", "error": {"type": error_type, "message": message}})


@dataclass
class FakeAnthropic:
    replies: list = field(default_factory=list)
    requests: list = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode() or "{}")
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if not self.replies:
            return stream_reply()
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply

    def http_client(self):
        return anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler))

    @property
    def bodies(self):
        return [r["body"] for r in self.requests]


def install(monkeypatch, fake: FakeAnthropic, *, secret: str = FAKE_KEY):
    """Route the adapter to ``fake``, give the resolver a fake secret store, and clear every cache. Returns the
    mock of the secret reader (call count = Secrets Manager reads)."""
    from unittest.mock import MagicMock

    anthropic_messages._clients.clear()
    connections._credentials.clear()
    monkeypatch.setattr(anthropic_messages, "_http_client_factory", fake.http_client)
    reader = MagicMock(return_value=json.dumps({"api_key": secret}))
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    return reader


def anthropic_connection(**kw) -> connections.ProviderConnection:
    base = dict(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="Anthropic API",
                data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct",
                base_url="https://api.anthropic.com", timeout_s=None, max_retries=0)
    base.update(kw)
    return connections.ProviderConnection(**base)
