"""AA-790 round 8 (S255) — the T8 brief contract behind `T8_ANGLE_BRIEF_MODE`, and the A/B harness's T8 regen step.

Off (the default) must keep the live T8 call byte for byte: the same system prompt (pinned by sha), the same user
prompt, the same stage, model route and log row. Round 8 swaps only the best_final_style bullet and adds one
brand-first paragraph to the system prompt, plus a $0 brief check that feeds the existing one-retry path.
No DB, no model call: the gateway and the call log are faked."""
from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_angle_gate import generate as gen_mod
from services.acp_angle_gate import prompts
from services.acp_angle_gate import service
from services.acp_angle_gate.goals import get_goal
from shared.llm_client.models import LLMResponse

GOAL = get_goal("introduction_awareness")
# sha256 of the T8 SYSTEM_PROMPT on origin/main b665382f (unchanged since #749): mode 'off' must keep these bytes.
ROUND6_SYSTEM_SHA = "df5bd75e54608e21"

GOOD_BRIEF = ("Reader: you ride the valley road on day two and stop for a swim at the falls. "
              "Brand: pace, the seed says the ride is short and on tarmac. "
              "Lead with: the swim, the valley road, the evening in town. "
              "Shape: a short post, two paragraphs, the call to action last.")
OUTLINE_BRIEF = ("Structure the piece around the idea of convergence. Lead with the lake, 70 percent of the "
                 "fresh water, 0.4 percent of the world's, then the caldera at 20,000 to 24,000 years, day 3, "
                 "day 4 and day 14.")


def _angles(brief: str) -> list[dict]:
    return [{"name": f"A{i}", "why_it_works": "w", "formula_fit": "f", "best_final_style": brief, "answers": []}
            for i in range(3)]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- prompt texts

class TestPromptTexts:
    def test_off_prompt_is_the_round6_bytes(self):
        assert _sha(prompts.SYSTEM_PROMPT) == ROUND6_SYSTEM_SHA
        assert prompts.system_prompt("off") is prompts.SYSTEM_PROMPT

    def test_r8_swaps_only_the_brief_bullet_and_adds_the_brand_first_paragraph(self):
        r8 = prompts.SYSTEM_PROMPT_R8
        assert prompts._BRIEF_SPEC_OFF in prompts.SYSTEM_PROMPT  # the replace can never miss silently
        assert prompts._BRIEF_SPEC_OFF not in r8
        assert r8.count(prompts._BRIEF_SPEC_R8) == 1 and r8.count(prompts._BRAND_FIRST_R8) == 1
        back = r8.replace(prompts._BRIEF_SPEC_R8, prompts._BRIEF_SPEC_OFF).replace(prompts._BRAND_FIRST_R8, "")
        assert back == prompts.SYSTEM_PROMPT
        assert r8.index(prompts._BRAND_FIRST_R8) < r8.index(prompts._BRAND_ANCHOR)
        assert prompts.system_prompt("r8") is r8

    def test_r8_brief_names_the_four_parts_in_order(self):
        spec = prompts._BRIEF_SPEC_R8
        positions = [spec.index(f'"{label} ...') for label in prompts.R8_BRIEF_LABELS]
        assert positions == sorted(positions)
        assert f"at most {prompts.R8_BRIEF_TARGET_WORDS} words" in spec

    def test_r8_keeps_every_round6_rule(self):
        for kept in ("never writes or prescribes the first sentence", "never names an audience other than",
                     "the guide unrolling a map", "THIN MOMENTS", "Never invent facts", "PRIOR PIECES",
                     '"recommended_index": 0', "recommended_index must be 0, 1, or 2"):
            assert kept in prompts.SYSTEM_PROMPT_R8

    @pytest.mark.parametrize("phrase", [
        "never lists more than three facts", "never asks for a survey of every stop",
        "never gives the piece a thesis", "no reassurance, practical note, comfort, pace, safety, logistics",
        "leave it out of the brief", "for search demand second", "two different brands given the same seed",
    ])
    def test_r8_rules_are_there(self, phrase):
        assert phrase in prompts.SYSTEM_PROMPT_R8

    def test_r8_additions_are_generic(self):
        added = prompts._BRIEF_SPEC_R8 + prompts._BRAND_FIRST_R8
        assert "—" not in added and "{" not in added
        for name in ("Terra", "Atlas", "Hearth", "Trail Pulse", "WildKind", "Kuang", "Khovsgol", "Khövsgöl",
                     "Bhutan", "Laos", "Mongolia", "Zorig", "Adventure Asia"):
            assert name not in added


class TestBriefMode:
    @pytest.mark.parametrize("env,expected", [
        (None, "off"), ("", "off"), ("r8", "r8"), (" R8 ", "r8"), ("on", "off"), ("true", "off"), ("1", "off"),
        ("off", "off"), ("r7", "off"),
    ])
    def test_env_switch(self, monkeypatch, env, expected):
        if env is None:
            monkeypatch.delenv(prompts.BRIEF_MODE_ENV, raising=False)
        else:
            monkeypatch.setenv(prompts.BRIEF_MODE_ENV, env)
        assert prompts.brief_mode() == expected

    def test_explicit_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(prompts.BRIEF_MODE_ENV, "r8")
        assert prompts.brief_mode("off") == "off"
        monkeypatch.setenv(prompts.BRIEF_MODE_ENV, "off")
        assert prompts.brief_mode("r8") == "r8"
        assert prompts.system_prompt(None) is prompts.SYSTEM_PROMPT


# ---------------------------------------------------------------- the $0 brief check

class TestBriefProblems:
    def test_good_brief_passes(self):
        assert gen_mod.r8_brief_problems(GOOD_BRIEF) == []

    def test_labels_any_case(self):
        assert gen_mod.r8_brief_problems(GOOD_BRIEF.lower()) == []

    def test_missing_parts(self):
        out = gen_mod.r8_brief_problems("Lead with: the swim. Shape: a short post.")
        assert out == ["has no 'Reader:' part", "has no 'Brand:' part"]
        assert len(gen_mod.r8_brief_problems("")) == 4

    def test_word_limit(self):
        long = GOOD_BRIEF + " " + " ".join(["more"] * prompts.R8_BRIEF_MAX_WORDS)
        assert any("words long" in p for p in gen_mod.r8_brief_problems(long))
        edge = "Reader: a. Brand: b. Lead with: c. Shape: d. " + " ".join(["w"] * (prompts.R8_BRIEF_MAX_WORDS - 9))
        assert gen_mod.r8_brief_problems(edge) == []

    def test_fact_outline_is_caught(self):
        out = gen_mod.r8_brief_problems(OUTLINE_BRIEF)
        assert any("figures" in p for p in out)

    @pytest.mark.parametrize("text,count", [
        ("day 3-5", 1), ("1637–1638", 1), ("20,000 to 24,000", 2), ("Day 2 and day 14", 2), ("no digits", 0),
    ])
    def test_figure_count(self, text, count):
        assert len(gen_mod._FIGURE_RE.findall(text)) == count

    def test_five_figures_allowed_six_not(self):
        base = "Reader: r. Brand: b. Lead with: l. Shape: s."
        assert gen_mod.r8_brief_problems(base + " day 1 day 2 day 3 day 4 day 5") == []
        assert any("figures" in p for p in gen_mod.r8_brief_problems(base + " day 1 day 2 day 3 day 4 day 5 day 6"))


class TestContractViolations:
    def test_off_mode_does_not_check_the_brief(self):
        assert gen_mod.angle_contract_violations(_angles(OUTLINE_BRIEF), {}) == []
        assert gen_mod.angle_contract_violations(_angles(OUTLINE_BRIEF), {}, brief_mode="off") == []

    def test_r8_mode_lists_each_angle(self):
        out = gen_mod.angle_contract_violations(_angles(OUTLINE_BRIEF), {}, brief_mode="r8")
        assert out and all("best_final_style" in v for v in out)
        assert {v.split(" ")[1] for v in out} == {"1", "2", "3"}
        assert gen_mod.angle_contract_violations(_angles(GOOD_BRIEF), {}, brief_mode="r8") == []

    def test_r8_keeps_the_round6_checks(self):
        pack = {"brand_name": "Kestrel Trips", "forbidden_words": ["breathtaking"]}
        bad = _angles(GOOD_BRIEF)
        bad[0]["why_it_works"] = "a breathtaking ride"
        out = gen_mod.angle_contract_violations(bad, pack, brief_mode="r8")
        assert any("breathtaking" in v for v in out)


# ---------------------------------------------------------------- the call

def _client(content: dict):
    client = MagicMock()
    client.generate.return_value = LLMResponse(content=json.dumps(content), model_used="sonnet", provider="bedrock",
                                               cost_usd=0.03)
    return client


def _valid(brief: str) -> dict:
    return {"angles": [{k: v for k, v in a.items() if k != "answers"} for a in _angles(brief)],
            "recommended_index": 1, "recommendation_reason": "r"}


class TestGenerateOnce:
    def _run(self, monkeypatch, brief=GOOD_BRIEF, env=None, **kw):
        if env is None:
            monkeypatch.delenv(prompts.BRIEF_MODE_ENV, raising=False)
        else:
            monkeypatch.setenv(prompts.BRIEF_MODE_ENV, env)
        client = _client(_valid(brief))
        log = AsyncMock()
        with patch.object(gen_mod, "LLMClient", return_value=client), patch.object(gen_mod, "record_call", log):
            out = asyncio.run(gen_mod.generate_angles(content_seed="seed", goal=GOAL, brand_audience={}, **kw))
        return out, client, log

    def test_off_request_and_log_row_are_unchanged(self, monkeypatch):
        _, client, log = self._run(monkeypatch)
        req = client.generate.call_args.args[0]
        assert req.system_prompt is prompts.SYSTEM_PROMPT and req.stage == "t8_angle_gen"
        assert req.model_tier is None and req.max_tokens == 2048
        kw = log.await_args.kwargs
        assert kw["stage"] == "t8_angle_gen"
        assert set(kw["quality_signal"]) == {"angles_generated", "recommended_index", "avg_paa_answers_per_angle",
                                             "attempt"}

    def test_env_r8_switches_the_system_prompt_only(self, monkeypatch):
        _, off_client, _ = self._run(monkeypatch)
        _, r8_client, log = self._run(monkeypatch, env="r8")
        off_req, r8_req = off_client.generate.call_args.args[0], r8_client.generate.call_args.args[0]
        assert r8_req.system_prompt is prompts.SYSTEM_PROMPT_R8
        assert r8_req.user_prompt == off_req.user_prompt and r8_req.stage == off_req.stage
        assert log.await_args.kwargs["quality_signal"]["brief_mode"] == "r8"

    def test_harness_stage_model_and_arm(self, monkeypatch):
        _, client, log = self._run(monkeypatch, brief_mode="r8", stage="adhoc_aa790", model_tier="sonnet-x",
                                   quality_signal_extra={"arm": "r8", "aa790_run": "run1"})
        req = client.generate.call_args.args[0]
        assert req.stage == "adhoc_aa790" and req.model_tier == "sonnet-x"
        kw = log.await_args.kwargs
        assert kw["stage"] == "adhoc_aa790" and kw["tenant_id"] is None
        assert kw["quality_signal"]["arm"] == "r8" and kw["quality_signal"]["brief_mode"] == "r8"

    def test_explicit_off_beats_env_r8(self, monkeypatch):
        _, client, _ = self._run(monkeypatch, env="r8", brief_mode="off")
        assert client.generate.call_args.args[0].system_prompt is prompts.SYSTEM_PROMPT

    def test_r8_retries_once_on_a_fact_outline(self, monkeypatch):
        _, client, log = self._run(monkeypatch, brief=OUTLINE_BRIEF, brief_mode="r8")
        assert client.generate.call_count == 2 and log.await_count == 2
        second = client.generate.call_args_list[1].args[0]
        assert "YOUR PREVIOUS ANGLES BROKE THESE RULES" in second.user_prompt and "figures" in second.user_prompt
        assert second.system_prompt is prompts.SYSTEM_PROMPT_R8

    def test_off_never_retries_for_the_brief(self, monkeypatch):
        _, client, _ = self._run(monkeypatch, brief=OUTLINE_BRIEF)
        assert client.generate.call_count == 1

    def test_unusable_reply_logs_under_the_given_stage(self, monkeypatch):
        client = MagicMock()
        client.generate.return_value = LLMResponse(content="not json at all", model_used="sonnet",
                                                   provider="bedrock", cost_usd=0.03)
        unusable = AsyncMock()
        with patch.object(gen_mod, "LLMClient", return_value=client), patch.object(gen_mod, "record_unusable",
                                                                                   unusable):
            with pytest.raises(gen_mod.AngleGenerationError):
                asyncio.run(gen_mod.generate_angles(content_seed="s", goal=GOAL, brand_audience={},
                                                    brief_mode="r8", stage="adhoc_aa790",
                                                    quality_signal_extra={"arm": "r8"}))
        kw = unusable.await_args.kwargs
        assert kw["stage"] == "adhoc_aa790"
        assert kw["quality_signal_extra"] == {"attempt": 1, "arm": "r8", "brief_mode": "r8"}


# ---------------------------------------------------------------- the shared input object

class TestAngleInputs:
    def _inputs(self, moments=None):
        return service.AngleInputs(
            request={"request_id": "r"}, goal=GOAL, content_seed="seed", moments=moments, brand_audience={"x": 1},
            channel="blog", channel_style={"avoid": "hype"}, trip_name="T", destination="D", search_demand=None,
            tour_paa=None, piece_history=[])

    def test_generate_kwargs_single_atom(self):
        kw = self._inputs().generate_kwargs()
        assert set(kw) == {"content_seed", "goal", "brand_audience", "destination", "trip_name", "search_demand",
                           "piece_history", "channel_style"}

    def test_generate_kwargs_route(self):
        kw = self._inputs(moments=[{"m": 1}]).generate_kwargs()
        assert kw["moments"] == [{"m": 1}] and kw["is_route"] is True

    def test_measurable_rank_reads_the_channel_avoid_and_forbidden_words(self):
        inputs = self._inputs()
        inputs.brand_audience = {"forbidden_words": ["hidden gem"]}
        angles = _angles(GOOD_BRIEF)
        angles[0]["name"] = "A hidden gem ride"
        evidence, idx = service.measurable_rank(angles, inputs)
        assert evidence[0].violations and idx != 0

    def test_sanitize_briefs_in_place(self):
        angles = _angles("Open with the swim at the falls. Lead with the ride.")
        service.sanitize_briefs(angles, {})
        assert all(not a["best_final_style"].startswith("Open with") for a in angles)


# ---------------------------------------------------------------- the harness

class TestHarness:
    def test_request_keys_once_each_in_order(self):
        from scripts.aa790 import t9_writer_repanel as rp
        rows = [{"request_id": "r2", "tenant_id": "t2"}, {"request_id": "r1", "tenant_id": "t1"},
                {"request_id": "r2", "tenant_id": "t2"}]
        assert rp.request_keys(rows) == [("r2", "t2"), ("r1", "t1")]

    def test_chosen_angle_and_override(self):
        from scripts.aa790 import t9_writer_repanel as rp
        angles = _angles(GOOD_BRIEF)
        angles[2]["name"] = "Picked"
        chosen = rp.chosen_angle(angles, 2)
        assert chosen == {"name": "Picked", "why_it_works": "w", "formula_fit": "f", "best_final_style": GOOD_BRIEF}
        row = {"piece_id": "p", "request_id": "r", "channel": "linkedin", "atom_id": "a",
               "writer_input": {"seed": "Source itinerary: a ride. A swim.", "goal": "introduction_awareness",
                                "channel": "linkedin", "brand_pack": {},
                                "angle": {"name": "Stored", "why_it_works": "w0", "formula_fit": "f0",
                                          "best_final_style": "s0"}, "cta": "Write to us."}}
        assert rp.writer_args(row, 0)["angle"]["name"] == "Stored"
        assert rp.writer_args(row, 0, chosen)["angle"] == chosen

    def test_brief_census(self):
        from scripts.aa790 import t9_writer_repanel as rp
        census = rp.brief_census(_angles(GOOD_BRIEF)[:1] + _angles(OUTLINE_BRIEF)[:1])
        assert census[0]["problems"] == [] and census[1]["problems"]
        # the census counts words exactly as the round-8 contract check does
        assert census[0]["words"] == gen_mod.brief_words(GOOD_BRIEF)
        assert gen_mod.brief_words("Reader: a ride — then a swim.") == 6

    def test_stored_t8_seed_single_atom_and_route(self):
        from scripts.aa790 import t9_writer_repanel as rp
        from services.acp_angle_gate.route_digest import render_digest, route_moments
        assert rp.stored_t8_seed({"seed": "Atom seed"}) == "Atom seed"
        # one route moment is not a Route (service._route_moments needs more than one segment)
        assert rp.stored_t8_seed({"seed": "S", "route_moments": [{"atom_id": "a", "seed": "A"}]}) == "S"
        blocks = [("a1", "Falls — swim\nLocation: Luang Prabang, Laos\nSource itinerary: A swim."),
                  ("a2", "Night market — visit\nLocation: Luang Prabang, Laos\nSource itinerary: A walk.")]
        wi = {"seed": "S", "route_moments": [{"atom_id": a, "seed": b} for a, b in blocks]}
        assert rp.stored_t8_seed(wi) == render_digest(route_moments(blocks))
        assert rp.seed_sha(" x ") == rp.seed_sha("x") != rp.seed_sha("y")

    def _main(self, monkeypatch, regen, tmp_path):
        from scripts.aa790 import t9_writer_repanel as rp
        rows = [{"piece_id": "p1" * 8, "tenant_id": "t", "request_id": "r1", "channel": "linkedin",
                 "status": "approved", "created_at": "x", "content_text": "Old.", "atom_id": "a", "tenant": "T",
                 "writer_input": {"seed": "Atom seed. A swim.", "goal": "introduction_awareness",
                                  "channel": "linkedin", "brand_pack": {}, "cta": "Write to us.",
                                  "angle": {"name": "Stored", "why_it_works": "w", "formula_fit": "f",
                                            "best_final_style": "s"}}}]
        written = []

        async def _load(ids):
            return rows

        async def _regen(rows, **kw):
            return regen, 0.05

        def _write(row, args, tier, run_id, arm=""):
            written.append(args["angle"]["name"])
            return {"text": "A swim at the falls. Write to us.", "cost_usd": 0.03, "model": "m", "stop_reason": "end"}

        monkeypatch.setattr(rp, "load_rows", _load)
        monkeypatch.setattr(rp, "regen_angles", _regen)
        monkeypatch.setattr(rp, "stage_model", lambda stage="t9_write": "model-x")
        monkeypatch.setattr(rp, "call_writer", _write)
        out = tmp_path / "o.json"
        monkeypatch.setattr("sys.argv", ["x", "--ids", "p1", "--angles", "regen", "--t8-mode", "r8", "--arm", "r8",
                                         "--out", str(out)])
        assert rp.main() == 0
        return written, json.loads(out.read_text())

    def test_a_failed_regen_skips_the_piece_instead_of_writing_the_stored_angle(self, monkeypatch, tmp_path):
        written, out = self._main(monkeypatch, {"r1": {"error": "AtomNotFoundError: gone"}}, tmp_path)
        assert written == []
        assert out[0]["error"].startswith("t8_regen: AtomNotFoundError") and out[0]["arm"] == "r8"

    def test_a_regen_angle_is_written_and_the_seed_match_recorded(self, monkeypatch, tmp_path):
        from scripts.aa790 import t9_writer_repanel as rp
        chosen = {"name": "Fresh", "why_it_works": "w", "formula_fit": "f", "best_final_style": GOOD_BRIEF}
        same = rp.seed_sha("Atom seed. A swim.")
        written, out = self._main(monkeypatch, {"r1": {"chosen": chosen, "t8_seed_sha": same}}, tmp_path)
        assert written == ["Fresh"]
        assert out[0]["angle_source"] == "regen" and out[0]["seed_match"] is True
        _, drifted = self._main(monkeypatch, {"r1": {"chosen": chosen, "t8_seed_sha": rp.seed_sha("New seed")}},
                                tmp_path)
        assert drifted[0]["seed_match"] is False

    def test_t8_mode_needs_regen(self, monkeypatch):
        from scripts.aa790 import t9_writer_repanel as rp
        monkeypatch.setattr("sys.argv", ["x", "--ids", "a", "--t8-mode", "r8"])
        with pytest.raises(SystemExit):
            rp.main()
