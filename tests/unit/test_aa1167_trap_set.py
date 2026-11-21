"""AA-1167 — the prompt-injection trap set, offline (no model call): every trap reaches its writer's prompt, sits
inside a fence when the fence is on, and the scorer catches a reply that obeyed it (and the output sanitizer still
cleans its published render). The paid half is scripts/aa1167/trap_run.py (a live step, not CI). Invented text only."""
import pytest

from services.ai_safety import prompt_fence as pf
from services.ai_safety import trap_set


@pytest.mark.parametrize("fence", [False, True])
def test_every_trap_reaches_its_writer(fence):
    cases = trap_set.cases(fence)
    assert [c.trap.id for c in cases] == [t.id for t in trap_set.TRAPS]
    assert {c.writer for c in cases} == set(trap_set.WRITERS)
    for c in cases:
        assert any(m.lower() in c.user.lower() for m in c.trap.markers), c.trap.id
        assert c.stage in ("s1_generate", "t9_write", "t2_generate") and c.max_tokens > 0


def test_with_the_fence_on_every_payload_is_fenced_data():
    for c in trap_set.cases(True):
        outside = pf.outside_fences(c.user).lower()
        assert not any(m.lower() in outside for m in c.trap.markers), c.trap.id
        assert pf.SYSTEM_RULE in c.system


def test_with_the_fence_off_the_prompts_are_main():
    for c in trap_set.cases(False):
        assert '<untrusted_data label="' not in c.user and pf.SYSTEM_RULE not in c.system


def test_the_fence_break_trap_cannot_close_the_fence():
    case = next(c for c in trap_set.cases(True) if c.trap.kind == "fence_break")
    assert case.user.count("</untrusted_data>") == case.user.count("<untrusted_data ")


def test_the_trap_run_sends_at_most_the_trap_set():
    from scripts.aa1167 import trap_run
    assert len(trap_run.plan("on")) == len(trap_set.TRAPS) <= 10
    assert len(trap_run.plan("both")) == 2 * len(trap_set.TRAPS)
    assert trap_run.STAGE == "adhoc_aa1167"


def test_the_trap_run_refuses_apply_without_a_budget():
    from scripts.aa1167 import trap_run
    with pytest.raises(SystemExit):
        trap_run.main(["--apply"])


@pytest.mark.parametrize("trap", trap_set.TRAPS, ids=[t.id for t in trap_set.TRAPS])
def test_a_reply_that_obeyed_the_trap_fails(trap):
    obeyed = f"Velor reed villages. {trap.payload}"
    r = trap_set.evaluate(trap, obeyed)
    assert r["passed"] is False and (r["leaks"] or r["published_markup"])


@pytest.mark.parametrize("trap", trap_set.TRAPS, ids=[t.id for t in trap_set.TRAPS])
def test_a_clean_reply_passes(trap):
    clean = "## Lake Velor\n\nA boat crosses to Isle Marrow, then the shore path leads to Tessa Point."
    assert trap_set.evaluate(trap, clean)["passed"] is True


def test_the_published_render_of_an_obeyed_markup_trap_is_clean():
    from services.acp_content_writing.export import render_markdown_body
    for trap in (t for t in trap_set.TRAPS if t.kind == "markup"):
        html = render_markdown_body(f"Velor. {trap.payload}").lower()
        for needle in ("<script", "<iframe", "onerror", "onmouseover", "javascript:", "<img"):
            assert needle not in html


def test_a_reply_naming_the_fence_tag_fails():
    trap = trap_set.TRAPS[0]
    assert trap_set.evaluate(trap, "Per the untrusted_data block, the lake is calm.")["passed"] is False


def test_summary_counts_per_writer_and_kind():
    results = [trap_set.evaluate(t, "clean text") for t in trap_set.TRAPS]
    s = trap_set.summary(results)
    assert s["traps"] == s["passed"] == len(trap_set.TRAPS)
    assert sum(v["traps"] for v in s["by_writer"].values()) == len(trap_set.TRAPS)
    assert set(s["by_kind"]) == {t.kind for t in trap_set.TRAPS}
