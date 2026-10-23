"""AA-645 / S1-first step 2 — the S1 writer v2 builders (services/content_generation/s1_writer_v2.py). Pure, no LLM."""
from services.content_generation import s1_writer_v2 as v2
from services.content_generation.batch_prompt import build_s1_system_prompt, build_s1_user_prompt
from services.content_generation.forbidden_words import all_forbidden
from services.quality.s1_checks import run_checks
from services.quality.s1_facts import build_fact_table

MULTI = {
    "name": "Island Loop",
    "country": "Sri Lanka",
    "summary": "A loop by road. Contact Lanka Trails Ltd for details.",
    "itineraries": ("Day 1: Colombo to Chilaw\nRelax at the beach in Chilaw. (B, D)\n"
                    "Day 2-3: Chilaw\nFree days at the coast.\nOvernight: Chilaw Beach Hotel\n"
                    "Day 4: Chilaw to Habarana\nDrive 3 hrs to Habarana.\n"),
    "duration": "4 days",
    "inclusions": "Breakfast and dinner\nAccommodation\nGuide",
    "exclusions": "Lunches\nDrinks",
}
RAFT = {
    "name": "1 Day Trishuli River Rafting",
    "country": "Nepal",
    "itineraries": "Drive 3-4 hrs to Fishling. Raft for about 4 hrs down to Mugling with a short lunch break.",
    "duration": "1 DAY",
    "inclusions": "Rafting trip with lunch on the river bank",
    "exclusions": "Drinks\nInsurance",
}
NO_MARKERS = {**RAFT, "duration": "3 days", "name": "Three days in the hills",
              "itineraries": "Walk through the hills for three days and sleep in village homes."}


def plan_of(tour):
    return v2.day_plan(tour, build_fact_table(tour))


def test_day_plan_follows_source_markers_and_expands_ranges():
    p = plan_of(MULTI)
    assert p["mode"] == "source_days"
    assert [d["day"] for d in p["days"]] == [1, 2, 3, 4]
    assert p["days"][1]["range"] == (2, 3) and p["days"][2]["range"] == (2, 3)
    assert "Free days" in p["days"][1]["source"]


def test_day_plan_one_block_for_a_one_day_tour_and_duration_days_without_markers():
    assert [d["day"] for d in plan_of(RAFT)["days"]] == [1]
    assert plan_of(RAFT)["mode"] == "single_day"
    p = plan_of(NO_MARKERS)
    assert p["mode"] == "duration_days" and [d["day"] for d in p["days"]] == [1, 2, 3]


def test_day_plan_follows_markers_when_the_duration_disagrees():
    tour = {**MULTI, "duration": "6 days"}
    facts = build_fact_table(tour)
    assert any(c["value"] == "duration_vs_day_markers" for c in facts["conflicts"])
    assert [d["day"] for d in v2.day_plan(tour, facts)["days"]] == [1, 2, 3, 4]
    assert any(line.startswith("conflict:") for line in v2.ask_supplier(facts))


def test_fact_sheet_carries_days_figures_missing_and_do_not_use():
    facts = build_fact_table(MULTI)
    sheet = v2.render_fact_sheet(MULTI, facts, v2.day_plan(MULTI, facts))
    assert "DAYS TO WRITE (checked by code): Day 1, Day 2, Day 3, Day 4." in sheet
    assert "Days 2–3 are one block in the source" in sheet
    assert "[Day 4] Chilaw to Habarana\nDrive 3 hrs to Habarana." in sheet
    assert "- 3 hrs" in sheet
    assert "- group size" in sheet.split("NOT IN THE SOURCE")[1]
    assert "Lanka Trails Ltd" in sheet.split("DO NOT USE")[1]


def test_w_prompt_drops_the_old_length_brand_and_meta_pressure():
    facts = build_fact_table(MULTI)
    system = v2.w_system_prompt(all_forbidden(), "allow")
    user = v2.w_user_prompt(MULTI, facts, v2.day_plan(MULTI, facts))
    text = system + user
    for gone in ("PER-DAY SOURCE LENGTH", "0.7x", "expand it", "expert local", "BRAND DIFFERENTIATION",
                 "LESSONS FROM PREVIOUS", "People also ask", "seo_meta", "route type, distance, terrain"):
        assert gone not in text, gone
    assert "Never pad, never expand" in system
    assert "curated" in system                       # the AA core forbidden list is stated
    assert "one short clause of well-known background" in system
    assert "Do not add background" in v2.w_system_prompt([], "forbid")


def test_aplus_is_todays_prompt_byte_for_byte_plus_the_days_block():
    facts = build_fact_table(MULTI)
    plan = v2.day_plan(MULTI, facts)
    state = {"tour": MULTI, "seo": {"top_keywords": ["sri lanka loop"]}, "few_shots": []}
    system, user = v2.arm_prompts("aplus", MULTI, facts, plan, forbidden=[], aplus_state={"seo": state["seo"]})
    assert system == build_s1_system_prompt(state)
    assert user.startswith(build_s1_user_prompt(state))
    assert user.endswith("in this order: day 1, 2, 3, 4.")
    _, one = v2.arm_prompts("aplus", RAFT, build_fact_table(RAFT), plan_of(RAFT), forbidden=[])
    assert "write exactly one itinerary object (day 1)" in one


def test_relevant_keywords_keep_only_source_covered_non_transactional():
    seo = {"top_keywords": ["trishuli rafting nepal", "trishuli rafting price", "everest base camp trek",
                            {"keyword": "rafting nepal"}, "nepal rafting", "mugling", "fishling rafting"]}
    assert v2.relevant_keywords(seo, RAFT) == ["trishuli rafting nepal", "rafting nepal", "nepal rafting"]


def test_meta_checks_accept_a_good_meta_and_name_each_problem():
    facts = build_fact_table(RAFT)
    good = {"seo_title": "Trishuli River Rafting in Nepal: 1 Day",
            "seo_meta": ("A one-day Trishuli River rafting trip in Nepal: drive 3-4 hrs to Fishling, then raft for "
                         "about 4 hrs to Mugling with lunch on the river bank.")}
    assert 140 <= len(good["seo_meta"]) <= 155
    assert v2.meta_problems(good, RAFT, facts, all_forbidden()) == []
    short = {"seo_title": good["seo_title"], "seo_meta": "A rafting day in Nepal."}
    assert any("characters" in p for p in v2.meta_problems(short, RAFT, facts, []))
    cut = {"seo_title": good["seo_title"], "seo_meta": good["seo_meta"][:-1].rsplit(" ", 3)[0] + " with."}
    assert any("seo_meta" in p and "characters" not in p for p in v2.meta_problems(cut, RAFT, facts, []))
    invented = {**good, "seo_meta": good["seo_meta"].replace("about 4 hrs", "about 6 hrs")}
    assert any("not in the source" in p for p in v2.meta_problems(invented, RAFT, facts, []))
    hype = {**good, "seo_title": "Curated Trishuli Rafting"}
    assert any("forbidden" in p for p in v2.meta_problems(hype, RAFT, facts, all_forbidden()))
    assert v2.meta_problems(None, RAFT, facts, []) == ["the answer is not the JSON object asked for"]


def test_meta_retry_prompt_states_the_length_and_problems():
    text = v2.meta_retry_prompt({"seo_meta": "x" * 120}, ["seo_meta has 120 characters"])
    assert "has 120 characters" in text and "- seo_meta has 120 characters" in text


def test_e_page_is_code_built_and_passes_the_structure_checks():
    facts = build_fact_table(MULTI)
    plan = v2.day_plan(MULTI, facts)
    llm = {"name": "Sri Lanka Island Loop — 4 Days", "subtitle": "Four days from Colombo to Habarana.",
           "hook": "A four-day road loop from Colombo by the coast at Chilaw to Habarana.",
           "highlights": ["Beach at Chilaw", "Road to Habarana", "Colombo start"],
           "days": [{"day": 1, "title": "Colombo to Chilaw", "sentence": "The loop starts in Colombo."},
                    {"day": 2, "title": "Chilaw", "sentence": "Free time at the coast."},
                    {"day": 3, "title": "Chilaw", "sentence": "Free time at the coast."},
                    {"day": 4, "title": "Chilaw to Habarana", "sentence": "A drive to Habarana."}]}
    page = v2.finalize_meta(v2.assemble_e_page(MULTI, facts, plan, llm), {
        "seo_title": "Sri Lanka Island Loop: 4 Days",
        "seo_meta": ("A four-day Sri Lanka road loop from Colombo to the coast at Chilaw and on to Habarana, with "
                     "breakfast, dinner, accommodation and a guide included.")})
    assert page["itineraries"].startswith("Day 1 — Colombo to Chilaw\nThe loop starts in Colombo.\nMeals: B, D")
    assert "Overnight: Chilaw Beach Hotel" in page["itineraries"]
    assert "At a glance\n- Duration: 4 days\n- Destination: Sri Lanka" in page["summary"]
    assert page["description"].startswith("Included:\n- Breakfast and dinner")
    assert "Not included:\n- Lunches\n- Drinks" in page["description"]
    codes = [f["code"] for f in run_checks(MULTI, page, facts)]
    assert not [c for c in codes if c.startswith(("DAY_", "TEXT_TRUNCATED", "META_"))], codes


def test_facts_box_skips_inclusion_lines_about_more_than_one_thing():
    facts = build_fact_table(RAFT)
    glance = v2.at_a_glance(RAFT, facts)
    assert glance == ["Duration: 1 DAY", "Destination: Nepal"]


def test_agency_fields_reach_the_source_and_leave_the_missing_list():
    tour = v2.with_agency_fields(RAFT, {"group_size": "Max 12 people", "best_time_to_go": "October to May"})
    assert tour["description"].endswith("Group size: Max 12 people\nBest time to go: October to May")
    assert "group_size" not in build_fact_table(tour)["missing"]
    assert "Group size: Max 12 people" in v2.at_a_glance(tour, build_fact_table(tour))
    assert v2.with_agency_fields(RAFT, {}) == RAFT


def test_serialize_days_keeps_order_and_duplicates():
    text = v2.serialize_days([{"day": 1, "title": "A", "body": "x."}, {"day": 1, "title": "B", "body": "y."}])
    assert text == "Day 1 — A\nx.\n\nDay 1 — B\ny."


def test_parse_json_strips_fences_and_salvages():
    assert v2.parse_json('```json\n{"seo_title": "T", "seo_meta": "M."}\n```') == {"seo_title": "T", "seo_meta": "M."}
    assert v2.parse_json('{"seo_title": "T", "seo_meta": "M."') == {"seo_title": "T", "seo_meta": "M."}
    assert v2.parse_json("no json here") is None


def test_truncation_check_accepts_list_items_and_fact_lines_but_not_cut_prose():
    from services.quality.s1_checks import check_truncation
    assert check_truncation({"description": "Included:\n- Bike, helmet"}) == []
    assert check_truncation({"itineraries": "Day 1 — Paro\nA walk.\nOvernight: Paro"}) == []
    assert check_truncation({"summary": "At a glance\n- Duration: 3 hours"}) == []
    cut = check_truncation({"summary": "The ride follows the river past the old"})
    assert [f["code"] for f in cut] == ["TEXT_TRUNCATED"]


# ------------------------------------------------------------------ review round 1 (S248)

def test_truncation_still_flags_cut_label_and_list_lines():
    from services.quality.s1_checks import check_truncation
    for cut in ("Distance: 12 km. Overnight: Phakding. Meals: breakfast and",
                "Highlights include: the old town and the",
                "- Visit the temple and the",
                "- Bike, helmet,",
                "Overnight: Phakding or"):
        assert [f["code"] for f in check_truncation({"itineraries": "Day 1 — Paro\n" + cut})] == ["TEXT_TRUNCATED"], cut
    assert check_truncation({"summary": "At a glance\n- Grade: A"}) == []
    assert check_truncation({"itineraries": "Day 1 — Paro\nA walk.\nMeals: B, L, D"}) == []


def test_w_and_e_prompts_do_not_ask_for_claims_the_sheet_may_lack():
    facts = build_fact_table(MULTI)
    plan = v2.day_plan(MULTI, facts)
    w = v2.w_user_prompt(MULTI, facts, plan)
    e = v2.e_user_prompt(MULTI, facts, plan)
    assert "duration, start and end, main activity" not in w and "3 to 6" not in w and "3 to 5" not in e
    assert "start and end only when the sheet states them" in w
    assert "up to 6 concrete items from the sheet (fewer when it has fewer)" in w
    assert "up to 5 concrete items from the sheet (fewer when the sheet has fewer)" in e


def test_a_string_highlights_reply_is_one_item_not_characters():
    assert v2.assemble_page({"highlights": "Beach at Chilaw"})["highlights"] == ["Beach at Chilaw"]
    facts = build_fact_table(MULTI)
    page = v2.assemble_e_page(MULTI, facts, v2.day_plan(MULTI, facts), {"highlights": "Road to Habarana"})
    assert page["highlights"] == ["Road to Habarana"]
    assert v2.assemble_page({"highlights": None})["highlights"] == []


def test_e_page_drops_verbatim_lines_that_carry_do_not_use_items():
    tour = {**MULTI, "itineraries": MULTI["itineraries"].replace(
        "Overnight: Chilaw Beach Hotel", "Overnight: Chilaw Beach Hotel, call +94 77 123 4567"),
        "inclusions": "Breakfast and dinner\nGuide from www.lankatrails.example\nAccommodation"}
    facts = build_fact_table(tour)
    assert {e["reason"] for e in facts["do_not_use"]} >= {"phone", "url"}
    page = v2.assemble_e_page(tour, facts, v2.day_plan(tour, facts), {})
    text = page["itineraries"] + page["description"] + page["summary"]
    for e in facts["do_not_use"]:
        assert e["value"] not in text, e["value"]
    assert "- Breakfast and dinner" in page["description"]


def test_meta_problems_flag_operator_and_brand_names():
    facts = build_fact_table(MULTI)
    good = {"seo_title": "Sri Lanka Island Loop: 4 Days",
            "seo_meta": ("A four-day Sri Lanka road loop from Colombo to the coast at Chilaw and on to Habarana, with "
                         "breakfast, dinner, accommodation and a guide included.")}
    assert not any("names the operator" in p for p in v2.meta_problems(good, MULTI, facts, []))
    branded = {**good, "seo_title": "Adventure Asia: Sri Lanka Loop"}
    assert any("Adventure Asia" in p for p in v2.meta_problems(branded, MULTI, facts, []))
    op = {**good, "seo_meta": good["seo_meta"].replace("Sri Lanka road loop", "Lanka Trails Ltd loop")}
    assert any("Lanka Trails Ltd" in p for p in v2.meta_problems(op, MULTI, facts, []))


def test_meta_retry_prompt_handles_a_missing_first_answer():
    text = v2.meta_retry_prompt(None, ["the answer is not the JSON object asked for"])
    assert "YOUR PREVIOUS ANSWER was not usable" in text and '"seo_meta"' in text


# ------------------------------------------------------------------ review round 2 (S248)

def test_truncation_accepts_a_plus_at_the_end_of_a_count():
    from services.quality.s1_checks import check_truncation
    for whole in ("Group size: 10+", "- Suitable for ages 8+"):
        assert check_truncation({"summary": "At a glance\n" + whole}) == [], whole
        assert check_truncation({"itineraries": "Day 1 — Paro\nA walk.\n" + whole}) == [], whole
    assert [f["code"] for f in check_truncation({"summary": "At a glance\n- Bike, helmet &"})] == ["TEXT_TRUNCATED"]
