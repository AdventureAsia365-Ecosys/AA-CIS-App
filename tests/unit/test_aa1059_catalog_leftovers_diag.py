"""AA-1059: the read-only catalog leftovers diagnostic (scripts/aa1059/catalog_leftovers_diag.py).

The script runs in the api container through ecsx.sh; these tests pin the parts that need no database: the verdict
and next-step labels, the outcome class of a job result, the atom counts and the route facts. The SQL runs against a
real Postgres in tests/integration/test_aa1059_catalog_leftovers.py.
"""
import importlib.util
import os
import re

import pytest

from services.acp_contract.atom_ranking import classify_exclusion
from services.acp_contract.route_detection import Moment, derive_routes, route_gap_reason
from services.acp_contract.segment_matching import SegmentAtom, forms_segment

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1059", "catalog_leftovers_diag.py")


@pytest.fixture(scope="module")
def diag():
    spec = importlib.util.spec_from_file_location("aa1059_catalog_leftovers_diag", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_is_tracked_next_to_its_usage_line():
    # scripts/ is git-ignored in this repo: the file must be force-added or the ecsx.sh live step has nothing to run
    assert os.path.isfile(_PATH)


def test_the_script_opens_a_read_only_ssl_session_and_writes_nothing():
    src = open(_PATH, encoding="utf-8").read()
    assert '"default_transaction_read_only": "on"' in src
    assert 'ssl="require"' in src
    assert 'os.environ["DATABASE_URL"]' in src
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
        assert verb not in src.upper(), verb


def test_it_never_prints_names_places_or_texts():
    src = open(_PATH, encoding="utf-8").read()
    # text columns are never read (the approve gate SQL that reads version text lives in the app, its result is
    # reduced to blocker codes here)
    for column in ("aa_name", "src_name", "failure_summary", "reviewer_notes", "canonical_action", "ta.text",
                   "evidence", "error"):
        assert column not in src.replace("ERR ", "").replace("never error messages", ""), column
    # no f-string (the only way a value reaches the output) interpolates a place or an action
    for literal in re.findall(r'f"[^"]*"', src):
        for expr in re.findall(r"\{([^{}]*)\}", literal):
            expr = expr.replace("with_place_action", "")   # a count key, not a value
            assert "place" not in expr and "action" not in expr, literal


@pytest.mark.parametrize("value,label", [
    (None, "missing"), ("ok", "ok"), ("not_run", "not_run"),
    ("failed: AssertionError: some free text", "failed:AssertionError"),
    ("failed: ", "failed:unknown"), ("something else", "other"),
])
def test_ssr_class(diag, value, label):
    assert diag.ssr_class(value) == label


def test_atom_counts_use_the_segment_rules(diag):
    atoms = [{"place": "Zzlo Fortress", "action": "visit", "activity_type": "culture", "itinerary_day": 1},
             {"place": "Zzlo", "action": "drive to Qarvi", "activity_type": None, "itinerary_day": 2},
             {"place": None, "action": None, "activity_type": None, "itinerary_day": None}]
    c = diag.atom_counts(atoms, forms_segment=forms_segment, classify_exclusion=classify_exclusion,
                         segment_atom=SegmentAtom)
    assert c == {"atoms": 3, "with_place_action": 2, "forming": 1, "transit": 1, "unnamed": 0, "no_day": 1}
    unknown = diag.atom_counts(atoms)
    assert unknown["forming"] is None and unknown["transit"] is None


@pytest.mark.parametrize("counts,a3,after,verdict", [
    ({"with_place_action": 0, "forming": 0}, None, [], "no_place_action"),
    ({"with_place_action": 3, "forming": 0}, {"ssr": "ok"}, [], "all_atoms_excluded"),
    ({"with_place_action": 3, "forming": 2}, None, [], "no_atomize_job"),
    ({"with_place_action": 3, "forming": 2}, {"ssr": "failed:AssertionError"}, [],
     "segment_step_failed:AssertionError"),
    ({"with_place_action": 3, "forming": 2}, {"ssr": "not_run"}, [], "segment_step_not_run"),
    ({"with_place_action": 3, "forming": 2}, {"ssr": "missing"}, [], "segment_step_not_run"),
    ({"with_place_action": 3, "forming": 2}, {"ssr": "ok"}, ["restored"], "atomized_while_inactive"),
    ({"with_place_action": 3, "forming": 2}, {"ssr": "ok"}, ["paused"], "segment_ran_no_member"),
    ({"with_place_action": 3, "forming": None}, {"ssr": "ok"}, [], "segment_ran_no_member"),
])
def test_segment_verdict(diag, counts, a3, after, verdict):
    assert diag.segment_verdict(counts, a3, after) == verdict


@pytest.mark.parametrize("kw,step", [
    (dict(latest_status="hitl", pending={"on_latest": True}, blockers=[]), "staff_approve"),
    (dict(latest_status="hitl", pending={"on_latest": True}, blockers=["score_below_floor"]),
     "staff_edit_or_regenerate"),
    (dict(latest_status="hitl", pending={"on_latest": True}, blockers=None), "staff_review"),
    (dict(latest_status="hitl", pending={"on_latest": False}, blockers=[]), "pending_not_latest"),
    (dict(latest_status="hitl", pending=None, blockers=None), "no_review_item"),
    (dict(latest_status="rejected", pending=None, blockers=None), "rejected_regenerate"),
    (dict(latest_status="approved", pending=None, blockers=None), "approved_not_published"),
    (dict(latest_status="generating", pending=None, blockers=None), "s1_in_flight"),
    (dict(latest_status=None, pending=None, blockers=None), "no_s1_version"),
    (dict(latest_status="hitl", pending={"on_latest": True}, blockers=[], has_master=True), "stale_item_dismiss"),
    (dict(latest_status="hitl", pending={"on_latest": True}, blockers=[], source_live=False), "stale_item_dismiss"),
])
def test_s1_next_step(diag, kw, step):
    assert diag.s1_next_step(**kw) == step


def test_tour_route_facts_and_the_every_day_what_if(diag):
    segs = [("base", "Zzlo", [1, 2]), ("s3", "Qarvi", [3])]
    f = diag.tour_route_facts(segs, Moment, route_gap_reason, derive_routes)
    assert f == {"reason": "no_consecutive_days", "segments": 2, "days": 3, "whatif_all_days": True}
    none = diag.tour_route_facts([], Moment, route_gap_reason, derive_routes)
    assert none == {"reason": "no_ranked_moment", "segments": 0, "days": 0, "whatif_all_days": False}
    undated = diag.tour_route_facts([("s1", "Zzlo", [])], Moment, route_gap_reason, derive_routes)
    assert undated["reason"] == "no_day" and undated["whatif_all_days"] is False


# ── the enqueue helper the orchestrator runs after the deploy ─────────────────────────────────────────────────────────

_ENQ = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "aa1059", "enqueue_catchup_pass.py")


def test_enqueue_helper_asks_for_one_platform_pass_with_an_empty_segment_scope(monkeypatch, capsys):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    spec = importlib.util.spec_from_file_location("aa1059_enqueue_catchup_pass", _ENQ)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = MagicMock()
    conn.close = AsyncMock()
    enq = AsyncMock(return_value=("job-zz1059", True))
    monkeypatch.setenv("DATABASE_URL", "postgresql://zz:zz@127.0.0.1:1/zz")
    with patch.object(mod.asyncpg, "connect", AsyncMock(return_value=conn)) as connect, \
         patch("services.jobs.recompute_job.enqueue_recompute", enq):
        asyncio.run(mod.main())
    assert connect.await_args.kwargs["ssl"] == "require"
    kw = enq.await_args.kwargs
    # [] (only the touched and brand-new Segments are re-landed), never None (a full backfill)
    assert kw["scope"] == "platform" and kw["segment_ids"] == [] and kw["debounce_s"] == 0
    assert kw["created_by"] == "script:aa1059"
    conn.close.assert_awaited()
    assert "job=job-zz1059 created=True" in capsys.readouterr().out
