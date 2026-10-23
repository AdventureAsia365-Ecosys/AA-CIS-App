"""AA-985 round 2 — F17_trip_fact 'ended role': entity binding.

PR #955 fixed role identity (which role a clause claims). The S248 live census after its deploy still found the
rule firing on 12 distinct sentences (20 stored hits, 4 pieces of request 3c9dc7bb), all false positives: they
claim nothing about the entity whose role the seed dates as ended (Punakha Dzong, the capital and seat of
government of Bhutan until 1955). They are statements about the class ('A dzong is …', 'In Bhutan, dzongs house
…', 'Like all dzongs, …'), about other places ('Kuenselphodrang, on a forested hillside above the capital',
'Khaling, where the National Handloom Development Corporation operates a government-run weaving centre') or about
other subjects ('The weavers at the government looms'). The rule now fires only when the clause's subject is the
entity: its name or an alias, or a singular pronoun / definite NP whose antecedent is the entity.
"""
import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import trip_facts as tf
from tests.unit.test_aa985_f17_role_identity import DZONG_SEED

# the shape of the multi-moment blog seed of request 3c9dc7bb: the ended role sits in the Punakha Dzong moment,
# other moments name the current capital and other places the writer also covers
TRIP_SEED = """Thimphu — Buddha Dordenma at Kuenselphodrang
Location: Kuenselphodrang, Thimphu, Thimphu District, Bhutan
Day 2 of the trip
Source itinerary: Visit the Buddha Dordenma statue at Kuenselphodrang above Thimphu.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- Thimphu has been the capital of Bhutan since 1955.

Punakha Dzong — visit courtyards and assembly hall
Location: Punakha Dzong, Punakha, Punakha District, Bhutan
Day 4 of the trip
Source itinerary: Visit Punakha Dzong's courtyards and assembly hall.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- It was built in 1637–1638 by Zhabdrung Ngawang Namgyal, the unifier of Bhutan, who founded it to serve as the administrative and religious centre of the country.
- Punakha Dzong served as the capital and seat of government of Bhutan until 1955, when the capital was moved to Thimphu.
- It remains the winter residence of the Je Khenpo and the Central Monk Body, who relocate here from Thimphu each year.

Mongar Dzong — courtyard visit
Location: Mongar Dzong, Mongar, Mongar District, Bhutan
Day 8 of the trip
Source itinerary: Visit Mongar Dzong.

Khaling — weaving centre
Location: Khaling, Trashigang District, Bhutan
Day 10 of the trip
Source itinerary: Visit the weaving centre at Khaling."""

SEEDS = pytest.mark.parametrize("seed", [DZONG_SEED, TRIP_SEED], ids=["dzong_seed", "trip_seed"])

# every stored hit of the S248 census (aa985-census.out, all_stored_hits, in its order), verbatim; the two
# Kuenselphodrang hits are stored cut at 300 characters (trip_facts._quoted) and are kept as stored
STORED_HITS = [
    "Like all dzongs in Bhutan, it houses both monk bodies and government offices for the district administration, "
    "the two functions that have always shared the same walls.",
    "A dzong is a fortress-monastery that functions simultaneously as the administrative headquarters and religious "
    "centre of a Bhutanese district.",
    "The dual role, government offices alongside monk bodies, continues in active dzongs today.",
    "The dzongs still house government offices and monk bodies.",
    "Their dual administrative and religious function reflects Bhutan's integration of civic and spiritual "
    "governance.",
    "Dzongs in Bhutan serve as fortress-monasteries that function as both administrative and religious centres for "
    "their districts.",
    "They house monk bodies and government offices simultaneously, embodying Bhutan's integration of spiritual and "
    "civic life.",
    "A dzong is a fortress-monastery that serves simultaneously as an administrative headquarters and a religious "
    "centre in Bhutan.",
    "From Dewathang, the route continues through eastern Bhutan's Trashigang District to Khaling, where the National "
    "Handloom Development Corporation operates a government-run weaving centre.",
    "Mongar Dzong houses both monk bodies and government offices for the district administration, exactly as the "
    "older western dzongs do, built centuries before it.",
    "Kuenselphodrang, on a forested hillside above the capital, is the site of the Buddha Dordenma: a gilded bronze "
    "statue of Shakyamuni Buddha standing approximately 51.5 metres tall, built to commemorate the centenary of the "
    "Wangchuck dynasty and the 60th birthday of the Fourth Druk Gyalpo, Jigme Singy",
    "A dzong is a fortress-monastery that serves simultaneously as an administrative headquarters and a religious "
    "centre in Bhutan.",
    "The word is Dzongkha for this distinct building type, which houses both government offices for the district "
    "administration and a resident monk body.",
    "The weavers at the government looms in Khaling still produce kishuthara on backstop frames, as women in eastern "
    "Bhutan have done for generations.",
    "Kuensel Phodrang Nature Park, on a forested hillside above the capital, is the site of the Buddha Dordenma: a "
    "gilded bronze statue of Shakyamuni Buddha standing approximately 51.5 metres tall, built to commemorate the "
    "centenary of the Wangchuck dynasty and the 60th birthday of the Fourth Druk Gyalpo",
    "Like all dzongs, it houses both monk bodies and government offices for district administration, fulfilling a "
    "dual function that has defined these structures across the centuries.",
    "Like all dzongs, it houses both monk bodies and government offices for the district administration, fulfilling "
    "the same dual function that Zhabdrung Ngawang Namgyal embedded in the form when he began building these "
    "structures in the 17th century.",
    "Dzongs in Bhutan serve a dual civic and religious purpose, housing both monk bodies and government offices for "
    "district administration.",
    "In Bhutan, dzongs house government offices alongside monk bodies, fulfilling a dual civic and spiritual role.",
    "Like all dzongs, it houses both monk bodies and government offices for the district administration, fulfilling "
    "the same dual function that has defined these structures across the centuries.",
]
assert len(STORED_HITS) == 20


def _ctx(seed):
    return tf.TripFactContext(seed=seed, audience=None, cta=None)


def _binder(seed=DZONG_SEED):
    role = tf.ended_roles(seed)[0]
    return tf._binder(seed, role, tf._seed_country(seed))


class TestTheCensusHits:
    @SEEDS
    @pytest.mark.parametrize("n", range(20))
    def test_no_stored_hit_fires(self, n, seed):
        assert tf.date_authority_claims(STORED_HITS[n], seed) == [], STORED_HITS[n]

    @pytest.mark.parametrize("n", range(20))
    def test_entity_binding_alone_rejects_every_stored_hit(self, n):
        """Defence in depth: whatever the role-identity layer decides, none of these clauses is about the entity."""
        b = _binder()
        assert not any(tf.binds_entity(c, "", None, b) for _, c in tf._clauses(STORED_HITS[n])), STORED_HITS[n]

    @SEEDS
    def test_the_hits_as_one_body_do_not_hold_the_piece(self, seed):
        body = "\n\n".join(["## Punakha Dzong", "Punakha Dzong sits where two rivers meet. " + STORED_HITS[15],
                            "## The dzongs", " ".join(STORED_HITS[1:8]),
                            "## The east", " ".join(STORED_HITS[8:15]), " ".join(STORED_HITS[16:])])
        f17, _f18 = qg.gate_trip_facts(body, _ctx(seed))
        assert f17["passed"], f17


class TestTheEntityStillFires:
    @SEEDS
    @pytest.mark.parametrize("text", [
        "Punakha Dzong is still the capital of Bhutan.",
        "Punakha remains the seat of government.",
        "Punakha Dzong stands where the Pho Chhu and Mo Chhu meet. Today the dzong is the seat of Bhutan's government.",
        "Punakha Dzong stands where the Pho Chhu and Mo Chhu meet. It still houses the government of the kingdom.",
        # derived
        "We reach Punakha Dzong, which is still the seat of Bhutan's government.",
        "Bhutan's government still meets inside Punakha Dzong.",
        "Punakha Dzong sits where two rivers meet. Here the national government still meets.",
        "Unlike other dzongs, Punakha Dzong is still the seat of the national government.",
        "Punakha Dzong sits where two rivers meet. Its role as the seat of government continues today.",
        "Punakha Dzong's role as the seat of government continues today.",
        "Bhutan's oldest great dzong, Punakha Dzong, is still the seat of government.",
        "In winter the dzong is still the capital of Bhutan.",
        "Zhabdrung Ngawang Namgyal founded Punakha Dzong in 1637. It remains the seat of Bhutan's government today.",
        "## Punakha Dzong\n\nThe fortress rises between two rivers. It remains the seat of government.",
        "## Punakha Dzong\n\nIt remains the seat of government.",
        "Mongar Dzong was built in 1953. Punakha Dzong, however, remains the capital of Bhutan.",
        "Punakha Dzong is a fortress-monastery. Inside, the government of Bhutan still sits.",
        "Punakha Dzong sits where two rivers meet. We enter the dzong, which is still the seat of Bhutan's government.",
        "Visitors rarely realise it is still the seat of government.",
        "Government offices for the district still fill the dzong, and it remains the seat of Bhutan's government.",
        "Punakha Dzong sits where two rivers meet.\n\n- Still the seat of Bhutan's government",
    ])
    def test_a_claim_about_the_entity_fires(self, text, seed):
        out = tf.date_authority_claims(text, seed)
        assert out and "until 1955" in out[0], text

    @SEEDS
    def test_a_meta_appositive_still_warns(self, seed):
        out = tf.date_authority_claims("", seed, extra_texts=(("meta", "Discover Punakha Dzong, still the seat of "
                                                                         "Bhutan's government."),))
        assert out and out[0].startswith("meta ")

    def test_a_span_about_the_entity_is_still_checked(self):
        out = tf.date_authority_claims("For nearly three centuries Punakha Dzong served as Bhutan's capital.",
                                       DZONG_SEED)
        assert out and "gives the span as" in out[0]


class TestOtherSubjectsDoNotFire:
    @SEEDS
    @pytest.mark.parametrize("text", [
        # the current capital, by name, by pronoun, by heading
        "Thimphu has been Bhutan's capital since 1955. Today it is the seat of government.",
        "## Thimphu\n\nIt remains the seat of Bhutan's government.",
        # another named place or subject
        "Mongar Dzong still houses the seat of government for the east.",
        "The capital's dzong, Tashichho Dzong, still houses the throne room and the government.",
        "Bhutan's largest dzong, Tashichho Dzong, is still the seat of government.",
        "The National Assembly still meets in Thimphu.",
        "The king still holds court in the capital.",
        "The road from Punakha climbs to the capital, which is still the seat of government.",
        "Punakha Dzong sits where two rivers meet. Thimphu took over in 1955. It is still the seat of government.",
        # the capital as a place on the map
        "Punakha Dzong is today a three-hour drive from the capital.",
        "Kuenselphodrang still looks down on the capital.",
        # class statements
        "Every dzong still houses the district administration and its government offices.",
        "Like all dzongs, Punakha Dzong still houses the district administration and its monk body.",
        "These fortresses remain the seat of each district's government.",
    ])
    def test_a_sentence_about_something_else_is_silent(self, text, seed):
        assert tf.date_authority_claims(text, seed) == [], text

    def test_a_span_about_another_place_is_not_checked_against_the_entitys_dates(self):
        assert tf.date_authority_claims("For nearly three centuries Trongsa Dzong served as the seat of the "
                                        "Wangchuck kings.", DZONG_SEED) == []


class TestTheEntity:
    def test_the_entity_is_the_proper_subject_of_the_seed_line(self):
        role = tf.ended_roles(TRIP_SEED)[0]
        assert role.entity == "Punakha Dzong"
        assert set(role.aliases) == {"Punakha Dzong", "Punakha"}

    def test_a_pronoun_seed_line_binds_the_moment(self):
        seed = DZONG_SEED.replace("- Punakha Dzong served as", "- It served as")
        role = tf.ended_roles(seed)[0]
        assert role.entity == "Punakha Dzong" and "Punakha" in role.aliases
        assert tf.date_authority_claims("Punakha is still the capital of Bhutan.", seed)
        assert tf.date_authority_claims("Kuenselphodrang is still the capital of Bhutan.", seed) == []

    def test_the_binder_sees_other_moments_and_the_successor(self):
        b = _binder(TRIP_SEED)
        kinds = [(m[2], m[3]) for m in b.mentions("From Mongar Dzong to Punakha Dzong and on to Thimphu")]
        assert kinds == [("other", "Mongar Dzong"), ("entity", "Punakha Dzong"), ("other", "Thimphu")]
        assert b.mentions("The Dzong is old.") == []
