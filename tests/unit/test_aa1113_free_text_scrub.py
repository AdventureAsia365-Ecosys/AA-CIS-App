"""AA-1113 (root ADR 0009 decision 10, accepted S283): the gateway's free-text PII scrub, fail closed.

- The port of the CRM log scrub (AA-Partner ``app/observability/scrub.py`` ``scrub_text`` + ``app/privacy/patterns.py``)
  keeps its behaviour: what is redacted, what survives (dates, UUIDs, plain links, catalogue numbers).
- A ``t9_write`` / ``t2_generate`` / ``t8_angle_gen`` prompt with a planted e-mail and phone reaches the fake model
  scrubbed; a catalogue stage is untouched (the same request object).
- A scrub that cannot run refuses the call: no provider attempt, one 0 USD ledger row (refused / other, the
  ``pii_scrub`` receipt), no planted value in the row.
- The reply of a scrubbed call keeps its ``billed_fields`` (connection, latency, attempt, trace, prompt hash).
- The platform's own static prompts of those stages scrub to themselves (a prompt edit never trips the scrub).
No database, no AWS, no vendor. Every value below is invented; the 12-digit id and the token are built at run time
(the resource-id scanner and the secret scan read literals).
"""
import hashlib
import json
from collections import Counter

import pytest

from shared.llm_client import call_log, free_text_scrub, pii_patterns
from shared.llm_client.call_log import billed_fields
from shared.llm_client.client import attempt_outcome
from shared.llm_client.errors import OTHER, classify
from shared.llm_client.free_text_scrub import (FREE_TEXT_STAGES, FreeTextScrubFailed, is_free_text_stage,
                                               scrub_for_stage, scrub_text)
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World, _anthropic

PLANTED_EMAIL = "lan.tester@example.test"
PLANTED_PHONE = "+84 912 345 678"
PLANTED_MOBILE = "0987 654 321"
CCCD = "001085" + "012345"
TOKEN = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxIn0" + ".abc"


# ── the ported scrub ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("text", "expected"), [
    ("write to lan.tester@example.test today", "write to [email] today"),
    ("call +84 912 345 678 now", "call [phone] now"),
    ("SĐT: 0912345678.", "SĐT: [phone]."),
    ("gọi 0912 345 678", "gọi [phone]"),
    ("US (415) 555-0132", "US [phone]"),
    ("card 4111 1111 1111 1111", "card [card]"),
    ("passport C1234567", "passport [passport]"),
    ("your verification code is 7391", "your verification code is [otp]"),
    ("see https://files.example.test/doc?X-Amz-Signature=abcdef", "see [link]"),
    ("url ?api_key=abc123&x=1", "url ?api_key=***&x=1"),
    ("Key (email)=(someone@example.test) already exists", "Key (email)=([Filtered]) already exists"),
])
def test_the_port_redacts_what_the_crm_log_scrub_redacts(text, expected):
    assert scrub_text(text) == expected


def test_a_national_id_and_a_token_are_redacted():
    assert scrub_text(f"CCCD {CCCD}") == "CCCD [national_id]"
    assert scrub_text(f"token {TOKEN}") == "token [jwt]"


@pytest.mark.parametrize("text", [
    "Day 3: 12/03/2027 depart 08:30, 4 nights, from 1,250 USD per person.",   # trip dates and prices stay
    "Tour 7c9e6679-7425-40de-944b-e07fc1f2b6a1, group of 12, 2 guides.",        # a UUID stays
    "Book on https://agency.example.test/tours/river-valley-5-days",            # a plain link stays
    "Born 12 March 1985",                                                        # dates are never redacted here
    "Hike 1200 m to the 3143 m summit, then 45 km by road.",
])
def test_catalogue_text_survives(text):
    assert scrub_text(text) == text


def test_counts_name_the_labels_never_the_values():
    counts: Counter = Counter()
    scrub_text(f"mail {PLANTED_EMAIL} or {PLANTED_PHONE} or {PLANTED_MOBILE}", counts)
    assert counts == Counter({"[email]": 1, "[phone]": 2})


def test_the_detectors_are_the_crm_detectors():
    found = [(s.kind) for s in pii_patterns.find("room 12 0912 345 678, card 4111 1111 1111 1111")]
    assert found == ["PHONE", "CARD"]
    assert pii_patterns.KINDS == ("LINK", "SECRET", "EMAIL", "DOB", "CARD", "NATIONAL_ID", "PASSPORT", "PHONE", "OTP")


# ── which stages ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_free_text_stages_are_the_three_tenant_writers():
    assert FREE_TEXT_STAGES == {"t9_write", "t2_generate", "t8_angle_gen"}


@pytest.mark.parametrize("stage", ["s1_generate", "a3_place_card", "tp_compose", "t10_judge", None, ""])
def test_another_stage_gets_the_same_request_object(stage):
    req = LLMRequest(system_prompt="s", user_prompt=f"mail {PLANTED_EMAIL}", stage=stage)
    assert not is_free_text_stage(stage)
    assert scrub_for_stage(req) is req


@pytest.mark.parametrize("stage", ["t9_write", "t2_generate", "t8_angle_gen"])
def test_a_free_text_request_is_scrubbed_everywhere_with_a_receipt(stage):
    class Scope:
        extra: dict = {}
    scope = Scope()
    scope.extra = {}
    req = LLMRequest(system_prompt=f"Brand contact {PLANTED_EMAIL}", user_prompt=f"Seed: call {PLANTED_PHONE}",
                     few_shots=[{"role": "user", "content": f"ring {PLANTED_MOBILE}"}], stage=stage)
    out = scrub_for_stage(req, scope)
    assert out is not req and req.user_prompt.endswith(PLANTED_PHONE)        # the caller's object is not changed
    assert out.system_prompt == "Brand contact [email]" and out.user_prompt == "Seed: call [phone]"
    assert out.few_shots == [{"role": "user", "content": "ring [phone]"}]
    assert scope.extra["pii_scrub"] == {"version": free_text_scrub.VERSION, "status": "scrubbed",
                                        "redacted": {"[email]": 1, "[phone]": 2}}


def test_a_scrub_that_raises_is_refused_and_says_no_value(monkeypatch):
    def boom(text, counts=None):
        raise RuntimeError(f"cannot scrub {text}")
    monkeypatch.setattr(free_text_scrub, "scrub_text", boom)
    req = LLMRequest(system_prompt="s", user_prompt=f"mail {PLANTED_EMAIL}", stage="t9_write")
    with pytest.raises(FreeTextScrubFailed) as caught:
        scrub_for_stage(req)
    assert PLANTED_EMAIL not in str(caught.value) and "t9_write" in str(caught.value)
    assert classify(caught.value) == OTHER


def test_a_scrub_that_leaves_an_email_is_refused(monkeypatch):
    monkeypatch.setattr(free_text_scrub, "scrub_text", lambda text, counts=None: text)
    req = LLMRequest(system_prompt="s", user_prompt=f"mail {PLANTED_EMAIL}", stage="t2_generate")
    with pytest.raises(FreeTextScrubFailed, match="ValueError"):
        scrub_for_stage(req)


def test_the_refusal_outcome_is_refused_other():
    exc = FreeTextScrubFailed("t9_write", "RuntimeError")
    assert attempt_outcome(exc) == ("refused", OTHER)
    try:
        raise RuntimeError("wrapped") from exc
    except RuntimeError as wrapped:
        assert attempt_outcome(wrapped) == ("refused", OTHER)


# ── through the gateway ─────────────────────────────────────────────────────────────────────────────────────────────

class _RecordingWorld(World):
    """World whose fake Bedrock also keeps the request each Converse call received."""

    def __init__(self, monkeypatch):
        self.sent: list = []
        super().__init__(monkeypatch)

    def runtime(self, where):
        rt = super().runtime(where)
        inner = rt.converse.side_effect

        inner_invoke = rt.invoke_model.side_effect

        def converse(**kw):
            self.sent.append(json.dumps({"system": kw.get("system"), "messages": kw.get("messages")}))
            return inner(**kw)

        def invoke(**kw):
            self.sent.append(kw["body"] if isinstance(kw["body"], str) else kw["body"].decode("utf-8"))
            return inner_invoke(**kw)
        rt.converse.side_effect = converse
        rt.invoke_model.side_effect = invoke
        return rt


@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = _RecordingWorld(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


@pytest.fixture
def ledger(monkeypatch):
    rows: list = []

    async def record_calls(rows_):
        rows.extend(rows_)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    return rows


def _stage(monkeypatch, stage):
    cfg = StageConfig(stage=stage, role="writer", provider="claude", model_id="gpt-6-luna", account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)


PROMPT = f"Agency note: the guest wrote from {PLANTED_EMAIL}, phone {PLANTED_PHONE}, mobile {PLANTED_MOBILE}."


@pytest.mark.parametrize("stage", ["t9_write", "t2_generate", "t8_angle_gen"])
def test_a_planted_email_and_phone_reach_the_model_scrubbed(world, monkeypatch, ledger, stage):
    _stage(monkeypatch, stage)
    resp = world.generate(stage=stage, system_prompt="Write for the brand.", user_prompt=PROMPT)
    assert resp.content == "from acc3" and len(world.sent) == 1
    sent = world.sent[0]
    for value in (PLANTED_EMAIL, PLANTED_PHONE, PLANTED_MOBILE, "912 345 678", "654 321"):
        assert value not in sent
    assert "[email]" in sent and sent.count("[phone]") == 2


def test_a_legacy_key_of_a_free_text_stage_is_scrubbed_too(world, monkeypatch, ledger):
    # lesson S260: a new gate must cover the legacy haiku / sonnet keys, which skip the deployment plan
    _stage(monkeypatch, "t9_write")
    world.generate(stage="t9_write", model_tier="haiku", system_prompt="Write for the brand.", user_prompt=PROMPT)
    assert world.sent and all(PLANTED_EMAIL not in sent and "912 345 678" not in sent for sent in world.sent)
    assert "[email]" in world.sent[-1]


def test_every_model_of_a_free_text_route_gets_the_scrubbed_prompt(world, monkeypatch, ledger):
    cfg = StageConfig(stage="t2_generate", role="writer", provider="claude", model_id="gpt-6-luna",
                      account_route="acc3", fallback_model_ids=("sonnet-5-5",),
                      allowed_scopes=("aws_only", "vendor_direct"))
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)
    world.fail["acc3"] = RuntimeError("fake acc3 outage")
    world.conns["anthropic-direct"] = _anthropic(True)
    world.reset()
    resp = world.generate(stage="t2_generate", system_prompt="Write the tour.", user_prompt=PROMPT)
    assert resp.content == "from anthropic"
    assert len(world.sent) >= 1 and all(PLANTED_EMAIL not in sent for sent in world.sent)
    sent_to_vendor = world.anthropic_send.call_args.kwargs
    flat = json.dumps({k: v for k, v in sent_to_vendor.items() if k != "connection"}, default=str)
    assert PLANTED_EMAIL not in flat and "912 345 678" not in flat and "[email]" in flat


def test_a_catalogue_stage_reaches_the_model_unchanged(world, monkeypatch, ledger):
    _stage(monkeypatch, "s1_generate")
    world.generate(stage="s1_generate", system_prompt="Write the tour.", user_prompt=PROMPT)
    assert PLANTED_EMAIL in world.sent[0] and "[email]" not in world.sent[0]


def test_a_scrub_failure_refuses_the_call_with_a_0_usd_row(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write")

    def boom(request):
        raise MemoryError("scrub could not run")
    monkeypatch.setattr(free_text_scrub, "scrub_request", boom)
    with pytest.raises(FreeTextScrubFailed):
        world.generate(stage="t9_write", system_prompt="Write for the brand.", user_prompt=PROMPT)
    assert world.calls == [] and world.sent == []                            # nothing was sent
    call_log.wait_for_pending_writes()
    assert len(ledger) == 1
    row = ledger[0]
    assert (row["status"], row["error_class"], row["cost_usd"]) == ("refused", OTHER, 0.0)
    assert row["stage"] == "t9_write" and row["model"] == "gpt-6-luna" and row["attempt_no"] is None
    assert row["quality_signal"]["pii_scrub"] == {"version": free_text_scrub.VERSION, "status": "failed",
                                                  "error_type": "MemoryError"}
    flat = json.dumps(row, default=str)
    assert PLANTED_EMAIL not in flat and "912 345 678" not in flat


def test_a_scrubbed_reply_keeps_its_billed_fields(world, monkeypatch, ledger):
    _stage(monkeypatch, "t9_write")
    system = f"Write for the brand. Contact {PLANTED_EMAIL}."
    resp = world.generate(stage="t9_write", system_prompt=system, user_prompt=PROMPT)
    fields = billed_fields(resp)
    assert fields["latency_ms"] is not None and fields["attempt_no"] == 1
    assert fields["deployment_wire_id"] == "global.openai.gpt-6-luna" and fields["route_trace"]
    assert fields["route_position"] == 0
    # the prompt hash names the prompt the call site built (its registered version), not the scrubbed copy
    assert fields["prompt_hash"] == hashlib.sha256(system.encode("utf-8")).hexdigest()
    assert set(call_log.BILLED_FIELD_KEYS) <= set(fields)


# ── the platform's own prompts never trip the scrub ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("module", [
    "services.acp_content_writing.prompts",       # t9_write
    "services.acp_angle_gate.prompts",            # t8_angle_gen
    "services.content_generation.prompts",        # t2_generate (shared with s1)
    "services.content_generation.s1_writer_v2",   # the v2 writer prompt blocks
])
def test_static_prompts_of_the_free_text_stages_scrub_to_themselves(module):
    import importlib
    mod = importlib.import_module(module)
    texts = {n: v for n, v in vars(mod).items() if isinstance(v, str) and len(v) > 200}
    assert texts
    changed = [n for n, v in texts.items() if scrub_text(v) != v]
    assert changed == [], f"{module}: these prompt texts lose words to the PII scrub: {changed}"
