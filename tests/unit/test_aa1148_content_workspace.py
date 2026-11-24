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
    text = F.render(F.normalize(_form()))
    assert text["duration"] == "3 days 2 nights"
    assert text["group_size"] == "2-12 travellers"
    assert text["inclusions"] == "Transport\nEnglish-speaking guide\nEntrance fees"
    assert text["src_itineraries"].startswith("Day 1 — Arrive in Quillmoor\nPlaces: Quillmoor, Old Mill\n"
                                             "Meals: Lunch, Dinner\nOvernight: Quillmoor Riverside Lodge\n")
    assert "Day 3 — Back to the city\nPlaces: Fennel City\nTransfer back." in text["src_itineraries"]
    assert text["src_description"] == "Tour type: Private tour\nGuide languages: English, French"


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
        _form(tour_type="cruise")
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
    assert sorted(seen) == sorted(policy.KEYS)
