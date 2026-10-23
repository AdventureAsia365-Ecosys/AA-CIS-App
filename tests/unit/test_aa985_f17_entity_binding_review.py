"""AA-985 round 2, review round — F17_trip_fact 'ended role': entity binding, the reviewer's failing sentences.

The review of the round 2 entity binding found seven gaps, each a set of sentences that either fired on something
other than the entity (false positives) or went silent on a real claim #955 still caught (lost true positives):
1. a government subject was bound by its verb's membership in a list, not by a locative relation to the entity;
2. a pronoun after a paragraph naming another place in one plain word ('Hanoi', 'Vientiane') fell back to the
   entity;
3. an appositive split off at ', still' lost its noun phrase;
4. the moment's own name ('Imperial City') was treated as another place when the seed's role line names the city;
5. a contrast lead ('Unlike Mongar Dzong,') became the pronoun's antecedent;
6. aliases were matched case- and accent-sensitively ('Huế', 'Kyōto', all caps, all lower case);
7. the moment's Naming aliases were ignored when the role line has a proper-noun subject.
"""
import pytest

from services.acp_content_writing import trip_facts as tf
from tests.unit.test_aa985_f17_role_identity import DZONG_SEED

_BG = ("Place background (checked facts about the place itself — you may use these; they say nothing about what "
       "this trip does):")


def _seed(label: str, location: str, role_line: str) -> str:
    return "\n".join([label, f"Location: {location}", "Day 2 of the trip", "Source itinerary: Visit the place.", _BG,
                      f"- {role_line}"])


# city-subject seeds: the role line names the city, the moment is a monument in it; Hue, Mandalay, Luang Prabang
# and Ayutthaya have no 'moved to X' successor
HUE_SEED = _seed("Imperial City — walk the citadel", "Imperial City, Hue, Thua Thien Hue Province, Vietnam",
                 "Hue served as the capital of Vietnam until 1945.")
MANDALAY_SEED = _seed("Mandalay Palace — palace walls", "Mandalay Palace, Mandalay, Myanmar",
                      "Mandalay served as the royal capital of Myanmar until 1885.")
LUANG_PRABANG_SEED = _seed("Royal Palace Museum — morning visit", "Royal Palace Museum, Luang Prabang, Laos",
                           "Luang Prabang served as the royal capital of Laos until 1975.")
AYUTTHAYA_SEED = _seed("Ayutthaya Historical Park — temple ruins", "Ayutthaya Historical Park, Ayutthaya, Thailand",
                       "Ayutthaya served as the capital of the Siamese kingdom until 1767.")
KYOTO_SEED = _seed("Kyoto Imperial Palace — garden walk", "Kyoto Imperial Palace, Kyoto, Japan",
                   "Kyoto served as the imperial capital of Japan until 1869, when the capital moved to Tokyo.")
NAMING_SEED = DZONG_SEED.replace("Location: Punakha Dzong, Punakha, Punakha District, Bhutan",
                                 "Location: Punakha Dzong, Punakha, Punakha District, Bhutan\n"
                                 "Naming: also called 'Pungthang Dewachen Phodrang'")

SEEDS = {"dzong": DZONG_SEED, "hue": HUE_SEED, "mandalay": MANDALAY_SEED, "luang_prabang": LUANG_PRABANG_SEED,
         "ayutthaya": AYUTTHAYA_SEED, "kyoto": KYOTO_SEED, "naming": NAMING_SEED}


def _fires_seed(text: str, seed: str) -> bool:
    return bool(tf.date_authority_claims(text, seed))


def _fires(text: str, seed_key: str, heading: str = "") -> bool:
    body = f"## {heading}\n\n{text}" if heading else text
    return bool(tf.date_authority_claims(body, SEEDS[seed_key]))


# ------------------------------------------------------------------ finding 1: government subject, no verb list

GOV_FALSE_POSITIVES = [
    ("dzong", "Punakha Dzong sits where two rivers meet. The government still maintains the dzong as a national "
              "monument."),
    ("dzong", "Punakha Dzong sits where two rivers meet. The government still protects it as a heritage site."),
    ("dzong", "Punakha Dzong sits where two rivers meet. The government still restricts photography inside."),
    ("dzong", "Punakha Dzong sits where two rivers meet. Bhutan's government still requires visitors to wear formal "
              "dress here."),
    ("dzong", "Government ministers still attend the Punakha Tshechu each spring."),
    ("dzong", "The government still subsidises the upkeep of Punakha Dzong."),
    ("dzong", "Bhutan's government still invests in restoring Punakha Dzong after the 1994 flood."),
    ("dzong", "The national government still sponsors festivals at Punakha Dzong."),
    ("dzong", "The capital still sends officials to Punakha Dzong every winter."),
    ("kyoto", "The national government still funds the palace gardens."),
    ("luang_prabang", "The government still regulates building heights in the old town."),
]

GOV_TRUE_POSITIVES = [
    ("dzong", "The national government still occupies the dzong."),
    ("dzong", "The government still governs Bhutan from the dzong."),
    ("dzong", "Punakha Dzong sits where two rivers meet. The government still governs Bhutan from these halls."),
    ("dzong", "Bhutan's government still meets inside Punakha Dzong."),
    ("dzong", "Punakha Dzong sits where two rivers meet. The government still sits here."),
    ("dzong", "Punakha Dzong sits where two rivers meet. The government is still housed in the dzong."),
    ("dzong", "The seat of Bhutan's government is still Punakha Dzong."),
    ("dzong", "Inside Punakha Dzong the government of Bhutan still meets."),
]


class TestGovernmentSubjectNeedsALocativeRelation:
    @pytest.mark.parametrize("seed_key,text", GOV_FALSE_POSITIVES)
    def test_a_government_doing_something_to_the_place_is_silent(self, seed_key, text):
        assert not _fires(text, seed_key), text

    def test_a_getting_there_section_is_silent(self):
        assert not _fires("It is still a three-hour drive from the capital, and the government still maintains the "
                          "road.", "dzong", heading="Getting there")

    @pytest.mark.parametrize("seed_key,text", GOV_TRUE_POSITIVES)
    def test_a_government_located_in_the_entity_fires(self, seed_key, text):
        assert _fires(text, seed_key), text

    def test_the_national_government_elsewhere_is_silent(self):
        assert not _fires("The national government still occupies Tashichho Dzong.", "dzong")
        assert not _fires("The government still governs Bhutan from Thimphu.", "dzong")


# ------------------------------------------------------------------ finding 2: a one-word other place is a mention

OTHER_PLACE_FIRST = [
    ("hue", "Hanoi lies 700 km to the north. It is the capital of Vietnam today."),
    ("hue", "Hanoi lies 700 km to the north. Today it remains the seat of Vietnam's government."),
    ("mandalay", "Naypyidaw, a planned city, became the capital in 2005. Today it remains the seat of Myanmar's "
                 "government."),
    ("luang_prabang", "Vientiane sits on the Mekong too. Today it is the capital of Laos."),
    ("luang_prabang", "Vientiane sits on the Mekong too. It remains the seat of the Lao government."),
    ("ayutthaya", "Bangkok replaced Thonburi in 1782. It remains the capital of Thailand today."),
    ("ayutthaya", "Bangkok replaced Thonburi in 1782. Today the city is still the seat of the Thai government."),
    ("dzong", "Tashichho sits on the Wang Chhu. Today it is the seat of Bhutan's government."),
]


class TestAnotherPlaceInOneWordIsTheAntecedent:
    @pytest.mark.parametrize("seed_key,text", OTHER_PLACE_FIRST)
    def test_a_pronoun_after_another_place_is_silent(self, seed_key, text):
        assert not _fires(text, seed_key), text

    @pytest.mark.parametrize("seed_key,text", [
        ("hue", "Hue sits on the Perfume River. It is still the capital of Vietnam."),
        ("mandalay", "Mandalay lies on the Irrawaddy. Today it remains the seat of Myanmar's government."),
        ("ayutthaya", "Zhabdrung visited Ayutthaya once. It remains the capital of Thailand today."),
        ("dzong", "Zhabdrung Ngawang Namgyal founded Punakha Dzong in 1637. It remains the seat of Bhutan's "
                  "government today."),
    ])
    def test_a_pronoun_after_the_entity_still_fires(self, seed_key, text):
        assert _fires(text, seed_key), text

    def test_a_person_or_a_common_word_does_not_count_as_a_place(self):
        # 'Visitors' is a common word: with nothing else named, the pronoun is the entity
        assert _fires("Visitors often ask about the walls. It remains the seat of Bhutan's government.", "dzong")

    @pytest.mark.parametrize("line,successor", [
        ("Ayutthaya served as the capital of Siam until 1767, when Thonburi became the capital.", "Thonburi"),
        ("Hue served as the capital of Vietnam until 1945, when it was replaced by Hanoi.", "Hanoi"),
        ("Mandalay served as the royal capital until 1885; the capital is now Naypyidaw.", "Naypyidaw"),
        ("Punakha Dzong served as the capital of Bhutan until 1955, when the capital was moved to Thimphu.",
         "Thimphu"),
    ])
    def test_the_successor_has_more_shapes(self, line, successor):
        seed = _seed("Old Palace — visit", "Old Palace, Somewhere, Asia", line)
        assert tf.ended_roles(seed)[0].successor == successor


# ------------------------------------------------------------------ finding 3: ', still …' is an appositive

class TestAnAppositiveAfterTheName:
    @pytest.mark.parametrize("seed_key,text", [
        ("dzong", "Punakha Dzong, still the capital of Bhutan, sits where two rivers meet."),
        ("luang_prabang", "Luang Prabang, still the royal capital of Laos, sits on the Mekong."),
        ("dzong", "Discover Punakha Dzong, still the seat of Bhutan's government."),
    ])
    def test_the_appositive_binds_to_the_name_before_it(self, seed_key, text):
        assert _fires(text, seed_key), text

    @pytest.mark.parametrize("seed_key,text", [
        ("dzong", "Tashichho Dzong, still the seat of Bhutan's government, sits in Thimphu."),
        ("luang_prabang", "Vientiane, still the capital of Laos, sits on the Mekong."),
    ])
    def test_an_appositive_to_another_place_is_silent(self, seed_key, text):
        assert not _fires(text, seed_key), text


# ------------------------------------------------------------------ finding 4: the moment's own name is the entity

class TestTheMomentsOwnNameWithACitySubject:
    @pytest.mark.parametrize("seed_key,text", [
        ("hue", "The Imperial City is still the seat of Vietnam's government."),
        ("luang_prabang", "The Royal Palace Museum is still the seat of the Lao government."),
        ("kyoto", "The Imperial Palace in Kyoto is still the seat of Japan's government."),
        ("luang_prabang", "The Royal Palace of Luang Prabang is still the seat of the Lao government."),
    ])
    def test_a_claim_about_the_moment_fires(self, seed_key, text):
        assert _fires(text, seed_key), text

    def test_the_moment_names_are_aliases(self):
        role = tf.ended_roles(HUE_SEED)[0]
        assert role.entity == "Hue"
        assert {"Hue", "Imperial City"} <= set(role.aliases)
        assert "Imperial" not in role.aliases  # a bare generic adjective is not a name
        b = tf._binder(HUE_SEED, role, tf._seed_country(HUE_SEED))
        assert "Imperial" not in b.other_names

    def test_a_moment_that_is_not_the_role_holder_adds_no_alias(self):
        """A Thimphu moment whose background dates Punakha's role: the moment's names stay other places."""
        seed = _seed("Tashichho Dzong — visit", "Tashichho Dzong, Thimphu, Thimphu District, Bhutan",
                     "Punakha Dzong served as the capital of Bhutan until 1955, when the capital was moved to Thimphu.")
        role = tf.ended_roles(seed)[0]
        assert "Tashichho Dzong" not in role.aliases
        assert not _fires_seed("Tashichho Dzong is still the seat of Bhutan's government.", seed)


# ------------------------------------------------------------------ finding 5: a contrast lead is not the antecedent

class TestAContrastLeadIsNotTheAntecedent:
    @pytest.mark.parametrize("text", [
        "Punakha Dzong sits where two rivers meet. Unlike Mongar Dzong, it remains the seat of Bhutan's government.",
        "Punakha Dzong sits where two rivers meet. After visiting Mongar Dzong, you will see it is still the seat of "
        "Bhutan's government.",
        "Unlike Thimphu, it is still the capital of Bhutan.",
    ])
    def test_the_pronoun_skips_the_lead(self, text):
        assert _fires(text, "dzong"), text

    def test_a_successor_in_the_main_clause_is_still_skipped(self):
        assert not _fires("In 1955 the capital moved to Thimphu, where it remains today.", "dzong")
        assert not _fires("Today Thimphu is Bhutan's capital.", "dzong")


# ------------------------------------------------------------------ finding 6: case and accents

class TestAliasesIgnoreCaseAndAccents:
    @pytest.mark.parametrize("seed_key,text", [
        ("hue", "Huế is still the capital of Vietnam."),
        ("kyoto", "Kyōto is still the capital of Japan."),
        ("dzong", "PUNAKHA DZONG IS STILL THE CAPITAL OF BHUTAN."),
        ("dzong", "punakha dzong is still the capital of bhutan."),
    ])
    def test_the_entity_fires_in_any_spelling(self, seed_key, text):
        assert _fires(text, seed_key), text

    def test_a_common_word_spelled_like_a_short_alias_is_not_the_alias(self):
        b = tf._binder(HUE_SEED, tf.ended_roles(HUE_SEED)[0], "Vietnam")
        assert b.alias_spans("The hue of the river changes at dusk.") == []
        assert b.alias_spans("Huế sits on the river.") == [(0, 3)]

    def test_a_longer_name_that_starts_with_the_alias_is_another_place(self):
        b = tf._binder(DZONG_SEED, tf.ended_roles(DZONG_SEED)[0], "Bhutan")
        assert b.alias_spans("The Punakha Tshechu draws crowds.") == []
        assert b.alias_spans("Punakha Dzong and Punakha town") == [(0, 13), (18, 25)]


# ------------------------------------------------------------------ finding 7: Naming aliases with a proper subject

class TestNamingAliasesWithAProperSubject:
    def test_the_naming_alias_fires(self):
        assert "Pungthang Dewachen Phodrang" in tf.ended_roles(NAMING_SEED)[0].aliases
        assert _fires("Pungthang Dewachen Phodrang is still the capital of Bhutan.", "naming")


# ------------------------------------------------------------------ derived while fixing the review findings

class TestDerivedCases:
    @pytest.mark.parametrize("seed_key,text", [
        # a definite NP ends at its government head ('the national government works …')
        ("dzong", "Punakha Dzong sits where two rivers meet. Today the national government works from its halls."),
        ("dzong", "The government of Bhutan still resides in Punakha Dzong."),
        # an appositive after a full clause describes that clause's subject
        ("dzong", "Punakha Dzong sits where two rivers meet, still the seat of Bhutan's government."),
        ("hue", "Hue's Imperial City is still the seat of Vietnam's government."),
        ("dzong", "Bhutan's capital is still Punakha."),
        # a person subject that talks about 'it' carries the topic on: 'it' is still the entity
        ("dzong", "Zhabdrung founded it in 1637. It remains the seat of Bhutan's government today."),
    ])
    def test_fires(self, seed_key, text):
        assert _fires(text, seed_key), text

    @pytest.mark.parametrize("seed_key,text", [
        ("dzong", "The government still uses the dzong for ceremonies."),
        ("dzong", "The capital is still a three-hour drive from the dzong."),
        ("dzong", "The government still sits in Thimphu, two hours from Punakha Dzong."),
        ("dzong", "The road from Punakha climbs to the capital, still the seat of government."),
        ("dzong", "Thimphu sits in a wide valley, still the seat of Bhutan's government."),
        ("dzong", "The Punakha Tshechu is still the capital of Bhutan's festival calendar."),
        # the known successor as the previous sentence's subject is the antecedent, even with the entity named later
        ("kyoto", "Tokyo replaced Kyoto in 1869. It is the capital of Japan today."),
        ("hue", "The hue of the citadel walls is still the capital's pride."),
    ])
    def test_silent(self, seed_key, text):
        assert not _fires(text, seed_key), text
