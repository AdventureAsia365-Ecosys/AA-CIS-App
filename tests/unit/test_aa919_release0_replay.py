"""AA-919 WM-20 — the release-0 replay in CI: the prompts of the four writers (A1 S1 writer v2, T2, T9 blog, T9 social),
captured on the live call path by the replay script's recorder (scripts/writing_memory/wm20_release0_replay.py), for
20 invented inputs per writer (tests/unit/aa919_release0_fakes.py).

The reference is `tests/unit/snapshots/aa919_release0_prompts.json`: the full sha256 of (system, user) per input,
written by the first commit of the AA-919 WM-18/20 branch on the unchanged writers (before the insertion path and the
state-builder extraction). The test compares the code of the tree with that file, never with itself (AA-919
critic 2). Regenerate only on purpose (a deliberate prompt change), never to make this test pass:
    AA919_WRITE_PINS=1 pytest tests/unit/test_aa919_release0_replay.py
Regenerated AA-789 tier 0 (S258, 29/10/2026): the route blog's "Thin moment" rule no longer says "ONE plain sentence"
(a thin moment is named in passing, with only what its source line states). Only the 5 route-blog user prompts change
(t9-blog-fake-00/04/08/12/16); every system prompt and the other 75 user prompts keep their pins.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.writing_memory import wm20_release0_replay as replay
from tests.unit import aa919_release0_fakes as fakes

PINS = Path(__file__).parent / "snapshots" / "aa919_release0_prompts.json"
# Environment flags the writer prompts read: pinned off, as on CI.
_FLAGS = ("S1_PER_DAY_TARGETS", "S1_STRUCTURED_FACTS", "T2_TARGETED_REPAIR", "T2_BEST_DRAFT", "S1_WRITER_V2",
          "PIPELINE_LESSONS_ENABLED")


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)


def _capture_all(view_for=None) -> dict:
    out = {}
    for writer, inputs in fakes.all_inputs().items():
        for inp in inputs:
            cap = replay.CAPTURE[writer](inp, view_for(writer) if view_for else None)
            out[inp["input_id"]] = {"writer": writer, "system_sha256": replay.sha(cap["system"]),
                                    "user_sha256": replay.sha(cap["user"])}
    return out


def test_fakes_are_twenty_per_writer_and_distinct():
    inputs = fakes.all_inputs()
    assert {w: len(v) for w, v in inputs.items()} == {"a1": 20, "t2": 20, "t9_blog": 20, "t9_social": 20}
    captured = _capture_all()
    for writer in replay.WRITERS:
        users = {v["user_sha256"] for v in captured.values() if v["writer"] == writer}
        assert len(users) == 20, f"{writer}: the fake inputs must give 20 different user prompts"


def test_memory_off_prompts_match_the_pins():
    captured = _capture_all()
    if os.environ.get("AA919_WRITE_PINS") == "1":
        PINS.write_text(json.dumps(captured, indent=1, sort_keys=True) + "\n")
    pins = json.loads(PINS.read_text())
    diffs = sorted(k for k in pins if captured.get(k) != pins[k])
    assert set(captured) == set(pins)
    assert diffs == [], f"{len(diffs)} prompts differ from the pre-AA-919 pins: {diffs[:5]}"


# ── WM-20: the insertion path changes no byte (release 0), and it is wired (positive control) ───────────────────────

def test_release0_view_prompts_match_the_pins():
    """Release 0 through the new insertion path gives the pinned (pre-AA-919) prompts, for all 4 x 20 inputs."""
    captured = _capture_all(view_for=replay.release0_view)
    pins = json.loads(PINS.read_text())
    diffs = sorted(k for k in pins if captured.get(k) != pins[k])
    assert diffs == [], f"{len(diffs)} release-0 prompts differ from the pins: {diffs[:5]}"


def test_replay_one_reports_zero_diffs_and_the_a1_stamp():
    for writer, inputs in fakes.all_inputs(3).items():
        for inp in inputs:
            row = replay.replay_one(writer, inp, phase="post")
            assert "error" not in row, row
            assert row["none_vs_r0_equal"] is True
            assert (row["r0_system_sha256"], row["r0_user_sha256"]) == (row["system_sha256"], row["user_sha256"])
            if writer == "a1":     # the v2 writer consumed the view: it stamps it (critic 29)
                assert row["r0_stamp"] == {"writer": "a1", "platform": {
                    "release_id": "00000000-0000-0000-0000-000000000000", "release_no": 0}, "tenant": None,
                    "ignored": False, "writer_version": "v2"}


SENTINEL = "WM-SENTINEL"


def _content_view(writer: str) -> dict:
    """AA-920: a view whose platform and tenant parts carry one checked writer_instruction each (the real builder
    renders them; the AA-919 sentinel monkeypatch of `_content` is gone with the stub)."""
    def part(scope, rid):
        return {"release_id": rid, "release_no": 1, "content_class": "rules", "rule_ids": [rid],
                "items": [{"rule_id": rid, "kind": "writer_instruction", "applies_to": [], "gate_class": None,
                           "rule_text": f"{SENTINEL} {scope} rule", "replacement": None}]}
    return {"writer": writer, "platform": part("platform", "p-1"),
            "tenant": None if writer == "a1" else part("tenant", "t-1")}


def test_positive_control_a1_slot_after_the_forbidden_line():
    from services.writing_memory.insertion import PLATFORM_HEADING
    inp = fakes.a1_inputs(1)[0]
    off = replay.capture_a1(inp)
    on = replay.capture_a1(inp, _content_view("a1"))
    assert SENTINEL not in off["system"]
    assert on["user"] == off["user"]
    # the HOUSE RULES lines right after the forbidden-words line and before "Output ONLY" (w_system_prompt's
    # filter(None) also drops its "" spacer, so the two are adjacent); the tenant part never reaches A1
    block = f"{PLATFORM_HEADING}\n- {SENTINEL} platform rule"
    assert on["system"] == off["system"].replace("\nOutput ONLY valid JSON", f"\n{block}\nOutput ONLY valid JSON", 1)
    lines = on["system"].split("\n")
    i = lines.index(PLATFORM_HEADING)
    assert lines[i - 1].startswith("Never use these words or phrases") and lines[i + 2].startswith("Output ONLY")


def test_positive_control_t2_slot_at_the_end_of_the_system_prompt():
    from services.writing_memory.insertion import PLATFORM_HEADING, TENANT_HEADING
    inp = fakes.t2_inputs(1)[0]
    off = replay.capture_t2(inp)
    on = replay.capture_t2(inp, _content_view("t2"))
    assert on["system"] == off["system"] + (f"\n\n{PLATFORM_HEADING}\n- {SENTINEL} platform rule"
                                            f"\n\n{TENANT_HEADING}\n- {SENTINEL} tenant rule")
    assert on["user"] == off["user"]


def test_positive_control_t9_platform_and_tenant_slots():
    from services.acp_content_writing.prompts import SYSTEM_PROMPT, t9_words_line
    from services.writing_memory.insertion import PLATFORM_HEADING, TENANT_HEADING

    for inp in (fakes.t9_inputs(1, blog=True)[0], fakes.t9_inputs(1, blog=False)[0]):
        off = replay.capture_t9(inp)
        on = replay.capture_t9(inp, _content_view("t9_blog"))
        assert off["system"] == SYSTEM_PROMPT
        assert on["system"] == SYSTEM_PROMPT + f"\n\n{PLATFORM_HEADING}\n- {SENTINEL} platform rule"
        words = t9_words_line(inp["writer_input"]["brand_pack"])
        assert on["user"] == off["user"].replace(
            words + "\n", words + f"\n{TENANT_HEADING}\n- {SENTINEL} tenant rule\n", 1)


def test_mirror_state_equals_the_live_state_builder():
    """The pre-image copy of the graph state (replay script) equals v1_pipeline.initial_state_for (post image)."""
    from api.routers.v1_pipeline import initial_state_for

    for inp in fakes.a1_inputs(3):
        kw = dict(brand_rules=inp["brand_rules"], tour_agency=inp["tour_agency"], generate_stage="s1_generate")
        assert replay._mirror_initial_state(inp["tour"], **kw) == initial_state_for(inp["tour"], **kw)
    for inp in fakes.t2_inputs(3):
        kw = dict(brand_rules=inp["brand_rules"], seo=inp["seo"], is_tenant_rewrite=True,
                  tenant_id=inp["tenant_id"], generate_stage="t2_generate")
        assert replay._mirror_initial_state(inp["tour"], **kw) == initial_state_for(inp["tour"], **kw)


def test_summarize_counts_diffs_against_the_pre_report():
    rows = [{"writer": "t2", "input_id": "x", "system_sha256": "a", "user_sha256": "b", "none_vs_r0_equal": True},
            {"writer": "t2", "input_id": "y", "system_sha256": "a", "user_sha256": "c", "none_vs_r0_equal": False},
            {"writer": "t2", "input_id": "z", "error": "boom"}]
    pre = {"x": {"system_sha256": "a", "user_sha256": "b"}, "y": {"system_sha256": "a", "user_sha256": "d"}}
    s = replay.summarize(rows, phase="post", pre=pre)["t2"]
    assert (s["inputs"], s["errors"], s["byte_diffs_none_vs_r0"], s["byte_diffs_vs_pre"]) == (2, 1, 1, 1)
