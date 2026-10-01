"""S207 Korea wave — the A1 judge and the brand audit must see every itinerary day (was [:600] / [:300])."""
from services.content_generation.brand_fit import _build_judge_prompt, itinerary_digest


def _long(days=15, words=120):
    return "\n\n".join(f"Day {i} — Place {i}\n" + "word " * words for i in range(1, days + 1))


def test_digest_keeps_every_day_and_is_bounded():
    out = itinerary_digest(_long())
    assert all(f"Day {i} — Place {i}" in out for i in range(1, 16))
    assert len(out) <= 6100


def test_digest_accepts_day_list_and_json():
    days = [{"day": 1, "title": "Seoul", "body": "x"}, {"day": 2, "title": "Busan", "body": "y"}]
    assert "Day 2 — Busan" in itinerary_digest(days)
    import json
    assert "Day 2 — Busan" in itinerary_digest(json.dumps(days))


def test_digest_caps_total_for_very_long_tours():
    out = itinerary_digest(_long(days=60), per_day=350, total=6000)
    assert out.endswith("[later days omitted for length]") and len(out) < 6100


def test_judge_prompt_shows_the_last_day():
    p = _build_judge_prompt({"brand_core_idea": "x"}, {"name": "T", "itineraries": _long(days=12)})
    assert "Day 12 — Place 12" in p


def test_audit_prompt_uses_digest():
    import inspect
    from services.content_generation import brand_audit_node
    src = inspect.getsource(brand_audit_node)
    assert "itinerary_digest(generated.get(\"itineraries\")" in src
    assert "[:300]" not in src.split("AA_ITINERARIES")[1][:120]
