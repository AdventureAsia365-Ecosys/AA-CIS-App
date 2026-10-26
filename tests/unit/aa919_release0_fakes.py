"""AA-919 WM-20 — invented writer inputs for the release-0 replay test (20 per writer, deterministic). No real tour,
tenant or brand text: every place, brand and sentence below is made up for the test (public repo, ADR 0010
decision 11). The inputs vary the branches the prompts depend on: A1 agency fields and forbidden lists; T2 brand
packs with / without a core idea, brief, signature words, en-GB, SEO; T9 blog (single moment and route), the social
channels, facts, keyword, search questions, lead focus.
"""
from __future__ import annotations

PLACES = ["Valley of Orrin", "Lake Tessaly", "Mount Corvel", "Brindle Gorge", "Old Port of Saliss",
          "Fennick Forest", "Harrow Steppe", "Isle of Quell", "Marrow Pass", "Upper Venn"]
COUNTRIES = ["Orrinia", "Tessaland", "Corvelia", "Brindlemark"]
ACTIVITIES = ["a guided walk along the ridge", "a boat crossing at dawn", "a visit to the weaving cooperative",
              "a cycle ride on the canal path", "a cooking class with a local family", "a short rafting stretch"]
BRANDS = ["Lanternfall Journeys", "Copperwing Trips", "Northbell Travel", "Quietfern Holidays"]


def _days(i: int, n: int) -> str:
    lines = []
    for d in range(1, n + 1):
        place = PLACES[(i + d) % len(PLACES)]
        act = ACTIVITIES[(i * 3 + d) % len(ACTIVITIES)]
        extra = ""
        if d % 3 == 0:
            extra = f"\nDistance: {12 + d * 3} km. Overnight: {place} guesthouse. Meals: breakfast, lunch."
        lines.append(f"Day {d}: Arrive in {place}\nThe group starts with {act} near {place}.{extra}")
    return "\n".join(lines)


def a1_inputs(n: int = 20) -> list[dict]:
    out = []
    for i in range(n):
        days = 2 + i % 9
        tour = {
            "name": f"{PLACES[i % len(PLACES)]} and {PLACES[(i + 3) % len(PLACES)]} — {days} Days",
            "subtitle": f"A {days}-day trip through {COUNTRIES[i % len(COUNTRIES)]}",
            "summary": f"This invented tour visits {PLACES[i % len(PLACES)]} over {days} days.",
            "description": f"Test description number {i} with {ACTIVITIES[i % len(ACTIVITIES)]}.",
            "highlights": [PLACES[(i + k) % len(PLACES)] for k in range(1 + i % 4)],
            "itineraries": _days(i, days),
            "country": COUNTRIES[i % len(COUNTRIES)],
            "duration": f"{days} days",
            "price": f"USD {900 + 37 * i}" if i % 2 else None,
            "inclusions": "Guide, entrance fees, meals as per itinerary" if i % 3 else "Guide",
            "exclusions": "International flights" if i % 4 else None,
        }
        agency = {"group_size": f"{2 + i % 5}-{10 + i % 5}" if i % 2 == 0 else None,
                  "best_time_to_go": "March to May" if i % 3 == 0 else None}
        forbidden = ["breathtaking", "nestled"] + (["curated"] if i % 2 else []) + (["Unique Gem"] if i % 5 == 0 else [])
        out.append({"input_id": f"a1-fake-{i:02d}", "source": "fake", "tour": tour, "tour_agency": agency,
                    "brand_rules": {"forbidden_words": forbidden, "rewrite_language": "en-US"}})
    return out


def _pack(i: int) -> dict:
    name = BRANDS[i % len(BRANDS)]
    return {
        "brand_name": name,
        "brand_type": ["family travel agency", "adventure operator", "slow travel studio", ""][i % 4],
        "core_idea": "Small groups that walk more than they ride" if i % 2 == 0 else "",
        "customer_segment": "Families with children aged six to twelve" if i % 3 == 0 else "Active couples",
        "customer_mindset": "Want unhurried days with one big moment" if i % 2 else "",
        "voice": [["warm", "plain"], ["energetic", "direct"], ["calm", "precise"], []][i % 4],
        "style_guide": "Short sentences. Name the place before the feeling." if i % 3 != 2 else "",
        "forbidden_words": ["epic", "hidden gem"] if i % 2 else ["unforgettable"],
        "good_examples": "We stop where the path bends and the valley opens." if i % 4 == 1 else "",
        "default_cta": f"Ask {name} for dates",
        "system_prompt": "Always mention that groups stay small." if i % 5 == 0 else "",
        "signature_words": ["trailhead"] if i % 4 == 2 else [],
    }


def t2_inputs(n: int = 20) -> list[dict]:
    out = []
    for i in range(n):
        days = 9 + i % 6
        pack = _pack(i)
        brand = {
            "system_prompt": pack["system_prompt"], "style_guide": pack["style_guide"],
            "forbidden_words": list(pack["forbidden_words"]),
            "rewrite_language": "en-GB" if i % 4 == 3 else "EN-US",
            "core_idea": pack["core_idea"], "customer_segment": pack["customer_segment"],
            "customer_mindset": pack["customer_mindset"], "voice_examples": list(pack["voice"]),
            "good_examples": pack["good_examples"], "brand_name": pack["brand_name"],
            "brand_type": pack["brand_type"], "default_cta": pack["default_cta"],
            "signature_words": list(pack["signature_words"]),
        }
        tour = {
            "name": f"{PLACES[i % len(PLACES)]} Loop — {days} Days", "subtitle": f"Through {COUNTRIES[i % 4]}",
            "summary": f"An invented master tour {i} across {PLACES[(i + 1) % len(PLACES)]}.",
            "description": f"Master description {i}. {ACTIVITIES[i % len(ACTIVITIES)].capitalize()}.",
            "highlights": [PLACES[(i + k) % len(PLACES)] for k in range(3)],
            "itineraries": _days(i + 7, days), "seo_title": f"{PLACES[i % len(PLACES)]} tour",
            "seo_meta": "An invented meta description for a test tour.", "country": COUNTRIES[i % 4],
            "duration": f"{days} days",
        }
        seo = {} if i % 3 == 0 else {
            "top_keywords": [f"{PLACES[i % len(PLACES)].lower()} tour", f"{COUNTRIES[i % 4].lower()} trek"],
            "keywords": {"top_keywords": [f"{PLACES[i % len(PLACES)].lower()} tour",
                                          f"{COUNTRIES[i % 4].lower()} trek"]},
            "people_also_ask": [f"When is the best time to visit {PLACES[i % len(PLACES)]}?"],
        }
        out.append({"input_id": f"t2-fake-{i:02d}", "source": "fake",
                    "tenant_id": f"00000000-0000-0000-0000-0000000009{i:02d}", "tour": tour, "brand_rules": brand,
                    "seo": seo})
    return out


_GOALS = ["promotion", "lead_generation", "conversion", "introduction_awareness", "trust_building",
          "engagement_conversation", "product_service_explanation"]


def _writer_input(i: int, channel: str) -> dict:
    pack = _pack(i)
    place = PLACES[i % len(PLACES)]
    wi = {
        "writer_prompt_version": "00000000", "seed": f"{place}: {ACTIVITIES[i % len(ACTIVITIES)]}. Location: "
                                                    f"{place}, {COUNTRIES[i % 4]}.",
        "route_moments": [], "facts": "", "scope": f"SCOPE: this piece covers {place} only." if i % 2 else None,
        "brand_pack": pack, "brand_rubric": None,
        "angle": {"name": ["Slow morning", "The one big moment", "Hands on", "Who it suits"][i % 4],
                  "why_it_works": "It matches what this audience asks for.", "formula_fit": "PAS",
                  "best_final_style": "Plain, specific, one image per paragraph."},
        "goal": _GOALS[i % len(_GOALS)], "channel": channel, "keyword": None, "cta": pack["default_cta"],
        "trip": f"{place} Loop — {9 + i % 3} Days", "destination": COUNTRIES[i % 4],
        "writer_trip": f"{place} Loop" if i % 2 else f"{place} Loop — {9 + i % 3} Days",
    }
    if i % 3 == 1:
        wi["facts"] = f"[Fact id=f{i}] The {place} museum opens daily except Mondays."
    if channel == "blog":
        wi["keyword"] = f"{place.lower()} walking tour" if i % 2 == 0 else None
        if i % 4 == 0:
            wi["route_moments"] = [{"atom_id": f"atom-{i}-a", "seed": f"{place}: morning walk."},
                                   {"atom_id": f"atom-{i}-b", "seed": f"{PLACES[(i + 1) % len(PLACES)]}: lunch stop."}]
        if i % 3 == 0:
            wi["search_questions"] = [{"question": f"Is {place} good for families?", "source": "place_paa"}]
    elif i % 3 == 2:
        wi["lead_focus"] = f"The {place} ferry runs twice a day"
    return wi


def t9_inputs(n: int = 20, *, blog: bool) -> list[dict]:
    social = ["linkedin", "facebook", "instagram", "linkedin", "email", "landing_page", "ads", "tiktok"]
    out = []
    for i in range(n):
        channel = "blog" if blog else social[i % len(social)]
        out.append({"input_id": f"t9-{'blog' if blog else 'social'}-fake-{i:02d}", "source": "fake",
                    "tenant_id": f"00000000-0000-0000-0000-0000000009{i:02d}",
                    "request_id": f"00000000-0000-0000-0000-0000000008{i:02d}", "atom_id": f"atom-{i}",
                    "stored_hash": None, "writer_input": _writer_input(i, channel)})
    return out


def all_inputs(n: int = 20) -> dict[str, list[dict]]:
    return {"a1": a1_inputs(n), "t2": t2_inputs(n), "t9_blog": t9_inputs(n, blog=True),
            "t9_social": t9_inputs(n, blog=False)}
