"""AA-1148 (S290) — the content workspace without a database: the facts form (normalise, the text columns rendered
from it, the first wording, the required list), the supplier list (the picker's order, the name match), the route
policy and the limits read from the settings registry. Invented names and places only."""
import asyncio
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from api.core.staff_authz import POLICY, ROLE_PERMS, policy_capabilities
from services.content_workspace import facts as F
from services.content_workspace import policy
from services.content_workspace import store
from services.content_workspace import suppliers as S

LIMITS = policy.Limits(max_days=21, min_inclusions=3, min_exclusions=1, bulk_max=100)


def _form(**over):
    base = dict(
        name="  Zephyr Delta   Discovery ", country="VN", supplier_id=None, summary="Three slow days on the delta.",
        nights=2,
        days=[
            {"title": "Arrive in Quillmoor", "places": ["Quillmoor", "Old Mill"], "meals": ["dinner", "lunch"],
             "overnight": "Quillmoor Riverside Lodge", "description": "Boat ride to the old mill."},
            {"title": "Floating market", "places": ["Ombre Market"], "meals": ["breakfast"],
             "overnight": "Ombre Homestay", "description": "Early start at the market."},
            {"title": "Back to the city", "places": ["Fennel City"], "meals": [], "overnight": "",
             "description": "Transfer back."},
        ],
        inclusions=["Transport", "English-speaking guide", "Entrance fees", "transport"],
        exclusions=["Drinks"], group_min=2, group_max=12, tour_type="private",
        guide_languages=["English", "French"], best_time_to_go="November to April",
    )
    base.update(over)
    return F.FactsIn(**base)


WORDING = {"aa_name": "Zephyr Delta Discovery", "aa_summary": "Three slow days.",
           "aa_itineraries": "Day 1 — Arrive\nText.\n\nDay 2 — Market\nText.\n\nDay 3 — Home\nText."}
SUPPLIER = {"id": "11111111-1111-4111-8111-111111111111", "status": "active"}


def test_normalize_trims_dedupes_and_orders_meals():
    out = F.normalize(_form())
    assert out["name"] == "Zephyr Delta Discovery"
    assert out["inclusions"] == ["Transport", "English-speaking guide", "Entrance fees"]   # "transport" dropped
    assert out["days"][0]["meals"] == ["lunch", "dinner"]
    assert out["v"] == 1 and len(out["days"]) == 3


def test_render_gives_the_excel_shaped_text_columns():
    text = F.render(F.normalize(_form(), tour_type_label="Private tour"))
    assert text["duration"] == "3 days 2 nights"
    assert text["group_size"] == "2-12 travellers"
    assert text["inclusions"] == "Transport\nEnglish-speaking guide\nEntrance fees"
    assert text["src_itineraries"].startswith("Day 1 — Arrive in Quillmoor\nPlaces: Quillmoor, Old Mill\n"
                                             "Meals: Lunch, Dinner\nOvernight: Quillmoor Riverside Lodge\n")
    assert "Day 3 — Back to the city\nPlaces: Fennel City\nTransfer back." in text["src_itineraries"]
    assert text["src_description"] == "Tour type: Private tour\nGuide languages: English, French"


def test_render_uses_the_saved_label_of_the_tour_type():
    # the label an administrator gave the type when the tour was saved; a code alone falls back to a readable name
    stored = F.normalize(_form(tour_type="zz_small_group"), tour_type_label="Zz small group (max 8)")
    assert stored["tour_type_label"] == "Zz small group (max 8)"
    assert F.render(stored)["src_description"].startswith("Tour type: Zz small group (max 8)\n")
    bare = F.normalize(_form(tour_type="zz_small_group"))
    assert F.render(bare)["src_description"].startswith("Tour type: Zz small group\n")
    assert F.normalize(_form(tour_type=None), tour_type_label="ignored")["tour_type_label"] is None


@pytest.mark.parametrize("lo,hi,expected", [(None, None, None), (2, 2, "2 travellers"), (1, None, "from 1 traveller"),
                                            (None, 8, "up to 8 travellers")])
def test_group_text(lo, hi, expected):
    assert F.group_text(lo, hi) == expected


def test_duration_without_days_is_none_and_one_day_is_singular():
    assert F.duration_text(0, None) is None
    assert F.duration_text(1, 0) == "1 day 0 nights"


def test_first_wording_has_one_canonical_block_per_day():
    w = F.first_wording(F.normalize(_form()))
    assert w["aa_name"] == "Zephyr Delta Discovery" and w["aa_highlights"] == []
    assert F.wording_days(w["aa_itineraries"]) == 3
    assert "Places:" not in w["aa_itineraries"]          # facts lines stay in the source text only


def test_wording_days_counts_ranges_once():
    assert F.wording_days("Day 1 — A\nx\n\nDay 2-3 — B\ny\n\nDay 3 — C") == 3
    assert F.wording_days(None) == 0


def test_a_complete_form_passes_the_required_list():
    items = F.checklist(F.normalize(_form()), limits=LIMITS, country_ok=True, supplier=SUPPLIER, wording=WORDING)
    assert F.missing(items) == [] and F.completeness(items) == 100


def test_the_required_list_names_what_is_missing():
    stored = F.normalize(_form(inclusions=["Transport"], exclusions=[], nights=None,
                               days=[{"title": "", "places": [], "description": ""}]))
    items = F.checklist(stored, limits=LIMITS, country_ok=False, supplier=None, wording={})
    keys = {i["key"] for i in F.missing(items)}
    assert keys == {"country", "supplier", "nights", "day_titles", "day_places", "day_descriptions", "inclusions",
                    "exclusions", "wording", "wording_days"}
    inc = next(i for i in items if i["key"] == "inclusions")
    assert inc["params"] == {"minimum": 3, "count": 1} and "at least 3 items" in inc["message"]
    assert 0 < F.completeness(items) < 100


def test_limits_come_from_the_settings():
    stored = F.normalize(_form(inclusions=["Transport"]))
    lenient = policy.Limits(max_days=2, min_inclusions=1, min_exclusions=0, bulk_max=5)
    items = {i["key"]: i for i in F.checklist(stored, limits=lenient, country_ok=True, supplier=SUPPLIER,
                                               wording=WORDING)}
    assert items["inclusions"]["ok"] and not items["days"]["ok"] and items["days"]["params"]["max_days"] == 2


def test_overlap_and_hints():
    stored = F.normalize(_form(exclusions=["Entrance fees"], nights=1))
    items = {i["key"]: i for i in F.checklist(stored, limits=LIMITS, country_ok=True, supplier=SUPPLIER,
                                               wording=WORDING)}
    assert not items["no_overlap"]["ok"] and items["no_overlap"]["params"]["items"] == ["entrance fees"]
    assert not items["nights_match"]["ok"] and items["nights_match"]["required"] is False


@pytest.mark.parametrize("status,ok", [("active", True), ("pending", True), ("unknown", True), ("inactive", False),
                                       ("missing", False)])
def test_supplier_item_by_status(status, ok):
    items = {i["key"]: i for i in F.checklist(F.normalize(_form()), limits=LIMITS, country_ok=True,
                                               supplier={"id": "x", "status": status}, wording=WORDING)}
    assert items["supplier"]["ok"] is ok


def test_form_bounds_are_validated():
    with pytest.raises(ValidationError):
        _form(tour_type="Cruise!")          # a code: lower-case letters, digits and underscores
    with pytest.raises(ValidationError):
        _form(tour_type="z" * 41)
    with pytest.raises(ValidationError):
        _form(inclusions=["x" * 201])
    with pytest.raises(ValidationError):
        F.DayIn(meals=["brunch"])


def test_facts_from_a_sheet_row_are_rebuilt_read_only():
    row = {"manual_facts": None, "src_name": "Ombre Lake Walk", "src_summary": None, "inclusions": "Water\n\nGuide",
           "exclusions": None, "best_time_to_go": None}
    out = F.facts_from_row(row)
    assert out["inclusions"] == ["Water", "Guide"] and out["days"] == []


# ── suppliers ─────────────────────────────────────────────────────────────────────────────────────────────────────

SUPPLIERS = [
    {"id": "a", "code": "SUP-1", "company_name": "Mekong Zephyr Co., Ltd", "categories": ["dmc"], "country_code": "VN",
     "status": "active"},
    {"id": "b", "code": "SUP-2", "company_name": "Quillmoor Hotels", "categories": ["hotel"], "country_code": "VN",
     "status": "active"},
    {"id": "c", "code": "SUP-3", "company_name": "Ombre Travel JSC", "categories": ["dmc"], "country_code": "LA",
     "status": "pending"},
    {"id": "d", "code": "SUP-4", "company_name": "Ombre Travel", "categories": ["dmc"], "country_code": "TH",
     "status": "inactive"},
    {"id": "e", "code": "SUP-5", "company_name": "Fennel Trips", "categories": ["guide"], "country_code": "KH",
     "status": "active"},
    {"id": "f", "code": "SUP-6", "company_name": "Fennel Trips", "categories": ["dmc"], "country_code": None,
     "status": "active"},
]


@pytest.mark.parametrize("text,expected", [("Mekong Zephyr Co., Ltd", "mekong zephyr"),
                                           ("Công ty TNHH Đồng Tháp Xanh", "dong thap xanh"),
                                           ("  OMBRE-travel, JSC ", "ombre travel"), (None, "")])
def test_normalize_names(text, expected):
    assert S.normalize(text) == expected


def test_suggestion_needs_one_match_in_a_compatible_country():
    assert S.suggestion("MEKONG ZEPHYR", "VN", SUPPLIERS)["id"] == "a"
    assert S.suggestion("Mekong Zephyr", "TH", SUPPLIERS) is None       # the countries contradict
    assert S.suggestion("Ombre Travel", None, SUPPLIERS)["id"] == "c"   # the inactive twin is never suggested
    assert S.suggestion("Fennel Trips", None, SUPPLIERS) is None        # two candidates: a person chooses
    assert S.suggestion("Fennel Trips", "KH", SUPPLIERS) is None        # f has no country: still two candidates
    assert S.suggestion("", "VN", SUPPLIERS) is None


def test_picker_order_active_dmc_country_name():
    ids = [s["id"] for s in S.rank(SUPPLIERS, country="LA")]
    assert ids == ["f", "a", "e", "b", "c"]          # active first, DMCs first, then the tour's country, then name
    assert [s["id"] for s in S.rank(SUPPLIERS, query="ombre", include_inactive=True)] == ["c", "d"]
    assert [s["id"] for s in S.rank(SUPPLIERS, query="sup-2")] == ["b"]


def test_an_unreadable_view_is_none_not_an_error():
    class Boom:
        def acquire(self):
            raise RuntimeError("no grant")

    assert asyncio.run(S.list_all(Boom())) is None
    assert asyncio.run(S.by_ids(Boom(), ["a"])) is None
    assert asyncio.run(S.by_ids(Boom(), [None])) == {}


# ── routes and limits ─────────────────────────────────────────────────────────────────────────────────────────────

def test_route_policy():
    assert policy_capabilities(POLICY[("POST", "/admin/content-workspace/tours")]) == ("content.write",)
    assert policy_capabilities(POLICY[("PUT", "/admin/content-workspace/tours/{tour_id}/facts")]) == ("content.write",)
    assert policy_capabilities(POLICY[("POST", "/admin/content-workspace/supplier-links")]) == ("content.write",)
    assert policy_capabilities(POLICY[("POST", "/admin/content-workspace/supplier-links/apply-suggestions")]) == (
        "content.publish",)
    assert "content.write" in ROLE_PERMS["content"] and "content.publish" not in ROLE_PERMS["content"]


def test_limits_read_the_four_keys():
    seen = []

    async def aget(key, tenant_id=None):
        seen.append(key)
        return {policy.MAX_DAYS_KEY: 9, policy.MIN_INCLUSIONS_KEY: 2, policy.MIN_EXCLUSIONS_KEY: 0,
                policy.BULK_MAX_KEY: 7}[key]

    with patch("shared.settings.aget", aget):
        lim = asyncio.run(policy.limits())
    assert lim.as_dict() == {"max_days": 9, "min_inclusions": 2, "min_exclusions": 0, "bulk_max": 7}
    assert sorted(seen) == sorted([policy.MAX_DAYS_KEY, policy.MIN_INCLUSIONS_KEY, policy.MIN_EXCLUSIONS_KEY,
                                   policy.BULK_MAX_KEY])


# ── the option lists (admin data, CIS 478) ────────────────────────────────────────────────────────────────────────

def test_form_options_read_the_three_lists_and_skip_malformed_entries():
    values = {
        policy.TOUR_TYPES_KEY: [{"code": "zz_private", "label": "Zz private", "label_vi": "Zz riêng"},
                                {"code": "zz_join", "label": " Zz   join "}, {"code": "Bad Code", "label": "x"},
                                {"code": "zz_private", "label": "dup"}, {"code": "zz_nolabel"}, "not an object"],
        policy.QUICK_INCLUSIONS_KEY: ["Zz boat", "zz BOAT", "", 7, "Zz lunch"],
        policy.QUICK_EXCLUSIONS_KEY: "not a list",
    }

    async def aget(key, tenant_id=None):
        return values[key]

    with patch("shared.settings.aget", aget):
        opts = asyncio.run(policy.form_options())
    assert opts.as_dict() == {
        "tour_types": [{"code": "zz_private", "label": "Zz private", "label_vi": "Zz riêng"},
                       {"code": "zz_join", "label": "Zz join", "label_vi": "Zz join"}],
        "quick_inclusions": ["Zz boat", "Zz lunch"], "quick_exclusions": []}
    assert opts.tour_type("zz_join")["label"] == "Zz join" and opts.tour_type("zz_none") is None
    assert set(policy.KEYS) >= {policy.TOUR_TYPES_KEY, policy.QUICK_INCLUSIONS_KEY, policy.QUICK_EXCLUSIONS_KEY}


OPTS = policy.FormOptions(tour_types=({"code": "zz_private", "label": "Zz private", "label_vi": "Zz riêng"},),
                          quick_inclusions=(), quick_exclusions=())


def test_a_tour_type_must_be_offered_or_already_held():
    assert store._tour_type_label("zz_private", {}, OPTS) == "Zz private"
    assert store._tour_type_label(None, {"tour_type": "zz_private"}, OPTS) is None
    # removed by an administrator since: the tour keeps it, with the label it was saved with
    held = {"tour_type": "zz_gone", "tour_type_label": "Zz gone"}
    assert store._tour_type_label("zz_gone", held, OPTS) == "Zz gone"
    with pytest.raises(store.WorkspaceError) as e:
        store._tour_type_label("zz_gone", {}, OPTS)
    assert e.value.status == 422 and e.value.code == "tour_type_unknown" and e.value.extra["allowed"] == ["zz_private"]


# ── the required list on every publish path, and the two-person facts rule ───────────────────────────────────────

GAPS = [{"key": "supplier", "message": "Choose the AA supplier from Booking's list", "params": {"status": None},
         "ok": False, "required": True}]


def test_required_missing_error_carries_the_items():
    err = store.RequiredMissingError("t-zz", "v-zz", GAPS)
    message = "This hand-made tour is not complete: Choose the AA supplier from Booking's list"
    assert str(err) == message
    assert err.detail() == {"code": "required_missing", "message": message,
                            "items": [{"key": "supplier", "message": "Choose the AA supplier from Booking's list",
                                       "params": {"status": None}}]}


class _Conn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def test_process_export_guard_refuses_an_incomplete_hand_made_tour():
    from services.export import handler

    conn = _Conn()
    row = {"aa_name": "Zz", "aa_summary": "Zz.", "aa_itineraries": "Day 1 — Zz"}
    with patch.object(store, "required_gaps", side_effect=_async(GAPS)) as gaps:
        with pytest.raises(store.RequiredMissingError):
            asyncio.run(handler._required_list_guard(conn, "silver_aa_internal", "v-zz", "t-zz", row))
    assert gaps.call_args.kwargs["wording"] == {"aa_name": "Zz", "aa_summary": "Zz.", "aa_itineraries": "Day 1 — Zz"}
    assert len(conn.executed) == 1 and "SET status = 'hitl'" in conn.executed[0][0]
    for ok in (None, []):                    # a sheet tour, a complete hand-made tour: nothing written
        conn = _Conn()
        with patch.object(store, "required_gaps", side_effect=_async(ok)):
            asyncio.run(handler._required_list_guard(conn, "silver_aa_internal", "v-zz", "t-zz", row))
        assert conn.executed == []


def test_process_export_runs_the_guard_after_the_lock_guard_on_every_exit():
    import inspect

    from services.export import handler

    src = inspect.getsource(handler.process_export)
    assert src.index("guard_export(") < src.index("_required_list_guard(") < src.index("_lifecycle_publish(conn")


def test_s1_auto_publish_files_an_incomplete_hand_made_tour_for_review():
    from api.routers.admin_pipeline import _engine

    req = type("Req", (), {"tour_id": "t-zz"})()
    with patch("services.export.handler.process_export", side_effect=store.RequiredMissingError("t-zz", "v", GAPS)), \
            patch.object(_engine, "_enqueue_review", side_effect=_async(None)) as enq:
        asyncio.run(_engine._auto_publish(object(), req, "v-zz", {}))
    assert enq.call_args.kwargs["failure_summary"].startswith("This hand-made tour is not complete")


def test_a_review_approve_of_an_incomplete_hand_made_tour_is_409_and_reopens_the_review():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from fastapi import HTTPException

    from api.routers.admin_pipeline import _publish

    conn = _Conn()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    request = SimpleNamespace(headers={}, scope={})
    with patch("services.export.handler.process_export", side_effect=store.RequiredMissingError("t-zz", "v", GAPS)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(_publish.approve_export(Pool(), request, "r-zz", "v-zz"))
    assert e.value.status_code == 409 and e.value.detail["code"] == "required_missing"
    assert e.value.headers["X-Error-Code"] == "required_missing"
    assert "review_status = 'pending'" in conn.executed[0][0] and conn.executed[0][1] == ("r-zz",)
    with patch("services.export.handler.process_export", side_effect=_async({"catalog_id": "pt-zz"})):
        asyncio.run(_publish.approve_export(Pool(), request, "r-zz", "v-zz"))     # published: no review write
    assert len(conn.executed) == 1


def test_a_live_facts_save_is_refused_while_two_person_publishing_is_on():
    row = {"entry_origin": "manual", "source_status": "active", "deleted_at": None, "master_status": "active",
           "master_deleted_at": None}
    with pytest.raises(store.WorkspaceError) as e:
        store._check_savable(row, {}, expected_saved_at=None, reason="zz a good reason", two_person=True)
    assert e.value.status == 409 and e.value.code == store.FACTS_TWO_PERSON
    assert store._check_savable(row, {}, expected_saved_at=None, reason="zz a good reason", two_person=False) is True
    draft = {**row, "master_status": None}    # never published: nobody outside sees it, the autosave goes on
    assert store._check_savable(draft, {}, expected_saved_at=None, reason=None, two_person=True) is False


def _async(value):
    async def _f(*_a, **_k):
        return value
    return _f
