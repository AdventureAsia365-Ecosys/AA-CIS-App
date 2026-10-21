"""AA-847 (S246) — geomatch.linked_feature: a descriptive atom place → the linear feature it is about, measured on a
labelled set (lesson AA-779 / 125: count the false links first, then the missed ones).

Labels: `link` (the component must link to that feature's destination), `generic` (no proper feature: stays a generic
phrase), `point` (a point on or by the feature: stays a point), `area` (an area along it), `self` (already the
feature: nothing to link). Most phrases are atom places or itinerary fragments of the live catalog (CIS calibration
data docs/calibration/data/*.json, S234/S246), the rest are the design's examples; author-labelled.
"""
from __future__ import annotations

import pytest

from services.places import geomatch

LINK = [
    ("sandbank on the Nam Ou River", "Nam Ou River"),
    ("sandbank on Nam Ou River", "Nam Ou River"),
    ("Gesashi River mangrove kayaking", "Gesashi River"),
    ("Li River near Yangshuo", "Li River"),
    ("Yangtze River cruise", "Yangtze River"),
    ("Mekong River Cruise", "Mekong River"),
    ("Annapurna Circuit Trek", "Annapurna Circuit"),
    ("Manaslu Circuit Trek", "Manaslu Circuit"),
    ("Druk Path Trek", "Druk Path"),
    ("Kanchanaburi River Kwai", "River Kwai"),
    ("slow boat on the Mekong River", "Mekong River"),
    ("Nam Ou River boat journey from Nong Khiaw", "Nam Ou River"),
    ("Nam Song River tubing", "Nam Song River"),
    ("rocky section of the Ho Chi Minh Trail", "Ho Chi Minh Trail"),
    ("walk a section of the Olle Trail", "Olle Trail"),
    ("Hike the 10km Pingxi Historic Trail", "Pingxi Historic Trail"),
    ("Riding along Sumida River", "Sumida River"),
    ("Crossing Sumida River", "Sumida River"),
    ("Kilimanjaro Trekking Via Machame Route", "Machame Route"),
    ("Nam Lik River lunch stop", "Nam Lik River"),
    ("kayaking on the Nam Khan River", "Nam Khan River"),
    ("Trishuli River Rafting", "Trishuli River"),
    ("Punakha River rafting", "Punakha River"),
    ("Manas River catch-and-release fishing", "Manas River"),
    ("Mandovi River sunset cruise", "Mandovi River"),
    ("Han River bike ride", "Han River"),
    ("Nakdong River bike path", "Nakdong River"),
    ("Jeju Olle Trail section", "Jeju Olle Trail"),
    ("trek a section of the Jeju Olle Trail", "Jeju Olle Trail"),
    ("Bangla Road bar crawl", "Bangla Road"),
    ("bar crawl on Patong's Bangla Road", "Patong's Bangla Road"),
    ("Qinghai-Tibet Railway train ride", "Qinghai-Tibet Railway"),
    ("Death Railway train ride", "Death Railway"),
    ("drive along the Karakoram Highway", "Karakoram Highway"),
    ("Kathmandu-Pokhara Highway drive", "Kathmandu-Pokhara Highway"),
    ("Yamuna Expressway drive", "Yamuna Expressway"),
    ("Cheonggyecheon Stream walk", "Cheonggyecheon Stream"),
    ("evening stroll along Cheonggyecheon Stream", "Cheonggyecheon Stream"),
    ("Thakhek Loop motorbike ride", "Thakhek Loop"),
    ("Ha Giang Loop by motorbike", "Ha Giang Loop"),
    ("rowing boat trip up the Ngo Dong River", "Ngo Dong River"),
    ("Sekong River boat ride", "Sekong River"),
    ("Chomphet Loop gravel ride", "Chomphet Loop"),
    ("Ella Rock Trail walk", "Ella Rock Trail"),
    ("Tea Horse Trail heritage walk", "Tea Horse Trail"),
    ("Silk Road caravan stop", "Silk Road"),
    ("Pekoe Trail Stage 2", "Pekoe Trail"),
    ("cruise the Mekong River", "Mekong River"),
    ("sunset walk along the Mekong River", "Mekong River"),
    ("Luang Prabang – Chomphet Loop to Kiao Niao Farmstead", "Chomphet Loop"),
]
NONE = [
    # generic: no proper feature
    ("the river", "generic"),
    ("river valleys", "generic"),
    ("the main road south from Luang Prabang", "generic"),
    ("riverside path", "generic"),
    ("Jungle River Trekking Adventure", "generic"),
    ("Mangrove River kayaking", "generic"),
    ("a scenic mountain road", "generic"),
    ("the old railway line", "generic"),
    ("rocky section of trail", "generic"),
    ("forest trail", "generic"),
    ("coastal road", "generic"),
    # a point on / by the feature
    ("bridge over the Nam Ou River", "point"),
    ("Death Railway Museum", "point"),
    ("Peradeniya Railway Station", "point"),
    ("boat pier on the Mekong River", "point"),
    ("River Kwai Bridge", "point"),
    ("Ho Chi Minh Trail Museum", "point"),
    ("Sekong River rapids near Stung Treng", "point"),
    ("Mekong River Lodge", "point"),
    ("village along the Nam Ou River", "point"),
    ("Kanchanaburi War Cemetery near the River Kwai", "point"),
    ("Wat Phou along the Mekong River", "point"),
    ("Luang Prabang on the Mekong River", "point"),
    ("Pakse on the Mekong River", "point"),
    ("Muang Ngoi by the Nam Ou River", "point"),
    ("Nong Khiaw overlooking the Nam Ou River", "point"),
    # an area along it
    ("Betwa River valley, Orchha", "area"),
    ("Nam Ou River valley", "area"),
    ("Mekong Delta river tour", "area"),
    # already the feature
    ("Nam Ou River", "self"),
    ("Ho Chi Minh Trail", "self"),
    ("Mekong River", "self"),
    ("Karakoram Highway", "self"),
    ("Patong's Bangla Road", "self"),
    ("Annapurna Circuit", "self"),
    ("Mekong River (Laos)", "self"),
]


def test_labelled_set_size():
    assert len(LINK) + len(NONE) >= 60


@pytest.mark.parametrize("phrase,label", NONE)
def test_no_false_link(phrase, label):
    assert geomatch.linked_feature(phrase) is None, f"{label} phrase linked"


@pytest.mark.parametrize("phrase,feature", LINK)
def test_links_to_the_feature(phrase, feature):
    assert geomatch.linked_feature(phrase) == feature


def test_measured_rates():
    """The numbers reported in the AA-847 lines notes: false links (the costly error) and missed links."""
    false_links = [p for p, _ in NONE if geomatch.linked_feature(p) is not None]
    wrong = [p for p, f in LINK if geomatch.linked_feature(p) not in (None, f)]
    missed = [p for p, _ in LINK if geomatch.linked_feature(p) is None]
    assert (len(false_links), len(wrong), len(missed)) == (0, 0, 0)


@pytest.mark.parametrize("name,cls", [
    ("Mekong River", "water"), ("Cheonggyecheon Stream", "water"), ("Bang Luang Canal", "water"),
    ("Karakoram Highway", "road"), ("Death Railway", "road"), ("Yamuna Expressway", "road"),
    ("Ho Chi Minh Trail", "trail"), ("Annapurna Circuit", "trail"), ("Druk Path", "trail"), ("Ha Giang Loop", "trail"),
    ("Tea Horse", "trail"),
])
def test_line_class(name, cls):
    assert geomatch.line_class(name) == cls
