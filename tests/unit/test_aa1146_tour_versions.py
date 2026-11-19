"""AA-1146 (S285) — tour versions, the hand-edit lock and the Excel merge: the pure parts and the wiring that needs no
database (tour_draft, excel_merge, human_lock with a fake connection, the S1 job skip, the route policy). Invented
tour names and places only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, ROLE_PERMS, denial_reason, principal_from_role
from services.content_generation import human_lock
from services.content_generation import tour_draft as td
from services.ingestion import excel_merge as em


# ── tour_draft ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_draft_fields_are_the_editable_version_fields():
    from api.routers.admin_pipeline.tour_detail import _ALLOWED_GC_FIELDS
    assert set(td.DRAFT_FIELDS) == _ALLOWED_GC_FIELDS
    assert "aa_itineraries" in td.DRAFT_FIELDS     # the Review Queue and the draft both edit the itinerary


def test_diff_lists_only_changed_fields_with_word_ops():
    live = {"aa_name": "Zephyr Ridge Walk", "aa_itineraries": "Day 1: walk to Lake Ombre. Day 2: boat home.",
            "aa_highlights": json.dumps(["Lake Ombre", "Old mill"])}
    draft = {"aa_name": "Zephyr Ridge Walk", "aa_itineraries": "Day 1: walk to Lake Ombre. Day 2: train home.",
             "aa_highlights": ["Lake Ombre", "Old mill"]}
    out = td.diff_fields(td.fields_of(live), td.fields_of(draft))
    assert [d["field"] for d in out] == ["aa_itineraries"]
    ops = out[0]["ops"]
    assert {"op": "delete", "text": "boat"} in ops and {"op": "insert", "text": "train"} in ops
    assert "".join(o["text"] for o in ops if o["op"] != "insert") == live["aa_itineraries"]


def test_diff_without_a_live_version_lists_every_written_field():
    out = td.diff_fields(None, td.fields_of({"aa_name": "Quill Bay Loop", "seo_title": "Quill Bay"}))
    assert [d["field"] for d in out] == ["aa_name", "seo_title"]


@pytest.mark.parametrize("reason,expected", [(None, None), ("ok", None), ("  fix day two  ", "fix day two"),
                                             ("x" * 501, None), (5, None)])
def test_clean_reason(reason, expected):
    assert td.clean_reason(reason) == expected


def test_editors_are_the_author_then_each_editor_once():
    d = td.with_editor({}, "admin:zz-a", ["aa_name"], "first pass")
    d = td.with_editor(json.dumps(d), "admin:zz-b", ["aa_summary"], "second pass")
    d = td.with_editor(d, "admin:zz-a", ["aa_summary"], "third pass")
    assert d["editors"] == ["admin:zz-a", "admin:zz-b"] and d["reason"] == "third pass"
    assert td.editors_of(d, "admin:zz-a") == ["admin:zz-a", "admin:zz-b"]
    assert td.editors_of(None, None) == []


def test_db_value_dumps_only_jsonb_lists():
    assert td.db_value("aa_highlights", ["a"]) == '["a"]'
    assert td.db_value("aa_highlights", '["a"]') == '["a"]'
    assert td.db_value("aa_name", "Plain") == "Plain"


# ── excel_merge (T1) ──────────────────────────────────────────────────────────────────────────────────────────────

CURRENT = {"src_name": "Fennel Valley Trek", "duration": "3 days", "inclusions": "Water", "period": "All year",
           "provider": "Zz Supplier", "src_highlights": json.dumps(["Fennel falls"]), "country": "Vietnam"}


def test_untouched_fields_auto_accept_and_hand_edits_conflict():
    incoming = {"src_name": "Fennel Valley Trek", "duration": "4 days", "inclusions": "Water, lunch",
                "period": "", "provider": None, "src_highlights": ["Fennel falls"]}
    plan = em.plan(CURRENT, incoming, hand_edited={"inclusions"})
    assert plan["unchanged"] == ["src_name", "src_highlights"]
    assert [a["field"] for a in plan["auto"]] == ["duration"]
    assert [c["field"] for c in plan["conflicts"]] == ["inclusions"] and plan["conflicts"][0]["hand_edited"]
    # an empty cell never erases a value (period, provider are not in the plan)
    assert "period" not in json.dumps(plan) and "provider" not in json.dumps(plan)


def test_resolve_needs_a_choice_for_every_conflict():
    plan = em.plan(CURRENT, {"duration": "4 days", "inclusions": "Water, lunch"}, {"inclusions"})
    with pytest.raises(ValueError, match="inclusions"):
        em.resolve(plan, {})
    with pytest.raises(ValueError, match="not conflicts"):
        em.resolve(plan, {"inclusions": "current", "duration": "current"})
    changes, res = em.resolve(plan, {"inclusions": "current"})
    assert changes == {"duration": "4 days"} and res == {"inclusions": "current"}     # the hand edit wins
    changes, res = em.resolve(plan, {"inclusions": "incoming"})
    assert changes == {"duration": "4 days", "inclusions": "Water, lunch"}


def test_hand_edited_fields_follow_the_history_in_order():
    revisions = [
        {"revision_num": 1, "source": "staff_edit", "fields": ["inclusions", "duration"], "resolutions": None},
        {"revision_num": 2, "source": "excel_merge", "fields": ["duration"], "resolutions": json.dumps(
            {"inclusions": "current"})},
        {"revision_num": 3, "source": "staff_edit", "fields": ["period"], "resolutions": None},
    ]
    # the merge took duration from the sheet (owned again by the sheet) and a person kept inclusions
    assert em.hand_edited_fields(reversed(revisions)) == {"inclusions", "period"}


# ── human_lock (fake connection) ──────────────────────────────────────────────────────────────────────────────────

class _LockConn:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows or [], exc
        self.executed = []

    async def fetch(self, sql, ids):
        if self.exc:
            raise self.exc
        return [r for r in self.rows if r["tour_id"] in ids]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.mark.real_human_lock
def test_lock_of_reads_locked_tours_and_tolerates_a_missing_column():
    import asyncpg
    row = {"tour_id": "00000000-0000-4000-8000-0000000000a1", "human_lock_at": None, "human_lock_by": "admin:zz",
           "human_lock_reason": "zz fixed the second day"}
    assert asyncio.run(human_lock.lock_of(_LockConn([row]), row["tour_id"]))["by"] == "admin:zz"
    assert asyncio.run(human_lock.lock_of(_LockConn([]), row["tour_id"])) is None
    missing = asyncpg.exceptions.UndefinedColumnError("column human_lock_at does not exist")
    assert asyncio.run(human_lock.locks_of(_LockConn(exc=missing), [row["tour_id"]])) == {}


def test_lock_exits_split_people_from_automation():
    assert human_lock.AUTOMATIC_EXITS == {"auto_publish", "recompose", "lambda"}
    assert human_lock.HUMAN_EXITS == {"approve", "promote", "human_publish"}
    assert not human_lock.AUTOMATIC_EXITS & human_lock.HUMAN_EXITS
    assert human_lock.is_human_version({"origin": "human", "human_edited": False})
    assert human_lock.is_human_version({"origin": None, "human_edited": True})
    assert not human_lock.is_human_version({"origin": "llm", "human_edited": False})


def test_the_error_names_the_tour():
    e = human_lock.HumanLockedError("t-1", {"by": "admin:zz"})
    assert "t-1" in str(e) and e.lock == {"by": "admin:zz"}


# ── the S1 job skips a locked tour before any paid call ───────────────────────────────────────────────────────────

def test_s1_job_skips_a_locked_tour(monkeypatch):
    from services.jobs import s1_rewrite_job as job

    async def _locked(conn, tour_id):
        return {"locked": True, "by": "admin:zz", "at": None, "reason": "zz edit"}
    monkeypatch.setattr(human_lock, "lock_of", _locked)
    progress = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = SimpleNamespace(pool=pool, job_id="job-zz", progress=progress,
                          job=SimpleNamespace(progress={}), payload={"tour_id": "t-1"})
    out = asyncio.run(job._human_lock_skip(ctx, "t-1"))
    assert out["skipped"] is True and out["reason"] == human_lock.SKIP_REASON
    progress.assert_awaited_once_with(phase="skipped", tour_id="t-1", skip_reason=human_lock.SKIP_REASON)


def test_s1_job_runs_an_unlocked_tour():
    from services.jobs import s1_rewrite_job as job
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    ctx = SimpleNamespace(pool=pool, job_id="job-zz", progress=AsyncMock())
    assert asyncio.run(job._human_lock_skip(ctx, "t-1")) is None   # conftest: nothing is locked


# ── who may do what (D1, D5) ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,may", [("admin", True), ("content_lead", True), ("content", True), ("viewer", False),
                                      ("tenant_qa", False), ("customer_success", False)])
def test_publish_routes_take_content_publish_or_review_act(role, may):
    for key in [("POST", "/admin/tours/{tour_id}/draft/publish"),
                ("POST", "/admin/tours/{tour_id}/versions/{version_num}/promote"),
                ("POST", "/admin/review-queue/{review_id}/approve")]:
        assert (denial_reason(principal_from_role(role), *key) is None) is may, (role, key)


def test_content_publish_is_lead_and_admin_only_and_the_lead_lost_price_writes():
    holders = {r for r, perms in ROLE_PERMS.items() if "content.publish" in perms}
    assert holders == {"admin", "content_lead"}
    writers = {r for r, perms in ROLE_PERMS.items() if "reference_price.write" in perms}
    assert writers == {"admin", "customer_success"}


def test_unlock_is_master_admin_and_drafts_are_content_work():
    assert POLICY[("POST", "/admin/tours/{tour_id}/unlock")] == "master.admin"
    assert denial_reason(principal_from_role("content"), "POST", "/admin/tours/{tour_id}/unlock") is not None
    for method in ("PUT", "DELETE"):
        assert denial_reason(principal_from_role("content"), method, "/admin/tours/{tour_id}/draft") is None
        assert denial_reason(principal_from_role("viewer"), method, "/admin/tours/{tour_id}/draft") is not None
