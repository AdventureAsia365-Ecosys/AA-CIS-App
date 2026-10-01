"""S207 — forbidden words match whole words (plus plural s), not substrings, everywhere validate,
flag_fix, seo_meta and the review queue check them."""
from services.content_generation.forbidden_words import forbidden_in, has_word
from services.content_generation.seo_meta_utils import meta_has_forbidden


def test_no_substring_false_positives_from_the_1709_run():
    for text, word in [("depicts the lord buddha", "epic"), ("the epicenter of", "epic"),
                       ("ideal growing conditions", "deal"), ("the fundamental ingredients", "fun"),
                       ("a village fund", "fun"), ("discovered in 1976", "discover")]:
        assert not has_word(text, word), (text, word)


def test_real_uses_still_fire():
    for text, word in [("an epic trek", "epic"), ("great deals", "deal"), ("a fun day", "fun"),
                       ("explores the old town", "explore"), ("book now!", "book now"),
                       ("world-class food", "world-class")]:
        assert has_word(text, word), (text, word)


def test_forbidden_in_and_meta_check_use_the_same_rule():
    assert forbidden_in({"summary": "ideal for fundamental history"}, ["deal", "fun"]) == set()
    assert forbidden_in({"summary": "a fun deal"}, ["deal", "fun"]) == {"deal", "fun"}
    assert not meta_has_forbidden("Dormant volcano views on a premium Jeju walking tour.", ["dorm"])
    assert meta_has_forbidden("Budget-friendly dorm stays.", ["dorm"])
