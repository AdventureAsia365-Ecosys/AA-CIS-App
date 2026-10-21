"""AA-847 — geomatch.feature_kind (linear / area / point) and what locate makes of it: a linear feature is never a
pin (precision area, review_reason linear_feature, hidden), an area feature is an area even when Geoapify confirms it.
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
    ("Annamite Range", None, "linear"),
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


def test_confirmed_linear_feature_is_hidden_area():
    res = Confirmed(16.46, 107.2, "Vietnam", "pid", "geoapify", {"provider": "geoapify", "result_type": "street"})
    p = locate._located(locate.Item(("k", "vietnam", ""), "Ho Chi Minh Trail", "Vietnam"), 16.5, 107.1, "Vietnam",
                        res)
    assert p.located_by == "geoapify" and p.precision == "area" and p.hidden
    assert p.review_reason == locate.REVIEW_LINEAR_FEATURE


def test_confirmed_area_feature_is_an_area_not_hidden():
    res = Confirmed(15.1, 106.3, "Laos", "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})
    p = locate._located(_item("Bolaven Plateau"), 15.1, 106.3, "Laos", res)
    assert p.precision == "area" and not p.hidden and p.review_reason is None


def test_confirmed_point_stays_a_point():
    res = Confirmed(19.749, 101.993, "Laos", "pid", "geoapify", {"provider": "geoapify", "result_type": "amenity"})
    p = locate._located(_item("Kuang Si Falls"), 19.75, 101.99, "Laos", res)
    assert p.precision == "point" and not p.hidden and p.kind is None


def test_model_point_of_a_linear_feature_is_flagged_linear_not_hard():
    p = locate._located(_item("Mekong River"), 19.9, 102.1, "Laos", None)
    assert p.located_by == "llm" and p.precision == "area" and p.hidden
    assert p.review_reason == locate.REVIEW_LINEAR_FEATURE
    temple = locate._located(_item("Wat Xieng Thong temple"), 19.9, 102.1, "Laos", None)
    assert temple.review_reason == locate.REVIEW_UNCONFIRMED_HARD and not temple.hidden
