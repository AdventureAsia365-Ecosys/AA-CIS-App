"""AA-847 — geomatch.feature_kind (linear / area / point) and what locate makes of it: a linear feature is never a
pin — owner decision S246: precision 'line' (drawn from geo.place_shape), never hidden, no review reason; an area
feature is an area even when Geoapify confirms it.
"""
from __future__ import annotations

import pytest

from services.places import geomatch, locate
from services.places.geocoder import Confirmed


@pytest.mark.parametrize("name,result_type,kind", [
    ("Ho Chi Minh Trail", None, "linear"),
    ("Ho Chi Minh Trail", "street", "linear"),
    ("Mekong River", None, "linear"),
    ("Nam Ou River", None, "linear"),
    ("Thakhek Loop", None, "linear"),
    ("Annamite Range", None, "area"),          # S246: a range is an area, not a line a traveller follows
    ("Cheonggyecheon Stream", None, "linear"),
    ("Yamuna Expressway", None, "linear"),
    ("Annapurna Circuit Trek", None, "linear"),
    ("Druk Path Trek", None, "linear"),
    ("Bang Luang Canal", None, "linear"),
    ("Peradeniya Railway Station", None, None),
    ("Death Railway Museum", None, None),
    ("Chorten Kora", None, None),               # a temple, not a kora route
    ("Yangtze Cruise", None, None),             # an activity, not a feature word
    ("Sisavangvong Road", "street", "linear"),
    ("Phonsavan Street", "street", "linear"),
    ("Mekong Delta", None, "area"),
    ("Bolaven Plateau", None, "area"),
    ("Phong Nha-Ke Bang National Park", None, "area"),
    ("Nam Et-Phou Louey National Protected Area", None, "area"),
    ("Hai Van Pass", None, None),
    ("Kuang Si Falls", None, None),
    ("River Kwai Bridge", None, None),
    ("Ho Chi Minh Trail Museum", None, None),
    ("Mekong River Lodge", None, None),
    ("Luang Prabang", "city", None),
    ("Night Market", "street", None),
    ("Ho Chi Minh City", "city", None),
])
def test_feature_kind(name, result_type, kind):
    assert geomatch.feature_kind(name, result_type) == kind


def _item(name):
    return locate.Item(("k", "laos", ""), name, "Laos")


def test_confirmed_linear_feature_is_a_line_not_hidden():
    res = Confirmed(16.46, 107.2, "Vietnam", "pid", "geoapify", {"provider": "geoapify", "result_type": "street"})
    p = locate._located(locate.Item(("k", "vietnam", ""), "Ho Chi Minh Trail", "Vietnam"), 16.5, 107.1, "Vietnam",
                        res)
    assert p.located_by == "geoapify" and p.precision == "line" and p.place_id == "pid"
    assert p.review_reason is None and not hasattr(p, "hidden")


def test_confirmed_area_feature_is_an_area_not_hidden():
    res = Confirmed(15.1, 106.3, "Laos", "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})
    p = locate._located(_item("Bolaven Plateau"), 15.1, 106.3, "Laos", res)
    assert p.precision == "area" and p.review_reason is None


def test_confirmed_point_stays_a_point():
    res = Confirmed(19.749, 101.993, "Laos", "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})
    p = locate._located(_item("Kuang Si Falls"), 19.75, 101.99, "Laos", res)
    assert p.precision == "point" and p.kind is None


def test_model_point_of_a_linear_feature_is_a_line_without_review():
    p = locate._located(_item("Mekong River"), 19.9, 102.1, "Laos", None)
    assert p.located_by == "llm" and p.precision == "line" and p.review_reason is None
    # a hard linear name ("Hai Van Pass Road" says pass) is not a pin review either: its shape is searched by name
    hard = locate._located(_item("Hai Van Pass Road"), 16.2, 108.1, "Vietnam", None)
    assert hard.precision == "line" and hard.review_reason is None
    temple = locate._located(_item("Wat Xieng Thong temple"), 19.9, 102.1, "Laos", None)
    assert temple.review_reason == locate.REVIEW_UNCONFIRMED_HARD and temple.precision == "area"
