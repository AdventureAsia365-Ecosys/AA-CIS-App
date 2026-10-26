"""AA-919 WM-20 — the release-0 replay in CI: the prompts of the four writers (A1 S1 writer v2, T2, T9 blog, T9 social),
captured on the live call path by the replay script's recorder (scripts/writing_memory/wm20_release0_replay.py), for
20 invented inputs per writer (tests/unit/aa919_release0_fakes.py).

The reference is `tests/unit/snapshots/aa919_release0_prompts.json`: the full sha256 of (system, user) per input,
written by the first commit of the AA-919 WM-18/20 branch on the unchanged writers (before the insertion path and the
state-builder extraction). The test compares the code of the tree with that file, never with itself (AA-919
critic 2). Regenerate only on purpose (a deliberate prompt change), never to make this test pass:
    AA919_WRITE_PINS=1 pytest tests/unit/test_aa919_release0_replay.py
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
