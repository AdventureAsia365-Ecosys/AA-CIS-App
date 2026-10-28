"""AA-920 WM-25 — T2 renders a rules release, the version records `generation_meta` (CIS 297), the tenant never sees it.

Mock-only: no AWS, no DB, no LLM. Every rule text is invented (the golden-set text guard scans this public repo).
The tenant side (key walk of every tenant tour-version read with a poisoned `generation_meta`) is in
tests/unit/test_aa796_tenant_projection.py; this file covers the writer side.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.content_generation import batch_prompt
from services.writing_memory import insertion
from services.writing_memory import releases as rel
from tests.unit.test_aa778_t2_em_dash import SOURCE, _T2, _pool
from tests.unit.test_aa920_injection import TENANT_A, _FakeDB, _release_row, _rule_row

PLATFORM_RULE, TENANT_RULE = "Cut a closing line that only restates the title.", "Name the start town in the summary."


def _t2_view():
    """A T2 view resolved through the real resolver: platform release 1 with one rule, tenant release 1 with one."""
    p = [_rule_row("r-p1", writer="t2", rule_text=PLATFORM_RULE)]
    t = [_rule_row("r-t1", writer="t2", scope="tenant", tenant=TENANT_A, rule_text=TENANT_RULE)]
    rows = [_release_row(no=1, cls="rules", rule_ids=["r-p1"], sha=rel.items_sha256(p)),
            _release_row("tenant", 1, cls="rules", rule_ids=["r-t1"], sha=rel.items_sha256(t), tenant=TENANT_A)]
    view = asyncio.run(insertion.resolve(_FakeDB(rows, p + t), "t2", TENANT_A))
    assert view is not None and view["writer"] == "t2"
    return view


def test_generation_meta_is_the_stamp_and_rule_ids_only():
    assert insertion.generation_meta(None) is None                       # memory off: the column stays NULL
    view = _t2_view()
    meta = insertion.generation_meta(view)
    assert meta == {"writing_release": insertion.stamp(view), "rule_ids": ["r-p1", "r-t1"]}
    assert meta["writing_release"]["platform"]["items_sha256"] == view["platform"]["items_sha256"]
    assert meta["writing_release"]["ignored"] is False
    dumped = json.dumps(meta)
    assert PLATFORM_RULE not in dumped and TENANT_RULE not in dumped and "rule_text" not in dumped


def test_generation_meta_carries_the_prompt_version_with_or_without_memory():
    assert insertion.generation_meta(None, prompt_version="") is None
    assert insertion.generation_meta(None, prompt_version="ab12cd34") == {"writer_prompt_version": "ab12cd34"}
    view = _t2_view()
    meta = insertion.generation_meta(view, prompt_version="ab12cd34")
    assert meta == {"writer_prompt_version": "ab12cd34", "writing_release": insertion.stamp(view),
                    "rule_ids": ["r-p1", "r-t1"]}


def test_generation_meta_of_release0_names_the_release_and_no_rule():
    view = {"writer": "t2", "platform": {"release_id": "p", "release_no": 0, "content_class": "empty", "rule_ids": [],
                                         "items": [], "items_sha256": rel.items_sha256([])}, "tenant": None}
    meta = insertion.generation_meta(view)
    assert meta["rule_ids"] == [] and meta["writing_release"]["platform"]["release_no"] == 0


def test_the_t2_system_prompt_ends_with_the_platform_then_tenant_rules():
    view = _t2_view()
    base = batch_prompt.build_t2_system_prompt(dict(_T2))
    with_rules = batch_prompt.build_t2_system_prompt({**_T2, "writing_memory": view})
    assert with_rules.startswith(base)                                  # nothing moves: the block is appended
    tail = with_rules[len(base):]
    assert tail.index(insertion.PLATFORM_HEADING) < tail.index(PLATFORM_RULE) < tail.index(
        insertion.TENANT_HEADING) < tail.index(TENANT_RULE)


def test_a_t2_rules_release_may_activate():
    assert rel.wiring_problem({"writer": "t2", "content_class": "rules"}) is None
    assert "rules" in rel.WIRED_CLASSES["t2"]


def _run_save(view, prompt_version=None):
    pv = {"prompt_version": prompt_version} if prompt_version else {}
    result = {"status": "success", "quality_score": 8.0, **pv,
              "generated": {"name": "Invented ridge walk", "subtitle": "s", "summary": "Two quiet days.",
                            "highlights": ["A ridge"], "itineraries": "Day 1: Walk.", "seo_title": "t",
                            "seo_meta": "m", "trip_type": "adventure"}}
    qa = {"result": result, "passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": []}
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 5.0
    tour = dict(SOURCE, subtitle="", description="", seo_title="", seo_meta="", country="Nepal", duration="2 Days")
    from services.jobs import t2_rewrite_job as t2
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value=result)) as rw, \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
         patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))), \
         patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=view)):
        diag = asyncio.run(t2._rewrite_and_save(_pool(conn), {"tour_id": "tour-1"}, tour, {}, TENANT_A, "v-1",
                                                "p-1"))
    [save] = [c.args for c in conn.execute.call_args_list if "SET rewritten_content" in c.args[0]]
    return save, diag, rw


def test_the_t2_save_stamps_generation_meta():
    view = _t2_view()
    save, diag, rw = _run_save(view)
    sql, args = save[0], save[1:]
    assert "generation_meta = $8::jsonb" in sql and args[6] == "v-1"
    assert json.loads(args[7]) == insertion.generation_meta(view)
    assert diag["writing_release"] == insertion.stamp(view)              # job.result keeps its stamp (AA-919)
    assert rw.call_args.kwargs["writing_memory"] is view                 # the writer wrote with the same view


def test_memory_off_saves_a_null_generation_meta():
    save, diag, _ = _run_save(None)
    assert save[8] is None and "writing_release" not in diag


@pytest.mark.parametrize("key", ["generation_meta", "rule_ids", "release", "memory"])
def test_the_tenant_regex_names_every_writing_memory_key(key):
    from api.routers._tenant_projection import TENANT_FORBIDDEN_KEY_RE
    assert TENANT_FORBIDDEN_KEY_RE.search(key)


def test_memory_off_still_stamps_the_prompt_version_of_the_kept_draft():
    save, diag, _ = _run_save(None, prompt_version="ab12cd34")
    assert json.loads(save[8]) == {"writer_prompt_version": "ab12cd34"} and "writing_release" not in diag


def test_the_save_stamps_release_and_prompt_version_together():
    view = _t2_view()
    save, _, _ = _run_save(view, prompt_version="ab12cd34")
    assert json.loads(save[8]) == insertion.generation_meta(view, prompt_version="ab12cd34")
