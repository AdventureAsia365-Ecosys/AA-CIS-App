"""AA-1113 (root ADR 0009 decision 10; CIS 448): the free-text stage list is the registry column
``shared.llm_role_config.free_text``, not a code literal, and the ledger shows what the model got.

- A stage marked free_text in its row is scrubbed whatever its name (a planted e-mail and phone never reach the fake
  model); an unmarked catalogue stage is sent as is; a tenant stage (``t<n>_``) is scrubbed even when its row says false
  (the fail-safe floor).
- role_config reads the column (missing before 448 = False, the read retried without it), remembers the last read,
  and a stage whose row could not be read on a cold cache is scrubbed (fail safe).
- The gateway's own ledger row of a scrubbed call carries ``quality_signal.pii_scrub.scrubbed_hash``, the sha256 of the
  text the model would get: the $0 live probe recomputes it (a refused model key, nothing billed).
No database, no AWS, no vendor; invented names and values only.
"""
import json

import asyncpg
import pytest

from shared.llm_client import call_log, free_text_scrub, role_config
from shared.llm_client.free_text_scrub import is_free_text_stage, scrub_request, scrubbed_hash
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1113_free_text_scrub import (PLANTED_EMAIL, PLANTED_MOBILE, PLANTED_PHONE, PROMPT,  # noqa: F401
                                                    ledger, world)

RAW_VALUES = (PLANTED_EMAIL, PLANTED_PHONE, PLANTED_MOBILE, "912 345 678", "654 321")


def _cfg(stage, *, free_text, model="gpt-6-luna"):
    return StageConfig(stage=stage, role="writer", provider="claude", model_id=model, account_route="acc3",
                       free_text=free_text)


def _route(monkeypatch, cfg):
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)


# ── the rule ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("stage", "marked", "expected"), [
    ("tp_plan_chat", True, True),          # the registry adds a stage that is not a tenant stage
    ("crm_zz_draft", True, True),
    ("tp_plan_chat", False, False),
    ("s1_generate", False, False),
    ("t9_write", False, True),             # the floor: a row never takes a tenant stage out
    ("t2_judge", True, True),
    ("", True, False),                     # no stage name: nothing to mark
    (None, True, False),
])
def test_the_registry_mark_and_the_floor(stage, marked, expected):
    assert is_free_text_stage(stage, _cfg(stage or "x", free_text=marked)) is expected


def test_a_stage_with_no_row_object_follows_the_floor():
    assert is_free_text_stage("t9_write") is True
    assert is_free_text_stage("tp_plan_chat") is False


# ── role_config reads the column ────────────────────────────────────────────────────────────────────────────────────

def _row(**cols):
    base = {"stage": "tp_plan_chat", "role": "writer", "provider": "claude", "model_id": "m", "account_route": None,
            "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0}
    base.update(cols)
    return base


def test_the_row_carries_the_mark_and_a_missing_column_reads_false():
    assert role_config._row_to_config(_row(free_text=True)).free_text is True
    assert role_config._row_to_config(_row(free_text=False)).free_text is False
    assert role_config._row_to_config(_row()).free_text is False


def test_the_select_asks_for_the_column_and_drops_it_when_missing(monkeypatch):
    monkeypatch.setattr(role_config, "_free_text_column_missing_at", 0.0)
    for name in ("_scope_column_missing_at", "_deadline_column_missing_at", "_switch_column_missing_at",
                 "_pii_column_missing_at"):
        monkeypatch.setattr(role_config, name, 0.0)
    assert role_config._scope_select().endswith(", pii_class, free_text")
    seen: list = []

    async def fetch(sql):
        seen.append(sql)
        if "free_text" in sql:
            raise asyncpg.exceptions.UndefinedColumnError('column "free_text" does not exist')
        return [_row(pii_class="content")]
    import asyncio
    rows = asyncio.run(role_config._fetch_scoped(fetch, lambda cols: f"SELECT x{cols}"))
    assert rows and "free_text" in seen[0] and "free_text" not in seen[-1] and seen[-1].endswith(", pii_class")
    assert role_config._scope_select().endswith(", pii_class")          # remembered for the retry window


def test_a_cold_failed_read_scrubs_until_the_stage_was_read(monkeypatch):
    monkeypatch.setattr(role_config, "_free_text_known", {})
    monkeypatch.setattr(role_config, "_pii_known", {"tp_plan_chat": "content"})
    assert role_config._read_failed_default("tp_plan_chat").free_text is True      # never read here: fail safe
    role_config._remember_class("tp_plan_chat", _cfg("tp_plan_chat", free_text=False))
    assert role_config._read_failed_default("tp_plan_chat").free_text is False     # the last real read wins
    role_config._remember_class("tp_plan_chat", _cfg("tp_plan_chat", free_text=True))
    assert role_config._read_failed_default("tp_plan_chat").free_text is True


def test_invalidate_keeps_the_known_marks(monkeypatch):
    monkeypatch.setattr(role_config, "_free_text_known", {"tp_plan_chat": False})
    role_config.invalidate()
    assert role_config._free_text_known == {"tp_plan_chat": False}


# ── through the gateway ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_marked_stage_reaches_the_model_scrubbed(world, monkeypatch, ledger):  # noqa: F811
    _route(monkeypatch, _cfg("tp_plan_chat", free_text=True))
    resp = world.generate(stage="tp_plan_chat", system_prompt="Plan the trip.", user_prompt=PROMPT)
    assert resp.content == "from acc3" and len(world.sent) == 1
    assert not any(v in world.sent[0] for v in RAW_VALUES)
    assert "[email]" in world.sent[0] and world.sent[0].count("[phone]") == 2


def test_an_unmarked_stage_reaches_the_model_unchanged(world, monkeypatch, ledger):  # noqa: F811
    _route(monkeypatch, _cfg("tp_plan_chat", free_text=False))
    world.generate(stage="tp_plan_chat", system_prompt="Plan the trip.", user_prompt=PROMPT)
    assert PLANTED_EMAIL in world.sent[0] and "[email]" not in world.sent[0]


def test_the_ledger_row_of_a_refused_model_shows_the_scrubbed_hash(world, monkeypatch, ledger):  # noqa: F811
    # The $0 live probe: a free-text call on a model key the catalog does not hold is refused before any provider
    # attempt, after the scrub; its 0 USD row carries the receipt, and the hash is the scrubbed text's.
    _route(monkeypatch, _cfg("t9_write", free_text=True))
    req = LLMRequest(system_prompt="Probe: write one line.", user_prompt=PROMPT, stage="t9_write",
                     model_tier="zz-aa1113-no-such-model", max_tokens=16)
    with pytest.raises(RuntimeError, match="zz-aa1113-no-such-model"):
        world.client.generate(req)
    assert world.calls == [] and world.sent == []                            # nothing was sent anywhere
    call_log.wait_for_pending_writes()
    rows = [r for r in ledger if r["stage"] == "t9_write"]
    assert rows, ledger
    receipt = rows[-1]["quality_signal"]["pii_scrub"]
    assert receipt["status"] == "scrubbed" and receipt["redacted"] == {"[email]": 1, "[phone]": 2}
    assert receipt["scrubbed_hash"] == scrubbed_hash(scrub_request(req)[0])
    assert float(rows[-1]["cost_usd"] or 0) == 0.0
    flat = json.dumps(rows, default=str)
    assert not any(v in flat for v in RAW_VALUES)


def test_a_failed_attempt_row_carries_the_receipt_too(world, monkeypatch, ledger):  # noqa: F811
    _route(monkeypatch, _cfg("tp_plan_chat", free_text=True))
    world.fail["acc3"] = RuntimeError("fake acc3 outage")
    world.reset()
    with pytest.raises(RuntimeError):
        world.generate(stage="tp_plan_chat", system_prompt="Plan the trip.", user_prompt=PROMPT)
    call_log.wait_for_pending_writes()
    attempts = [r for r in ledger if r["quality_signal"].get("gateway_attempt")]
    assert attempts and all(r["quality_signal"]["pii_scrub"]["status"] == "scrubbed" for r in attempts)
    assert not any(v in json.dumps(ledger, default=str) for v in RAW_VALUES)


def test_the_hash_names_what_was_sent_never_the_raw_text():
    req = LLMRequest(system_prompt="Plan the trip.", user_prompt=PROMPT, stage="t9_write")
    scrubbed, _ = scrub_request(req)
    assert scrubbed_hash(scrubbed) != scrubbed_hash(req)
    assert scrubbed_hash(scrubbed) == scrubbed_hash(scrubbed.model_copy())        # stable for the same text
    assert len(scrubbed_hash(scrubbed)) == 64 and free_text_scrub.VERSION == "aa1113-2"


# ── change control parity (ADR 0020): the new field leaves every fallback route as it was ──────────────────────────

PRE_448_FIELDS = ("stage", "role", "provider", "model_id", "account_route", "fallback_model_ids", "shadow_model_id",
                  "shadow_sample_pct", "allowed_scopes", "route_deadline_s", "enabled", "disabled_reason",
                  "degrade_mode", "pii_class")


def test_the_safe_defaults_gain_only_an_unset_mark():
    # The route_defaults lock moved because StageConfig gained free_text (default False); every route field of every
    # SAFE_DEFAULTS row is what it was, and no fallback row marks a stage (the t<n>_ floor scrubs the tenant stages).
    from dataclasses import asdict, fields
    assert tuple(f.name for f in fields(StageConfig)) == PRE_448_FIELDS + ("free_text",)
    for stage, cfg in role_config.SAFE_DEFAULTS.items():
        row = asdict(cfg)
        assert row.pop("free_text") is False, stage
        assert tuple(row) == PRE_448_FIELDS


# ── the $0 live probe script ────────────────────────────────────────────────────────────────────────────────────────

def test_the_live_probe_passes_on_a_scrubbing_gateway(world, monkeypatch, ledger, capsys):  # noqa: F811
    from scripts.llm_ops import aa1113_scrub_probe as probe
    monkeypatch.setattr("shared.llm_client.client.LLMClient", lambda: world.client)
    _route(monkeypatch, _cfg("t9_write", free_text=True))

    async def row(trace):
        hits = [r for r in ledger if (r.get("quality_signal") or {}).get("route_trace") == trace]
        if not hits:
            return None
        r = hits[-1]
        return {"stage": r["stage"], "model": r["model"], "status": r["status"], "error_class": r["error_class"],
                "cost_usd": r["cost_usd"], "qs": json.dumps(r["quality_signal"])}
    monkeypatch.setattr(probe, "_row", row)
    assert probe.main([]) == 0
    out = capsys.readouterr().out
    report = json.loads(out.split("--- AA1113 SCRUB PROBE ---")[1].split("--- END ---")[0])
    assert report["ok"] and all(report["checks"].values())
    assert report["pii_scrub"]["scrubbed_hash"] == report["expected_hash"]
    assert world.calls == [] and world.sent == []


def test_the_live_probe_fails_when_the_scrub_is_skipped(world, monkeypatch, ledger, capsys):  # noqa: F811
    from scripts.llm_ops import aa1113_scrub_probe as probe
    monkeypatch.setattr("shared.llm_client.client.LLMClient", lambda: world.client)
    _route(monkeypatch, _cfg("t9_write", free_text=True))
    monkeypatch.setattr(free_text_scrub, "is_free_text_stage", lambda stage, cfg=None: False)

    async def row(trace):
        hits = [r for r in ledger if (r.get("quality_signal") or {}).get("route_trace") == trace]
        return {"stage": "t9_write", "model": probe.PROBE_MODEL, "status": "refused", "error_class": "other",
                "cost_usd": 0, "qs": json.dumps(hits[-1]["quality_signal"])} if hits else None
    monkeypatch.setattr(probe, "_row", row)
    assert probe.main([]) == 1
