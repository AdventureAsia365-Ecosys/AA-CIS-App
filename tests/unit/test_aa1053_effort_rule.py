"""AA-1053 (W1 contradiction #14) — one effort rule per stage, mapped explicitly for each provider kind.

The stage x model row (``shared.stage_model_params.effort``) is the rule; ``shared/llm_client/effort_rule.py`` is the
only module that maps it to a provider field. For one stage (t9_write at effort medium) each provider path sends the
mapped effort and puts it on ``LLMResponse.effort`` (the ledger's effort column):

- Anthropic Messages API: ``output_config.effort`` medium;
- OpenAI / Azure Chat Completions: ``reasoning_effort`` medium;
- Bedrock Converse: ``additionalModelRequestFields`` in the form the catalog row states; with no form stated (every
  row today) the request is byte-identical to before and the reply's effort is None (nothing sent).

The static guard fails when an effort field is built or the row read outside the rule module.
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import client as client_mod
from shared.llm_client import connections, effort_rule, stage_params
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.models import LLMRequest
from shared.llm_client.stage_params import StageModelParams
from tests.unit.aa1002_fake_anthropic import FakeAnthropic, install
from tests.unit.test_s271_thinking_control import GPT_OPENAI, SONNET55, _call, _openai_call

ROOT = Path(__file__).resolve().parents[2]
STAGE = "t9_write"

SONNET55_CONVERSE = CatalogModel(
    model_key="sonnet-5-5", label="s", vendor="anthropic", provider="bedrock", api_style="converse",
    bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"}, callable_via=("llm_client",),
    supports_temperature=False, max_output_tokens=128000, price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
LUNA_CONVERSE = CatalogModel(
    model_key="gpt-6-luna", label="g", vendor="openai", provider="bedrock", api_style="converse",
    bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"}, callable_via=("llm_client",),
    supports_temperature=False, max_output_tokens=128000, price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)


@pytest.fixture
def table(monkeypatch):
    """The cached stage x model table, filled by the test (no DB)."""
    rows: dict = {}
    monkeypatch.setattr(stage_params, "_params", rows)
    monkeypatch.setattr(stage_params, "_loaded_at", 10 ** 12)          # fresh: no load
    monkeypatch.delenv(stage_params.SWITCH_ENV, raising=False)
    stage_params._NOT_APPLIED_WARNED.clear()
    return rows


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnthropic()
    f.reader = install(monkeypatch, f)
    yield f
    am._clients.clear()
    connections._credentials.clear()


def _row(table, model_key, effort="medium"):
    table[(STAGE, model_key)] = StageModelParams(STAGE, model_key, effort=effort)


# ── 1. the rule (pure) ─────────────────────────────────────────────────────────────────────────────────────────────

def test_no_row_means_nothing_on_any_path(table):
    assert effort_rule.plan_openai(STAGE, GPT_OPENAI) == effort_rule.EffortPlan(effort_rule.PATH_OPENAI)
    assert effort_rule.plan_converse(STAGE, SONNET55_CONVERSE) == effort_rule.EffortPlan(effort_rule.PATH_CONVERSE)
    defaults = {"effort": "high"}
    assert effort_rule.anthropic_request_defaults(STAGE, SONNET55, defaults) is defaults
    assert effort_rule.anthropic_request_defaults(STAGE, SONNET55, None) is None


def test_the_anthropic_defaults_carry_the_stage_effort_over_the_deployments(table):
    _row(table, "sonnet-5-5")
    out = effort_rule.anthropic_request_defaults(STAGE, SONNET55, {"effort": "high",
                                                                   "effort_by_stage": {STAGE: "max", "other": "low"}})
    assert out == {"effort": "high", "effort_by_stage": {STAGE: "medium", "other": "low"}}
    assert effort_rule.anthropic_request_defaults(None, SONNET55, None) is None      # no stage: no rule


@pytest.mark.parametrize("effort,sent", [("low", "low"), ("medium", "medium"), ("high", "high"),
                                         ("xhigh", "high"), ("max", "high")])
def test_every_effort_has_an_explicit_openai_value(table, effort, sent):
    _row(table, "gpt-6-luna-openai", effort)
    plan = effort_rule.plan_openai(STAGE, GPT_OPENAI)
    assert (plan.stage_effort, plan.sent, plan.wire) == (effort, sent, {"reasoning_effort": sent})
    assert set(effort_rule.OPENAI_EFFORTS) == set(effort_rule.EFFORTS) == set(stage_params_efforts())


def stage_params_efforts():
    """The efforts the table's CHECK takes (migration 320), read from the migration file."""
    sql = (ROOT / "api/migrations/320_llm_registry_v2_families_params.sql").read_text(encoding="utf-8")
    start = sql.index("effort IS NULL OR effort IN (")
    return [v.strip(" '") for v in sql[start:sql.index(")", start)].split("(", 1)[1].split(",")]


def test_converse_sends_only_a_form_the_catalog_row_states(table):
    _row(table, "sonnet-5-5")
    _row(table, "gpt-6-luna", "max")
    plan = effort_rule.plan_converse(STAGE, SONNET55_CONVERSE)
    assert plan.sent is None and plan.wire == {} and plan.reason == effort_rule.NOT_SENT_CONVERSE_UNPROBED
    claude = dataclasses.replace(SONNET55_CONVERSE, capabilities={"converse_effort": "output_config"})
    assert effort_rule.plan_converse(STAGE, claude).wire == {"output_config": {"effort": "medium"}}
    gpt = dataclasses.replace(LUNA_CONVERSE, capabilities={"converse_effort": "reasoning_effort"})
    assert effort_rule.plan_converse(STAGE, gpt).wire == {"reasoning_effort": "high"}
    odd = dataclasses.replace(SONNET55_CONVERSE, capabilities={"converse_effort": "thinking_budget"})
    assert effort_rule.plan_converse(STAGE, odd).wire == {}


def test_a_failed_read_sends_nothing(monkeypatch):
    monkeypatch.setattr(stage_params, "effort_for", MagicMock(side_effect=RuntimeError("db down")))
    assert effort_rule.plan_openai(STAGE, GPT_OPENAI).sent is None
    assert effort_rule.stage_effort(STAGE, "sonnet-5-5") is None


# ── 2. one stage, every provider path ──────────────────────────────────────────────────────────────────────────────

def test_anthropic_messages_sends_the_mapped_effort(table, fake):
    _row(table, "sonnet-5-5")
    resp = _call(SONNET55, "claude-sonnet-5-5", stage=STAGE)
    assert fake.bodies[0]["output_config"] == {"effort": "medium"} and resp.effort == "medium"


def test_openai_chat_sends_the_mapped_effort(table):
    _row(table, "gpt-6-luna-openai")
    resp, kwargs = _openai_call(GPT_OPENAI, stage=STAGE)
    assert kwargs["reasoning_effort"] == "medium" and resp.effort == "medium"


def _converse(model, *, side_effect=None):
    rt = MagicMock()
    ok = {"output": {"message": {"content": [{"text": "A short line about Brindle Cove."}]}},
          "usage": {"inputTokens": 40, "outputTokens": 9}, "stopReason": "end_turn"}
    rt.converse.side_effect = side_effect or [ok]
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = client_mod.LLMClient()
    request = LLMRequest(system_prompt="Write one line.", user_prompt="Brindle Cove", stage=STAGE, max_tokens=4096)
    with patch("shared.llm_client.pricing._catalog_rates", lambda m: None), \
            patch("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None):
        resp = c._call_bedrock_converse(request, model, "acc3", profile_id=model.bedrock_profile_ids["acc3"],
                                        runtime=rt)
    return resp, [call.kwargs for call in rt.converse.call_args_list]


def test_converse_without_a_stated_form_is_unchanged(table):
    _, before = _converse(SONNET55_CONVERSE)
    _row(table, "sonnet-5-5")
    resp, after = _converse(SONNET55_CONVERSE)
    assert after == before and "additionalModelRequestFields" not in after[0]
    assert resp.effort is None                     # nothing sent: the ledger's effort column stays empty


@pytest.mark.parametrize("model,form,wire", [
    (SONNET55_CONVERSE, "output_config", {"output_config": {"effort": "medium"}}),
    (LUNA_CONVERSE, "reasoning_effort", {"reasoning_effort": "medium"}),
])
def test_converse_sends_the_mapped_effort_in_the_stated_form(table, model, form, wire):
    _row(table, model.model_key)
    stated = dataclasses.replace(model, capabilities={"converse_effort": form})
    resp, calls = _converse(stated)
    assert calls[0]["additionalModelRequestFields"] == wire and resp.effort == "medium"


def test_a_rejected_converse_effort_field_is_dropped_once_and_not_recorded(table):
    class ValidationException(Exception):
        pass

    _row(table, "sonnet-5-5")
    stated = dataclasses.replace(SONNET55_CONVERSE, capabilities={"converse_effort": "output_config"})
    ok = {"output": {"message": {"content": [{"text": "A short line."}]}},
          "usage": {"inputTokens": 40, "outputTokens": 9}, "stopReason": "end_turn"}
    resp, calls = _converse(stated, side_effect=[
        ValidationException("ValidationException: extraneous key [output_config] is not permitted"), ok])
    assert len(calls) == 2 and "additionalModelRequestFields" not in calls[1]
    assert resp.effort is None


# ── 3. the static guard: one owner ─────────────────────────────────────────────────────────────────────────────────

RULE_FILES = {"shared/llm_client/effort_rule.py", "shared/llm_client/stage_params.py"}
# The adapter reads the request_defaults the rule built (effort_by_stage) and writes output_config.effort itself.
FIELD_FILES = RULE_FILES | {"shared/llm_client/adapters/anthropic_messages.py"}
FIELD_NAMES = {"reasoning_effort", "effort_by_stage", "additionalModelRequestFields"}


def _sources():
    for d in ("api", "services", "shared", "worker", "scripts"):
        for p in sorted((ROOT / d).rglob("*.py")):
            if "tests" in p.parts or "node_modules" in p.parts:
                continue
            yield p.relative_to(ROOT).as_posix(), ast.parse(p.read_text(encoding="utf-8"))


def test_only_the_rule_reads_a_stage_effort_and_builds_an_effort_field():
    readers, builders = set(), set()
    for rel, tree in _sources():
        for n in ast.walk(tree):
            if isinstance(n, (ast.Attribute, ast.Name)) and getattr(n, "attr", getattr(n, "id", "")) == "effort_for":
                readers.add(rel)
            if isinstance(n, ast.Constant) and n.value in FIELD_NAMES:
                builders.add(rel)
            if isinstance(n, ast.keyword) and n.arg in FIELD_NAMES:
                builders.add(rel)
    assert readers <= RULE_FILES, f"a stage's effort read outside effort_rule.py: {sorted(readers - RULE_FILES)}"
    assert builders <= FIELD_FILES, ("a provider effort field built outside effort_rule.py: "
                                     f"{sorted(builders - FIELD_FILES)}")
