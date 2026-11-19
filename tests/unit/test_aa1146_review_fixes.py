"""AA-1146 (S285) review fixes, the parts that need no database: a merge's price rule (D5), the merged facts held as a
draft and its stale check (T1), a wave that holds a hand-locked tour can complete (the 'locked' state), the editors a
draft's publish refuses, and who may publish a sheet draft. Invented tour names and places only; the database side is
tests/integration/test_aa1146_tour_versions_pg.py."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core.staff_authz import denial_reason, principal_from_role
from services.ingestion import excel_merge as em
from services.waves import report as wr


def _req(role):
    return SimpleNamespace(headers={} if role is None else {"x-admin-role": role}, scope={})


# ── excel_merge: withhold and the stale check ─────────────────────────────────────────────────────────────────────

def test_withhold_keeps_the_blocked_fields_out_and_names_them():
    changes, withheld = em.withhold({"duration": "4 days", "price_raw": "From 90 USD"}, em.PRICE_FIELDS)
    assert changes == {"duration": "4 days"} and withheld == {"price_raw": "From 90 USD"}
    assert em.withhold({"duration": "4 days"}, em.PRICE_FIELDS) == ({"duration": "4 days"}, {})


def test_stale_fields_compare_normalised_values():
    before = {"duration": "3 days", "src_highlights": ["Quill falls"]}
    assert em.stale_fields({"duration": "3  days", "src_highlights": '["Quill falls"]'}, before) == []
    assert em.stale_fields({"duration": "3 days and 2 nights", "src_highlights": ["Quill falls"]}, before) == \
        ["duration"]


# ── a merge never changes the price without reference_price.write (D5) ─────────────────────────────────────────────

def _plan(conflict_price: bool):
    current = {"duration": "3 days", "price_raw": "From 120 USD"}
    incoming = {"duration": "4 days", "price_raw": "From 99 USD"}
    return em.plan(current, incoming, {"price_raw"} if conflict_price else set())


@pytest.mark.parametrize("role", ["content", "content_lead"])
def test_choosing_the_sheet_price_is_refused_without_the_pricing_role(role):
    from api.routers.admin_pipeline.uploads import _merge_changes
    with pytest.raises(HTTPException) as e:
        _merge_changes(_req(role), _plan(True), {"price_raw": "incoming"})
    assert e.value.status_code == 403 and e.value.headers["X-Error-Code"] == "price_read_only"
    changes, res, withheld = _merge_changes(_req(role), _plan(True), {"price_raw": "current"})
    assert changes == {"duration": "4 days"} and res == {"price_raw": "current"} and withheld == {}


@pytest.mark.parametrize("role", ["content", "content_lead"])
def test_an_untouched_sheet_price_is_held_back_without_the_pricing_role(role):
    from api.routers.admin_pipeline.uploads import _merge_changes
    changes, _res, withheld = _merge_changes(_req(role), _plan(False), {})
    assert changes == {"duration": "4 days"} and withheld == {"price_raw": "From 99 USD"}


@pytest.mark.parametrize("role", ["admin", "customer_success", None])
def test_the_pricing_roles_and_the_machine_merge_the_price(role):
    from api.routers.admin_pipeline.uploads import _merge_changes
    changes, _res, withheld = _merge_changes(_req(role), _plan(True), {"price_raw": "incoming"})
    assert changes == {"duration": "4 days", "price_raw": "From 99 USD"} and withheld == {}


def test_an_unresolved_merge_is_422():
    from api.routers.admin_pipeline.uploads import _merge_changes
    with pytest.raises(HTTPException) as e:
        _merge_changes(_req("admin"), _plan(True), {})
    assert e.value.status_code == 422 and e.value.detail["code"] == "merge_unresolved"


# ── who may publish or discard a sheet draft ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,may", [("admin", True), ("content_lead", True), ("content", True), ("viewer", False),
                                      ("tenant_qa", False), ("customer_success", False)])
def test_sheet_drafts_follow_the_publish_and_content_rules(role, may):
    p = principal_from_role(role)
    assert (denial_reason(p, "POST", "/admin/tours/{tour_id}/fact-draft/publish") is None) is may
    assert (denial_reason(p, "DELETE", "/admin/tours/{tour_id}/fact-draft") is None) is may


# ── the editors a draft's publish refuses include a hand editor of the row ────────────────────────────────────────

def test_draft_editors_add_the_row_hand_editor():
    from api.routers.admin_pipeline.drafts import _draft_editors
    row = {"edit_diff": {"editors": ["admin:zz-a"]}, "author_id": "admin:zz-a", "reviewed_by": "zz-b"}
    assert _draft_editors(row) == ["admin:zz-a", "admin:zz-b"]
    assert _draft_editors({**row, "reviewed_by": None}) == ["admin:zz-a"]


def test_the_inline_edit_refuses_the_live_version_and_an_open_draft():
    from api.routers.admin_pipeline.tour_detail import _EDIT_REFUSALS, _EDIT_REFUSED_SQL
    assert set(_EDIT_REFUSALS) == {"live", "draft"}
    assert _EDIT_REFUSALS["draft"][0] == "edit_draft_version" and _EDIT_REFUSALS["live"][0] == "edit_live_version"
    assert "to_jsonb(gc) ->> 'origin' = 'human'" in _EDIT_REFUSED_SQL     # no failure before CIS 457


# ── a wave with a hand-locked tour completes ──────────────────────────────────────────────────────────────────────

LOCK = {"lock_at": "2026-11-19T08:00:00+00:00", "lock_by": "admin:zz-lead", "lock_reason": "zz fixed day two"}


def _t(**kw):
    return {"tour_id": "t-1", "src_name": "Quill Bay Loop", "country": "Vietnam", **kw}


def test_a_skipped_locked_tour_is_terminal_locked():
    assert wr.classify(_t(s1_status="succeeded", s1_skip_reason="human_locked", **LOCK)) == "locked"
    assert wr.classify(_t(s1_status=None, **LOCK)) == "locked"            # left out by the batch submit
    assert "locked" in wr.TERMINAL_STATES


def test_other_states_keep_their_order():
    assert wr.classify(_t(s1_status="succeeded")) == "unclassified"         # not skipped, not locked
    assert wr.classify(_t(s1_status=None)) == "not_started"
    assert wr.classify(_t(any_active=True, **LOCK)) == "running"
    assert wr.classify(_t(master_status="active", published_in_wave=True, s1_status="succeeded",
                          s1_skip_reason="human_locked", **LOCK)) == "master"
    assert wr.classify(_t(s1_status="failed", **LOCK)) == "failed"


def test_build_report_completes_and_shows_the_lock():
    tours = [_t(s1_status="succeeded", s1_skip_reason="human_locked", **LOCK),
             {**_t(master_status="active", published_in_wave=True, s1_status="succeeded"), "tour_id": "t-2"}]
    rep = wr.build_report({"id": "w"}, tours, [{"kind": "s1_rewrite", "status": "succeeded", "n": 2}], [],
                          {"calls": 0, "cost_usd": 0.0})
    assert rep["complete"] is True and rep["states"]["locked"] == 1 and rep["not_terminal"] == []
    locked = next(r for r in rep["tours"] if r["state"] == "locked")
    assert locked["lock"] == {"at": LOCK["lock_at"], "by": "admin:zz-lead", "reason": "zz fixed day two"}
    assert next(r for r in rep["tours"] if r["state"] == "master")["lock"] is None


def test_tours_sql_reads_the_skip_reason_and_the_lock_without_failing_before_457():
    assert "j.result ->> 'reason' AS skip_reason" in wr.TOURS_SQL
    assert "to_jsonb(pt) ->> 'human_lock_at'" in wr.TOURS_SQL
