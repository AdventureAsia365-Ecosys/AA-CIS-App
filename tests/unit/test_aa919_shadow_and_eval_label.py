"""AA-919 WM-21 — the targeted shadow run on fakes, and the eval label of shared.llm_call_log (WM-19 / WM-21 cost).

No DB, no model. Invented text only (public repo)."""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import patch

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import shadow
from shared.llm_client import call_log

TENANT_A = "0a919a00-0000-4000-8000-00000000000a"
TENANT_B = "0a919b00-0000-4000-8000-00000000000b"
BRANDS = {TENANT_A: "Zzfake Agency", TENANT_B: "Zzother Pretend Tours"}


# ── the eval label ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_relabel_without_an_eval_run_changes_nothing():
    assert call_log.eval_label() is None
    stage, tenant, qs, piece, req = call_log._relabel("t9_write", TENANT_A, {"x": 1}, "p", "r")
    assert (stage, tenant, qs, piece, req) == ("t9_write", TENANT_A, {"x": 1}, "p", "r")


def test_relabel_inside_an_eval_run_moves_stage_and_tenant_out():
    with call_log.bind_eval_stage(cfg.EVAL_STAGE, "run-1"):
        stage, tenant, qs, piece, req = call_log._relabel("t9_write", TENANT_A, {"x": 1}, "p", "r")
        args = call_log._insert_args({"stage": "s1_judge", "role": "judge", "model": "m", "tenant_id": TENANT_B,
                                      "quality_signal": {"passed": True}})
    assert (stage, tenant, piece, req) == (cfg.EVAL_STAGE, None, None, None)
    assert qs == {"x": 1, "real_stage": "t9_write", "eval_run": "run-1", "eval_tenant": TENANT_A}
    assert args[0] is None and args[1] == cfg.EVAL_STAGE
    assert json.loads(args[7]) == {"passed": True, "real_stage": "s1_judge", "eval_run": "run-1",
                                   "eval_tenant": TENANT_B}
    assert call_log.eval_label() is None


def test_record_call_writes_the_relabelled_row():
    sent = []

    class Conn:
        async def execute(self, sql, *args):
            sent.append(args)

        async def close(self):
            return None

    async def connect(*a, **k):
        return Conn()

    async def go():
        with call_log.bind_eval_stage(cfg.SHADOW_STAGE, "run-2"):
            await call_log.record_call(stage="t2_generate", role="writer", model="m", tokens_in=1, tokens_out=1,
                                       cost_usd=0.01, quality_signal={}, tenant_id=TENANT_A)
    with patch.object(call_log.asyncpg, "connect", connect):
        asyncio.run(go())
    assert sent[0][0] is None and sent[0][1] == cfg.SHADOW_STAGE
    assert json.loads(sent[0][7])["real_stage"] == "t2_generate"


def test_bind_eval_stage_refuses_an_empty_label():
    with pytest.raises(ValueError):
        with call_log.bind_eval_stage("", "run"):
            pass


def test_gateway_starts_no_ab_shadow_inside_an_eval_run():
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest, LLMResponse
    from shared.llm_client.role_config import StageConfig

    cfgs = StageConfig("s1_judge", "judge", "openai", "gpt-5.6-luna", "acc3", shadow_model_id="haiku",
                       shadow_sample_pct=100)
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    resp = LLMResponse(content="{}", model_used="gpt-5.6-luna", provider="p")
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=cfgs), \
            patch.object(client, "_call_key_logged", return_value=resp), \
            patch.object(client, "_start_shadow") as start:
        client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"))
        assert start.call_count == 1
        with call_log.bind_eval_stage(cfg.EVAL_STAGE, "run-3"):
            client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"))
        assert start.call_count == 1


def test_flag_fix_lessons_are_never_written_inside_an_eval_run():
    from services.content_generation import flag_fix_node
    with patch.object(flag_fix_node, "pipeline_lessons_enabled", return_value=True), \
            patch.object(flag_fix_node, "write_lessons_log") as w, \
            call_log.bind_eval_stage(cfg.EVAL_STAGE, "run-4"):
        flag_fix_node._write_lessons_safe([{"lesson": "x"}], {"tour": {}})
    w.assert_not_called()


# ── WM-21: sizes of a run ──────────────────────────────────────────────────────────────────────────────────────────

def _release(**kw):
    return {"release_id": "rel-1", "writer": "t9_social", "scope": "tenant", "tenant_id": TENANT_A, "release_no": 1,
            "content_class": "rules", "rule_ids": ["r-1"], "status": "draft", **kw}


def test_planned_size_follows_the_rule_and_only_lowers():
    conf = shadow.ShadowConfig(release_id="rel-1")
    li = shadow.ShadowConfig(release_id="rel-1", channel="linkedin")
    assert shadow.planned_size(_release(), li) == (20, None)
    # no channel: the release applies to every social channel -> Facebook 50 + the others 20
    assert shadow.planned_size(_release(), conf) == (70, None)
    assert shadow.planned_quota(_release(), conf, 70) == {"facebook": 50, "other": 20}
    assert shadow.planned_quota(_release(), conf, 5) == {"facebook": 5, "other": 0}
    assert shadow.planned_quota(_release(), li, 20) == {"all": 20}
    assert shadow.planned_size(_release(), shadow.ShadowConfig(release_id="rel-1", channel="facebook")) == (50, None)
    assert shadow.planned_size(_release(writer="t2"), conf) == (50, None)
    assert shadow.planned_size(_release(), shadow.ShadowConfig(release_id="rel-1", size=5)) == (5, "size_lowered")
    assert shadow.planned_size(_release(), shadow.ShadowConfig(release_id="rel-1", channel="linkedin", size=30)) == (
        20, None)
    assert shadow.planned_size(_release(content_class="repair_hint_only"), conf) == (10, None)
    assert shadow.planned_size(_release(content_class="lexicon_only"), conf) == (0, None)
    assert shadow.planned_size(_release(content_class="empty"),
                               shadow.ShadowConfig(release_id="rel-1", force_size=3)) == (3, "force_size")


def test_shadow_config_caps():
    with pytest.raises(shadow.ShadowRefused):
        shadow.ShadowConfig(release_id="r", cap_usd=7.5).validate()
    with pytest.raises(shadow.ShadowRefused):
        shadow.ShadowConfig(release_id="r", size=71).validate()


# ── WM-21: the run ─────────────────────────────────────────────────────────────────────────────────────────────────

class FakeShadowStore:
    def __init__(self, release, spend_per_write=0.0):
        self.release = release
        self.rows: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.spend = 0.0
        self.spend_per_write = spend_per_write

    async def get_release(self, rid):
        return self.release

    async def active_parts(self, writer, tenant_id):
        return {"platform": {"release_id": "p0", "release_no": 0, "content_class": "empty", "rule_ids": [],
                             "scope": "platform"}, "tenant": None}

    async def fingerprint(self, writer):
        return {}

    async def month_spend(self):
        return 0.0

    async def run_spend(self, run_id, since):
        return self.spend

    async def finish_run(self, run_id, row):
        self.runs[run_id].update(row)

    async def rules(self, ids):
        return [{"rule_id": "r-1", "rule_text": "Do not open with a rhetorical question", "gate_class": None}]

    async def create_run(self, row):
        rid = str(uuid.uuid4())
        self.runs[rid] = dict(row)
        return rid

    async def insert_shadow(self, row):
        self.rows.append(dict(row))


def _inputs(n_own, n_probe=0, bucket="all"):
    def one(i, tenant, probe):
        return {"input_source": "content_piece", "input_id": str(uuid.UUID(int=i)), "tenant_id": tenant,
                "channel": "linkedin", "probe": probe, "bucket": "probe" if probe else bucket,
                "input": {"writer_input": {"channel": "linkedin", "seed": f"Invented seed {i}.",
                                           "brand_pack": {"brand_name": BRANDS[tenant]}}, "tenant_id": tenant}}
    return [one(i, TENANT_A, False) for i in range(n_own)] + [one(100 + i, TENANT_B, True) for i in range(n_probe)]


def _run(store, conf, inputs, *, text=lambda inp, view: "A clean invented post."):
    views = []

    async def writer(w, inp, *, view, shared_inputs):
        views.append((inp.get("tenant_id"), view))
        store.spend += store.spend_per_write
        assert call_log.eval_label()[0] == cfg.SHADOW_STAGE
        return {"content_text": text(inp, view)}

    async def select(pool, release, *, n, channel, gates, quota=None):
        return inputs

    async def packs(pool, tids):
        return {TENANT_A: {"brand_name": "Zzfake Agency"}, TENANT_B: {"brand_name": "Zzother Pretend Tours"}}

    async def flush():
        return None
    out = asyncio.run(shadow.run_shadow(None, conf, writer_fn=writer, flush=flush, select=select, packs_fn=packs,
                                        store=store))
    return out, views


LI = {"release_id": "rel-1", "channel": "linkedin"}


def test_shadow_pass_writes_one_row_per_input_with_the_candidate_view():
    store = FakeShadowStore(_release())
    out, views = _run(store, shadow.ShadowConfig(**LI), _inputs(20, 2))
    assert out["verdict"] == "pass" and out["counts"]["written"] == 22
    assert len(store.rows) == 22 and all(r["verdict"] == "pass" and r["release_id"] == "rel-1" for r in store.rows)
    probe_rows = [r for r in store.rows if r["probe"]]
    assert len(probe_rows) == 2 and {r["tenant_id"] for r in probe_rows} == {TENANT_B}   # the INPUT's owner
    # the probe input of tenant B is written WITH tenant A's candidate release (that is the leak test)
    assert all(v["tenant"]["release_id"] == "rel-1" for _, v in views)
    assert all(r["scope"] == "tenant" and r["channel"] == "linkedin" for r in store.rows)


def test_shadow_stops_at_the_first_leak():
    store = FakeShadowStore(_release())

    def text(inp, view):
        return "Book with Zzfake Agency." if inp["tenant_id"] == TENANT_B else "A clean invented post."
    out, _ = _run(store, shadow.ShadowConfig(**LI), _inputs(3, 2), text=text)
    assert (out["verdict"], out["stop_reason"]) == ("fail", "hard_stop")
    assert store.rows[-1]["isolation"]["leak"] and store.rows[-1]["probe"]


def test_shadow_finds_the_rule_text_as_a_qa_trace():
    store = FakeShadowStore(_release())
    out, _ = _run(store, shadow.ShadowConfig(**LI), _inputs(5),
                  text=lambda i, v: "Do not open with a rhetorical question. Then walk.")
    assert out["verdict"] == "fail" and store.rows[0]["qa_artefacts"] == ["rule:r-1"]
    assert len(store.rows) == 1


def test_shadow_with_fewer_real_inputs_is_informative():
    store = FakeShadowStore(_release())
    out, _ = _run(store, shadow.ShadowConfig(**LI), _inputs(4))
    assert out["verdict"] == "informative"


def test_shadow_with_a_lowered_size_is_never_a_pass():
    store = FakeShadowStore(_release())
    out, _ = _run(store, shadow.ShadowConfig(**LI, size=1), _inputs(1))
    assert out["verdict"] == "informative" and store.runs[out["run_id"]]["details"]["size_note"] == "size_lowered"


def test_t9_social_with_no_channel_passes_only_with_facebooks_fifty():
    # 70 clean inputs, but all "other": the Facebook bucket is short -> informative, not an attachable pass
    store = FakeShadowStore(_release())
    out, _ = _run(store, shadow.ShadowConfig(release_id="rel-1"), _inputs(70, bucket="other"))
    d = store.runs[out["run_id"]]["details"]
    assert out["verdict"] == "informative" and d["quota"] == {"facebook": 50, "other": 20}
    assert d["found_by_bucket"] == {"facebook": 0, "other": 70}
    store2 = FakeShadowStore(_release())
    both = _inputs(50, bucket="facebook") + [{**x, "input_id": str(uuid.UUID(int=900 + i))}
                                             for i, x in enumerate(_inputs(20, bucket="other"))]
    out2, _ = _run(store2, shadow.ShadowConfig(release_id="rel-1"), both)
    assert out2["verdict"] == "pass"


def test_shadow_cap_is_enforced():
    store = FakeShadowStore(_release(), spend_per_write=0.4)
    out, _ = _run(store, shadow.ShadowConfig(**LI, cap_usd=1.0), _inputs(20))
    # the pre-call reservation is an estimate ($0.03 a T9 social input); an input that costs more is caught right
    # after it by the logged spend: the run stops, at most one input past the cap
    assert (out["verdict"], out["stop_reason"]) == ("invalid", "budget_cap")
    assert store.spend <= 1.0 + 0.4 and len(store.rows) == 3


def test_shadow_refuses_an_empty_release_without_force_and_an_active_one():
    with pytest.raises(shadow.ShadowRefused):
        _run(FakeShadowStore(_release(content_class="empty")), shadow.ShadowConfig(release_id="rel-1"), _inputs(1))
    with pytest.raises(shadow.ShadowRefused):
        _run(FakeShadowStore(_release(status="active")), shadow.ShadowConfig(release_id="rel-1"), _inputs(1))
    out, _ = _run(FakeShadowStore(_release(content_class="empty")),
                  shadow.ShadowConfig(release_id="rel-1", force_size=2), _inputs(2))
    assert out["verdict"] == "informative"          # an operator check, never attachable evidence


def test_shadow_writer_error_is_counted():
    store = FakeShadowStore(_release())

    def text(inp, view):
        raise RuntimeError("down")
    out, _ = _run(store, shadow.ShadowConfig(release_id="rel-1", size=3), _inputs(3), text=text)
    assert out["verdict"] == "invalid" and all(r["verdict"] == "error" and r["output"] is None for r in store.rows)


def test_target_gates_map_rule_classes_to_gates():
    gates = shadow.target_gates({"fact.inclusion"})
    assert gates and all(isinstance(g, str) for g in gates)
    assert shadow.target_gates(set()) == []


def test_t9_writer_input_shape_builds_the_write_kwargs():
    from services.writing_memory import eval_writers as ew
    inp = {"writer_input": {"channel": "linkedin", "goal": "awareness", "seed": "Invented seed.",
                            "brand_pack": {"brand_name": "Zzfake"}, "angle": {"name": "a"}, "cta": "Ask us.",
                            "route_moments": [{"atom_id": "x", "seed": "Moment."}], "facts": "F",
                            "lead_focus": "lead"},
           "atom_id": "atom-1", "tenant_id": TENANT_A}
    ctx = ew._t9_context(inp)
    assert ctx["atom_text"] == "Invented seed." and ctx["route_segments"] == [("x", "Moment.")]
    assert ew.own_pack("t9_social", inp) == {"brand_name": "Zzfake"}
    assert "Invented seed." in ew.source_text("t9_social", inp) and "Moment." in ew.source_text("t9_social", inp)
    with patch("services.acp_angle_gate.goals.get_goal", return_value={"key": "awareness"}), \
            patch("services.acp_angle_gate.channel_style.get_channel_style", return_value={"channel": "linkedin"}):
        kw = ew.t9_kwargs(ctx, tenant_id=TENANT_A, view={"writer": "t9_social"})
    assert kw["memory"] == {"writer": "t9_social"} and kw["lead_focus"] == "lead"
    assert kw["angle_gate_request_id"] is None
    assert kw["content_seed"] == "Invented seed." and kw["route_segments"] == [("x", "Moment.")]
    with pytest.raises(ew.WriterFailed):
        ew._t9_context({})


def test_t9_judge_source_holds_the_scope_and_the_angle_brief():
    """S255 (AA-919): the golden labels were made on the full writer input; the judge's SOURCE carries the scope line
    and the angle brief the writer was told to follow, for both stored shapes."""
    from services.writing_memory import eval_writers as ew
    angle = {"name": "Zz invented angle", "formula_fit": "Problem then proof.", "best_final_style": "Calm; the narrow "
             "invented peninsula.", "answers": ["not shown"], "option_id": "x"}
    golden = {"context": {"channel": "linkedin", "atom_text": "Invented seed.", "scope": "SCOPE: day 4 only.",
                          "chosen": angle}}
    live = {"writer_input": {"channel": "linkedin", "seed": "Invented seed.", "scope": "SCOPE: day 4 only.",
                             "angle": angle}}
    for inp in (golden, live):
        src = ew.source_text("t9_social", inp)
        assert "day 4 only." in src and "Zz invented angle" in src and "narrow invented peninsula" in src
        assert "not shown" not in src and "option_id" not in src
    assert "BRIEF" not in ew.source_text("t9_social", {"context": {"atom_text": "Seed only."}})


def test_t9_judge_prompt_keeps_the_brief_when_the_facts_are_long():
    """S255 review: a T9 blog's facts run to 15-40k chars and the judge reads the first SOURCE_CHARS of the source,
    so the scope, the brief and the brand come before the facts (at the end they were cut for every t9_blog item)."""
    from services.writing_memory import eval_writers as ew
    from services.writing_memory.eval_judge import SOURCE_CHARS, build_user_prompt
    ctx = {"channel": "blog", "atom_text": "Invented seed.", "facts_text": "Zz invented fact. " * 3000,
           "scope": "SCOPE: day 4 only.", "chosen": {"name": "Zz invented angle"},
           "brand_audience": {"brand_name": "Zz Invented Brand"}}
    src = ew.source_text("t9_blog", {"context": ctx})
    assert len(src) > SOURCE_CHARS
    prompt = build_user_prompt(writer="t9_blog", source_text=src, output_text="x")
    assert "day 4 only." in prompt and "Zz invented angle" in prompt and "Zz Invented Brand" in prompt


def test_job_kinds_are_registered_paid_and_background():
    from services.jobs import writing_memory_job as j
    from shared.jobs.registry import get_kind
    for name in (j.R2, j.SHADOW):
        k = get_kind(name)
        assert k.max_attempts == 1 and k.background and k.concurrency == 1 and k.expected_seconds >= 3600
        assert k.deploy_blocking            # a released paid run would start over and pay again
