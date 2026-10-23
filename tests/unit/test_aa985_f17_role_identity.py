"""AA-985 — F17_trip_fact 'ended role': role identity, not place identity.

S248 live check (AA-984): blog piece 4b58f52c (Bhutan, request 3c9dc7bb) was held on F17_trip_fact —
"says it still holds a role the source dates as ended (the capital and seat of government of Bhutan,
until 1955)" — for "Like all dzongs, it houses both monk bodies and government offices for district
administration". The seed dates only the national role as ended; the district administration the
dzong houses is a different role it still holds. The rule fires only when the sentence claims the
SAME role the seed dates as ended.
"""
import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import trip_facts as tf

# the shape of the Bhutan seed the held piece read (one moment, the national role dated as ended)
DZONG_SEED = """Punakha Dzong — visit courtyards and assembly hall
Location: Punakha Dzong, Punakha, Punakha District, Bhutan
Day 4 of the trip
Source itinerary: Visit Punakha Dzong's courtyards and assembly hall.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan, who founded it to serve as the administrative and religious centre of the country.
- Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital was moved to Thimphu.
- It remains the winter residence of the Je Khenpo and the Central Monk Body, who relocate here from Thimphu each year."""

HELD_SENTENCE = ("Like all dzongs, it houses both monk bodies and government offices for district "
                 "administration.")


def _ctx(seed=DZONG_SEED):
    return tf.TripFactContext(seed=seed, audience=None, cta=None)


class TestTheHeldShape:
    def test_the_held_sentence_is_not_flagged(self):
        assert tf.date_authority_claims(HELD_SENTENCE, DZONG_SEED) == []

    def test_the_gate_passes_the_held_sentence(self):
        f17, _f18 = qg.gate_trip_facts(f"Punakha Dzong sits where two rivers meet. {HELD_SENTENCE}", _ctx())
        assert f17["passed"], f17

    @pytest.mark.parametrize("sentence", [
        HELD_SENTENCE,
        "The dzong still houses government offices alongside the monastic body.",
        "Today it houses the offices of the Punakha district administration and the monastic body.",
        "It remains the seat of government for the district and the winter home of the monk body.",
        "It is still the administrative centre of Punakha District.",
        "Government officials and monks still share its courtyards.",
        "Punakha remains Bhutan's spiritual capital in winter.",
        "It still serves as the dzongkhag administration's headquarters.",
    ])
    def test_a_different_current_role_is_not_the_ended_role(self, sentence):
        assert tf.date_authority_claims(sentence, DZONG_SEED) == []


class TestTheEndedRoleStillFires:
    @pytest.mark.parametrize("sentence", [
        "It is still the capital of Bhutan.",
        "Punakha Dzong remains the seat of government today.",
        "Today it remains Bhutan's administrative centre.",
        "It still houses Bhutan's government offices alongside the monk body.",
        "It still houses the national government and the district administration.",
        "The founding purpose, religious and governmental in one, remains present.",
        "It remains the capital of Bhutan and the seat of the district administration.",
    ])
    def test_a_claim_of_the_same_role_fires(self, sentence):
        out = tf.date_authority_claims(sentence, DZONG_SEED)
        assert out and "until 1955" in out[0], sentence

    def test_a_meta_claiming_the_role_still_warns(self):
        meta = "Punakha Dzong remains Bhutan's administrative center today, where two rivers meet."
        out = tf.date_authority_claims("", DZONG_SEED, extra_texts=(("meta", meta),))
        assert out and out[0].startswith("meta ")


class TestRoleClaim:
    def test_role_identity_unit(self):
        role = tf.ended_roles(DZONG_SEED)[0]
        assert tf.role_claim("it is still the capital", "Bhutan", role)
        assert not tf.role_claim("it houses government offices for district administration", "Bhutan", role)
        assert not tf.role_claim("it remains the cultural capital", "Bhutan", role)
        assert tf.role_claim("it houses Bhutan's government offices", "Bhutan", role)

    def test_a_local_ended_role_matches_a_local_claim(self):
        seed = DZONG_SEED.replace(
            "- Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital "
            "was moved to Thimphu.",
            "- The fortress served as the district administrative centre until 1990, when the offices moved to "
            "Lobesa.")
        role = tf.ended_roles(seed)[0]
        assert "district" in role.role
        assert tf.date_authority_claims("It still houses the district administration.", seed)
        assert tf.date_authority_claims("It remains the national capital's pride.", seed) == []


class TestSpanChecksOnlyTheRole:
    def test_a_span_of_the_local_role_is_not_checked_against_the_national_dates(self):
        assert tf.date_authority_claims(
            "For nearly four centuries it has housed the district administration.", DZONG_SEED) == []

    def test_a_span_of_the_national_role_is_still_checked(self):
        assert tf.date_authority_claims("For nearly three centuries it served as Bhutan's capital.", DZONG_SEED)
