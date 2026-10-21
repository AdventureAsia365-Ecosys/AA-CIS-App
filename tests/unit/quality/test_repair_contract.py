"""AA-925 (Q-2) — the repair contract's five invariants (services/quality/repair_contract.py)."""
import pytest

from services.quality import repair_contract as rc

SOURCE = "Day 3: Drive over Dochula Pass to Punakha and visit Punakha Dzong. The last day has a 20-kilometre walk."


def test_unchanged_fields_pass():
    before = {"summary": "A tour of Bhutan."}
    assert rc.check(before, dict(before)).ok


def test_gates_kept_reverts_a_field_whose_repair_fails_a_passed_gate():
    def gate(_field, text):
        return {"FORBIDDEN_WORD:explore"} if "explore" in text.lower() else set()

    before = {"seo_meta": "Walk the valleys of Bhutan.", "summary": "Explore Paro."}
    after = {"seo_meta": "Explore the valleys of Bhutan.", "summary": "Explore Paro and Punakha."}
    kept, res = rc.enforce_fields(before, after, gate_runner=gate, source="Paro and Punakha")
    # summary already failed the gate before: its repair did not make it worse, so it is kept
    assert kept == {"seo_meta": "Walk the valleys of Bhutan.", "summary": "Explore Paro and Punakha."}
    assert res.invariants() == ["gates_kept"] and res.fields() == ["seo_meta"]


def test_new_figure_is_reverted():
    before = {"summary": "Cross Dochula Pass to Punakha."}
    after = {"summary": "Cross the 3,100-metre Dochula Pass to Punakha."}
    kept, res = rc.enforce_fields(before, after, source=SOURCE)
    assert kept == before and res.invariants() == ["no_new_specifics"]


def test_a_figure_the_source_gives_is_allowed():
    before = {"summary": "End with a long walk."}
    after = {"summary": "End with a 20-kilometre walk."}
    assert rc.check(before, after, source=SOURCE).ok


def test_new_name_is_reverted_unless_known():
    before = {"summary": "Visit the dzong and the river confluence."}
    assert not rc.check(before, {"summary": "Visit the dzong with Tashi Tours and the river confluence."},
                        source=SOURCE).ok
    assert rc.check(before, {"summary": "Visit Punakha Dzong and the river confluence."}, source=SOURCE).ok
    # names=False (flag_fix): a background name is allowed
    assert rc.check(before, {"summary": "Visit the dzong with Tashi Tours and the river confluence."},
                    source=SOURCE, names=False).ok


def test_cut_signature_catches_the_s2_shape():
    before = ("Four days from Paro to Punakha, crossing Dochula Pass and ending with a 20-kilometre walk along "
              "the river.")
    after = "Four days from Paro to Punakha, crossing Dochula Pass and ending with a 20-kilometre."
    assert rc.cut_signature(before, after)
    _, res = rc.enforce_fields({"seo_meta": before}, {"seo_meta": after}, source=SOURCE)
    assert res.invariants() == ["no_truncation"]


def test_cut_signature_catches_a_modifier_left_without_its_noun():
    assert rc.cut_signature("Walk the old town with expert guides and a driver.", "Walk the old town with expert.")
    assert rc.cut_signature("Start with a two-day trek to the lakes.", "Start with a two-day.")
    # a number compound never ends a phrase, even before a phrase opener
    assert rc.cut_signature("End with a 20-kilometre and quiet walk.", "End with a 20-kilometre.")


@pytest.mark.parametrize("before, after", [
    # the A1/T3 fact editor's "drop the unsupported detail": a trailing clause goes, the sentence stays whole
    ("We visit Paro Dzong and hike for 3 hours to the monastery.", "We visit Paro Dzong."),
    # an LLM meta trim before a phrase opener (AA-794 accepted it)
    ("Four days in western Bhutan from Paro to Punakha with local guides and quiet lodges.",
     "Four days in western Bhutan from Paro to Punakha with local guides."),
    # a dropped trailing adverbial: the kept text does not end like a cut
    ("Lunch is at the viewpoint above the valley every day.", "Lunch is at the viewpoint above the valley."),
    # a predicative modifier before a clause opener
    ("Our guides are local and know every trail.", "Our guides are local."),
])
def test_a_dropped_trailing_clause_is_not_a_cut(before, after):
    assert rc.cut_signature(before, after) is None
    assert rc.check({"summary": "This tour covers Bhutan. " + before},
                    {"summary": "This tour covers Bhutan. " + after}).ok
    assert rc.span_problems(before, after) == []


def test_sentence_boundary_salvage_is_not_a_cut():
    before = "Four days in western Bhutan from Paro to Punakha. The pace suits first visits."
    after = "Four days in western Bhutan from Paro to Punakha."
    assert rc.cut_signature(before, after) is None
    assert rc.check({"seo_meta": before}, {"seo_meta": after}).ok


@pytest.mark.parametrize("after", ["Walk the river path with", "Walk the river path…", "Walk the river path"])
def test_incomplete_ending_is_a_truncation(after):
    res = rc.check({"summary": "Walk the river path."}, {"summary": after})
    assert res.invariants() == ["no_truncation"]


def test_an_ending_that_was_already_incomplete_is_not_charged():
    assert rc.check({"highlights": ["Punakha Dzong", "Dochula Pass"]},
                    {"highlights": ["Punakha Dzong at the confluence", "Dochula Pass"]}).ok


def test_scope_kept():
    _, res = rc.enforce_fields({"summary": "A.", "subtitle": "B."}, {"summary": "A2.", "subtitle": "B2."},
                               allowed={"summary"})
    assert res.fields() == ["subtitle"] and res.invariants() == ["scope_kept"]


def test_tags_and_brackets_balanced():
    assert not rc.balanced("A dzong (built in the past. [R:atom_1]")
    assert not rc.balanced("**Day one** and **day two")
    res = rc.check({"summary": "A dzong (the old one)."}, {"summary": "A dzong (the old one."})
    assert res.invariants() == ["tags_balanced"]


def test_span_contract_keeps_the_original_sentence():
    items = [{"sentence": "The walk follows the river."}, {"sentence": "Lunch is at the viewpoint."}]
    keep, reps, reverted = rc.enforce_spans(items, ["The walk follows the", "Lunch is by the viewpoint."])
    assert [i["sentence"] for i in keep] == ["Lunch is at the viewpoint."] and reps == ["Lunch is by the viewpoint."]
    assert reverted[0]["guard"] == "contract" and reverted[0]["contract"]
    # a cut (empty replacement) is never a truncation
    keep, _, reverted = rc.enforce_spans([{"sentence": "The walk follows the river."}], [""])
    assert keep and not reverted


def test_new_specifics_is_re_exported_by_sentence_repair():
    from services.acp_content_writing import sentence_repair

    assert sentence_repair.new_specifics is rc.new_specifics
    assert rc.new_specifics("The hike is steep.", "The 3-hour hike is steep.", source="") != []
    assert rc.new_specifics("The hike is steep.", "") == []


def test_contract_imports_no_writer_module():
    """Load-time imports of services/quality stay pure (circular-import guard, design review)."""
    import ast
    import inspect

    for mod in (rc, __import__("services.quality.degraded", fromlist=["x"])):
        tree = ast.parse(inspect.getsource(mod))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {getattr(n, "module", None) or n.names[0].name for n in top}
        assert not any(m and (m.startswith("services.content_generation") or m.startswith("services.acp_content")
                              or m.startswith("services.acp_produce")) for m in names), names
