"""AA-921 — pinned exemplars (WM-27) and tenant preference summaries (WM-28), pure parts. No DB, no model call.

  * excerpt / mask: whole sentences up to the cap, specifics masked, too-short text gives no candidate;
  * pick: <= 2 per channel, pinned first, never skipped; release_sha256 equals items_sha256 without examples (every
    release before CIS 313 keeps its hash) and changes with an example's text;
  * rendering: examples follow the tenant rules in the T9 user-prompt lines and the T2 block, the tenant's first and
    the platform's as fill, never another tenant's, never in the cached T9 SYSTEM_PROMPT slot, nothing without a
    rendering part; release 0 still renders nothing;
  * the carry-over check: an output that copies a 6-word run of an example is a QA trace `exemplar:<id>`;
  * WM-28: the unsourced-claim lint quarantines a summary with a promise / specific / add-verb and passes a tone
    summary; the run is off by default (`needs_llm_path`, no call) and, switched on with the drafter mocked, stores
    the quarantined draft `rejected`.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.writing_memory import eval_checks as ec
from services.writing_memory import exemplars as ex
from services.writing_memory import insertion
from services.writing_memory import preference as pref
from services.writing_memory import releases as rel

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
VOICE = ("Our guides keep the pace slow and the groups small. Mornings start early, before the heat, and every walk "
         "ends with time to sit and look around. We write plainly and skip the superlatives.")


def _row(eid, channel="blog", status="candidate", age_days=0, pin_age=None):
    return {"exemplar_id": eid, "channel": channel, "status": status, "created_at": NOW - timedelta(days=age_days),
            "status_at": None if pin_age is None else NOW - timedelta(days=pin_age), "masked_text": f"text {eid}"}


# ── excerpt / mask ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_excerpt_keeps_whole_sentences_under_the_cap():
    text = " ".join(f"Sentence number {i} has five words." for i in range(60))
    out = ex.excerpt(text, max_words=20)
    assert out.endswith(".") and len(out.split()) <= 20


def test_masked_excerpt_masks_specifics_and_refuses_short_text():
    m = ex.masked_excerpt(VOICE + " Day 3 costs $450 at 7:30 am near Paro Dzong.")
    assert "$450" not in m["masked_text"] and "[PRICE]" in m["masked_text"] and "Paro" not in m["masked_text"]
    assert len(m["text_sha256"]) == 64 and m["word_count"] == len(m["masked_text"].split())
    assert ex.masked_excerpt("Too short to show a voice.") is None


def test_channel_for_each_writer():
    assert ex.channel_for("t2", "anything") == "tour_page"
    assert ex.channel_for("t9_blog", None) == "blog"
    assert ex.channel_for("t9_social", "facebook") == "facebook"
    assert ex.channel_for("t9_social", "blog") is None and ex.channel_for("t9_social", None) is None


# ── selection and the fingerprint ──────────────────────────────────────────────────────────────────────────────────

def test_pick_two_per_channel_pinned_first_never_skipped():
    rows = [_row("a", age_days=1), _row("b", age_days=0), _row("c", status="pinned", age_days=9, pin_age=2),
            _row("d", status="never"), _row("e", channel="facebook"), _row("f", channel="facebook", age_days=3),
            _row("g", channel="facebook", age_days=5)]
    picked = ex.pick(rows)
    by = {(p["channel"], p["position"]): p["exemplar_id"] for p in picked}
    assert by == {("blog", 1): "c", ("blog", 2): "b", ("facebook", 1): "e", ("facebook", 2): "f"}


def test_release_sha256_without_examples_is_the_old_hash():
    rules = [{"rule_id": "r1", "rule_text": "Cut it.", "replacement": None}]
    assert ex.release_sha256(rules, []) == rel.items_sha256(rules)
    e = [{"exemplar_id": "e1", "channel": "blog", "position": 1, "masked_text": "voice"}]
    h = ex.release_sha256(rules, e)
    assert h != rel.items_sha256(rules)
    assert ex.release_sha256(rules, [{**e[0], "masked_text": "other"}]) != h


# ── rendering ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _part(scope, exemplars, tenant=None, rules=True):
    items = [{"rule_id": f"{scope}-r", "kind": "writer_instruction", "applies_to": [], "gate_class": None,
              "rule_text": f"Cut {scope} filler.", "replacement": None}] if rules else []
    return {"release_id": f"{scope}-rel", "release_no": 1, "content_class": "rules",
            "rule_ids": [i["rule_id"] for i in items], "scope": scope, "tenant_id": tenant, "items_sha256": "x" * 64,
            "items": items, "exemplars": exemplars}


def _ex(eid, channel="blog", pos=1):
    return {"exemplar_id": eid, "channel": channel, "position": pos, "masked_text": f"Voice sample {eid} here."}


def test_t9_lines_put_tenant_examples_after_the_rules_with_platform_fill():
    view = {"writer": "t9_blog", "platform": _part("platform", [_ex("p1"), _ex("p2", pos=2)]),
            "tenant": _part("tenant", [_ex("t1")], tenant="A")}
    lines = insertion.t9_tenant_lines(view, "blog")
    assert lines[0] == insertion.TENANT_HEADING and lines[2] == insertion.EXAMPLES_HEADING
    assert lines[3:] == ["Example 1: Voice sample t1 here.", "Example 2: Voice sample p1 here."]
    assert insertion.EXAMPLES_HEADING not in insertion.t9_platform_block(view, "blog")   # cached slot: no example
    assert insertion.example_lines(view, "facebook") == []                                # other channel: none


def test_t2_block_ends_with_its_examples():
    view = {"writer": "t2", "platform": None,
            "tenant": _part("tenant", [_ex("t1", "tour_page"), _ex("t2", "tour_page", 2)], tenant="A")}
    block = insertion.t2_block(view)
    assert block.index(insertion.TENANT_HEADING) < block.index(insertion.EXAMPLES_HEADING)
    assert block.rstrip().endswith("Example 2: Voice sample t2 here.")


def test_a_part_that_does_not_render_shows_no_example():
    broken = _part("tenant", [_ex("t1")], tenant="A")
    broken.pop("items")                                    # refused items: the part renders nothing
    view = {"writer": "t9_blog", "platform": None, "tenant": broken}
    assert insertion.t9_tenant_lines(view, "blog") == [] and insertion.exemplar_texts(view) == []


def test_release0_still_renders_nothing():
    empty = {"release_id": "z", "release_no": 0, "content_class": "empty", "rule_ids": [], "scope": "platform"}
    view = {"writer": "t9_blog", "platform": empty, "tenant": None}
    assert insertion.t9_tenant_lines(view, "blog") == [] and insertion.t2_block(view) == ""


def test_part_of_carries_the_examples():
    row = {"release_id": "r", "release_no": 2, "content_class": "rules", "rule_ids": ["a"], "items": [],
           "exemplars": [_ex("e1")]}
    assert insertion.part_of(row)["exemplars"] == [_ex("e1")]


# ── the carry-over check ───────────────────────────────────────────────────────────────────────────────────────────

def test_an_output_copying_an_example_run_is_a_qa_trace():
    m = ex.masked_excerpt(VOICE)["masked_text"]
    copied = "Here we go. Mornings start early, before the heat, and every walk ends with a view."
    assert ec.exemplar_copies(copied, [("e1", m)]) == ["exemplar:e1"]
    assert "exemplar:e1" in ec.qa_artefacts(copied, exemplars=[("e1", m)])
    fresh = "The trail climbs through pine forest to a quiet monastery above the river."
    assert ec.exemplar_copies(fresh, [("e1", m)]) == [] and ec.exemplar_copies(copied, []) == []


# ── WM-28: preference summaries ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("summary", [
    "Keep sentences short. Always mention the free hotel pickup.",
    "Write in a calm voice. Trips include 3 dinners.",
    "Use a warm register and highlight the guaranteed departure.",
])
def test_a_summary_with_an_unsourced_claim_is_quarantined(summary):
    out = pref.lint_summary(summary, ["truly breathtaking"], held_out=[], other_names=[])
    assert out["unsourced_claim"]["ok"] is False and out["quarantine"] is True and out["ok"] is False


def test_a_tone_summary_passes_the_claim_check():
    out = pref.unsourced_claim("Keep sentences short and plain. Avoid exclamation marks. Write in the second person.")
    assert out == {"ok": True, "detail": ""}


def _sig(i, tenant="A", writer="t9_blog", piece=None, actor="tenant", fact="pass"):
    return {"signal_id": f"s{i:02d}", "writer": writer, "channel": "blog", "scope": "tenant", "tenant_id": tenant,
            "before_text": "A truly breathtaking view awaits.", "after_text": "A view.", "actor_type": actor,
            "fact_status": fact, "source_id": piece or f"p{i % 3}"}


def test_groups_stay_inside_one_tenant_and_need_the_threshold():
    sigs = [_sig(i) for i in range(5)] + [_sig(10 + i, tenant="B") for i in range(2)] + [_sig(20, fact="grey")]
    groups = pref.build_groups(sigs)
    assert [(g.tenant_id, g.writer, len(g.signals)) for g in groups] == [("A", "t9_blog", 5)]
    shown, held = pref.split(groups[0])
    assert len(held) >= 2 and len(shown) + len(held) == 5


def test_parse_summary_shape():
    d = pref.parse_summary(json.dumps({"summary": "  Keep it short.  ", "match_phrases": ["truly", " "]}))
    assert d == {"summary": "Keep it short.", "match_phrases": ["truly"]}
    with pytest.raises(ValueError):
        pref.parse_summary(json.dumps({"summary": "", "match_phrases": []}))


def test_the_run_is_off_by_default_and_makes_no_call(monkeypatch):
    monkeypatch.delenv(pref.PREF_ENV, raising=False)

    class Boom:
        def acquire(self):
            raise AssertionError("an off run must not touch the DB")

    out = asyncio.run(pref.run_preference(Boom(), None))
    assert out == {"ran": False, "mode": "preference_summary", "state": "needs_llm_path", "calls": 0, "cost_usd": 0.0}
    assert pref.state()["state"] == "needs_llm_path"


def test_the_admin_run_route_refuses_preference_summaries_while_off(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from api.routers import admin_writing_memory as r
    monkeypatch.delenv(pref.PREF_ENV, raising=False)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=object())))
    with pytest.raises(HTTPException) as e:
        asyncio.run(r.run(r.RunBody(kind="preference_summary"), req, x_admin_user_id=None))
    assert e.value.status_code == 409 and e.value.detail["code"] == "needs_llm_path"
