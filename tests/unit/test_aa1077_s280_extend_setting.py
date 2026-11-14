"""AA-1077 (S280, CIS 418) — the lengthening repair switch is the setting cis.t9.blog_length_extend, not an env var.

- `length_extend.mode(tenant_id)`: a job's trial flag first, then the settings reader (env override T9_LENGTH_EXTEND,
  then the tenant's value, then the platform value, then the default off); anything but 'on' is off.
- the plans (`plan_for`, `plan_after_repair`) take the piece's resolved `switch`, or resolve it for `tenant_id`.
- the loop resolves the switch ONCE per piece for the piece's tenant, so a tenant value runs the trial on one audit
  tenant through the normal studio flow, and the blog's length trace records it.

Uses the real settings reader (its snapshot set by hand, no DB). Invented places only; the model is always mocked.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import length_extend as le
from services.acp_content_writing import service
from services.quality import registry as qr
from services.quality import settings_keys
from shared import settings
from shared.settings.reader import _Row
from tests.unit.test_aa1077_length_extend import (  # noqa: F401 — _loop_stubs is a fixture used by name
    GOOD, SOURCE, TEXT, _call, _ctx, _loop_stubs, _outcome, _short_ledger)

KEY = settings_keys.T9_BLOG_LENGTH_EXTEND
TRIAL = "00000000-0000-4000-8000-0000000000a1"    # invented tenant ids
OTHER = "00000000-0000-4000-8000-0000000000b2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No env override and an empty snapshot (= every key at its compiled default) unless a test sets one."""
    monkeypatch.delenv(le.EXTEND_ENV, raising=False)
    monkeypatch.setattr(settings.READER, "_snapshot", {})


def _values(monkeypatch, *, platform=None, **tenants):
    snap = {}
    if platform is not None:
        snap[(KEY, None)] = _Row(value=platform, version=1)
    for tenant, value in tenants.items():
        snap[(KEY, {"trial": TRIAL, "other": OTHER}[tenant])] = _Row(value=value, version=1)
    monkeypatch.setattr(settings.READER, "_snapshot", snap)


def test_the_key_is_the_compiled_definition():
    d = settings.definition(KEY)
    assert le.EXTEND_SETTING == KEY == "cis.t9.blog_length_extend"
    assert d.default_value == "off" and list(d.allowed_values) == ["off", "on"] and d.tenant_scoped
    assert d.env_override == le.EXTEND_ENV and d.evidence_required and d.approval == "maker_checker"


def test_the_default_is_off_for_every_tenant():
    assert le.mode() == le.mode(TRIAL) == le.mode(OTHER) == "off"
    assert le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE, tenant_id=TRIAL) is None


def test_a_tenant_value_turns_it_on_for_that_tenant_only(monkeypatch):
    _values(monkeypatch, trial="on")
    assert (le.mode(TRIAL), le.mode(OTHER), le.mode()) == ("on", "off", "off")
    assert le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE, tenant_id=TRIAL) is not None
    assert le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE, tenant_id=OTHER) is None
    assert le.plan_after_repair(TEXT, before=TEXT, channel="blog", source=SOURCE, tenant_id=TRIAL)[1] == "expand"
    assert le.plan_after_repair(TEXT, before=TEXT, channel="blog", source=SOURCE, tenant_id=OTHER) == (None, "off")


def test_the_platform_value_is_everyones_and_a_tenant_value_wins_over_it(monkeypatch):
    _values(monkeypatch, platform="on", other="off")
    assert (le.mode(TRIAL), le.mode(OTHER), le.mode()) == ("on", "off", "on")


@pytest.mark.parametrize("env,expected", [("on", "on"), ("off", "off"), ("yes", "on")])
def test_the_env_var_is_the_emergency_override(monkeypatch, env, expected):
    """A valid env value wins over the tenant value; an invalid one ('yes') is ignored by the reader, never served,
    so the tenant value stands."""
    _values(monkeypatch, trial="on")
    monkeypatch.setenv(le.EXTEND_ENV, env)
    assert le.mode(TRIAL) == expected
    assert le.mode(OTHER) == ("on" if env == "on" else "off")


@pytest.mark.parametrize("flag,expected", [("on", "on"), ("off", "off")])
def test_a_job_trial_flag_comes_first(monkeypatch, flag, expected):
    _values(monkeypatch, trial="off" if flag == "on" else "on")
    monkeypatch.setattr(le.trial_flags, "active", lambda: {le.EXTEND_ENV: flag})
    assert le.mode(TRIAL) == expected


def test_a_resolved_switch_decides_the_plans_without_a_settings_read(monkeypatch):
    _values(monkeypatch, trial="on")
    real = settings.READER.get

    def no_switch_read(key, tenant_id=None):
        assert key != KEY, "the plan read the switch although the caller resolved it"
        return real(key, tenant_id)

    with patch.object(settings.READER, "get", side_effect=no_switch_read):
        assert le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE, switch="off") is None
        assert le.plan_for(TEXT, _short_ledger(), channel="blog", source=SOURCE, switch="on") is not None
        assert le.plan_after_repair(TEXT, before=TEXT, channel="blog", source=SOURCE, switch="off") == (None, "off")


# ---------------------------------------------------------------- the loop

REQUEST_ID, PIECE_ID = uuid.uuid4(), uuid.uuid4()
PASSING = [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": [], "blocking": True}]


async def _run(ctx, *, extend=None, modes=None):
    real_mode = le.mode

    def spy(tenant_id=None):
        modes.append(tenant_id)
        return real_mode(tenant_id)

    with patch.object(service, "write_content", return_value=(TEXT, 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=(TEXT, 0.05, {}, None)) as rewrite, \
            patch.object(service, "extend_sections", side_effect=extend or _call(GOOD)) as ext, \
            patch.object(service, "run_quality_gates",
                         side_effect=[_outcome(_short_ledger()), _outcome(PASSING), _outcome(PASSING)]), \
            patch.object(service, "_recheck_length", side_effect=lambda t, led, st, hr, **kw: (led, st, hr, {})), \
            patch.object(service, "_tenant_missing_brand_rules", new=AsyncMock(return_value=False)), \
            patch.object(service, "compute_embedding", return_value=None), \
            patch.object(service.length_extend, "mode", side_effect=spy), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, ctx, pool=MagicMock())
    return rewrite, ext, fin


@pytest.mark.asyncio
@pytest.mark.usefixtures("_loop_stubs")
async def test_the_trial_tenants_short_blog_gets_the_lengthening_repair(monkeypatch):
    _values(monkeypatch, trial="on")
    ctx, modes = {**_ctx(), "tenant_id": TRIAL}, []
    rewrite, ext, fin = await _run(ctx, modes=modes)
    rewrite.assert_not_called()
    ext.assert_called_once()
    assert modes == [TRIAL]                                   # resolved once per piece, for the piece's tenant
    assert any(e.get("repair_mode") == le.EXTEND_MODE for e in fin.call_args.kwargs["repair_log"])
    assert ctx["length_trace"]["extend"] == "on"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_loop_stubs")
async def test_another_tenants_short_blog_keeps_mains_rewrite(monkeypatch):
    _values(monkeypatch, trial="on")
    ctx, modes = {**_ctx(), "tenant_id": OTHER}, []
    rewrite, ext, fin = await _run(ctx, modes=modes)
    rewrite.assert_called_once()
    ext.assert_not_called()
    assert modes == [OTHER]
    assert not any("length_net" in e or e.get(le.EXTEND_MODE) for e in fin.call_args.kwargs["repair_log"])
    assert ctx["length_trace"]["extend"] == "off"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_loop_stubs")
async def test_a_setting_change_mid_piece_does_not_split_its_rounds(monkeypatch):
    """The switch is read once before attempt 1: flipping the tenant value off after it keeps the piece's rounds on."""
    _values(monkeypatch, trial="on")
    ctx, modes = {**_ctx(), "tenant_id": TRIAL}, []

    def flip_then_call(**kw):
        _values(monkeypatch, trial="off")
        return _call(GOOD)(**kw)

    real_plan = le.plan_after_repair

    def plan_after(*a, **kw):
        _values(monkeypatch, trial="off")          # flipped before the post-repair plan
        return real_plan(*a, **kw)

    with patch.object(service.length_extend, "plan_after_repair", side_effect=plan_after) as pa:
        rewrite, ext, fin = await _run(ctx, modes=modes, extend=flip_then_call)
    assert pa.call_args.kwargs["switch"] == "on"
    ext.assert_called_once()
    assert modes == [TRIAL] and ctx["length_trace"]["extend"] == "on"


# ---------------------------------------------------------------- the $0 extend-plan replay (scripts/aa1077)

def _row(pid, status, text, ledger, **wi):
    return {"piece_id": pid, "tenant": "audit-x", "status": status, "held_reason": None, "content_text": text,
            "gate_ledger": ledger, "repair_log": [], "writer_input": {"seed": SOURCE, **wi}}


def test_the_extend_plan_replay_reads_what_the_repair_could_reach():
    """The replay plans with the switch ON whatever the setting says (it measures the repair before it is turned
    on), and reads the source the loop's plan reads. Settings stay at their defaults (off) here."""
    from scripts.aa1077 import extend_plan_replay as rp
    from services.acp_content_writing import quality_gates as qg
    f4 = _short_ledger()[0]
    short = rp.replay_piece(_row("aaaaaaaa-1", "held", TEXT, [f4], length_trace={"held": False, "extend": "off"}),
                            source_of=lambda wi, row: SOURCE)
    assert short["side"] == "short" and short["length_only"] and short["plan"] == "expand"
    assert short["facts_offered"] >= 1 and short["deficit_chars"] > 0 and short["switch_recorded"] == "off"
    dry = rp.replay_piece(_row("bbbbbbbb-2", "held", TEXT, [f4]), source_of=lambda wi, row: "")
    assert dry["plan"] == "no_untold_facts"
    long_text = TEXT + "\n\n" + ("Lake Orrin lies still below the ridge in the long evening light. " * 90)
    ok = rp.replay_piece(_row("cccccccc-3", "approved", long_text,
                              [qg.gate_extreme_length(long_text, "blog")], length_trace={"held": False}),
                         source_of=lambda wi, row: SOURCE)
    assert ok["side"] == "in" and ok["plan"] == "at_or_over_floor" and not ok["length_only"]
    noseed = rp.replay_piece({**_row("dddddddd-4", "held", TEXT, [f4]), "writer_input": {}})
    assert noseed["plan"] == "no_seed"
    s = rp.summarize([short, dry, ok, noseed])
    assert s["short"] == 3 and s["reachable"] == ["aaaaaaaa"] == s["reachable_length_only"]
    assert s["no_material"] == ["bbbbbbbb"] and s["plan_reasons"]["no_seed"] == 1
    assert s["floor_chars"] == 4700 and s["recheck_held"] == []
