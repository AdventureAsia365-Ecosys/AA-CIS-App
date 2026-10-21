"""AA-925 (Q-2) — the Q0 corpus: labelled cases run through the production chain (tests/unit/quality/chain.py).

Truth labels are binding: a case whose detector flips away from its ``expected`` label fails, and there is
no file or flag that overrides it. Changing or deleting an existing case's label needs the ``q0-relabel``
PR label (.github/scripts/q0_red_on_base.py). Every NEW case must fail on the base branch tip (red on
base), which the same script checks in CI. The pytest id of each case is its ``id``.
"""
import re

import pytest

from tests.unit.quality import chain

CASES = chain.load_cases()
FIXTURES = chain.load_fixtures()
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case):
    result = chain.run_case(case, FIXTURES)
    found = chain.problems(case, result)
    assert not found, f"{case['id']} ({case['class_id']}): " + "; ".join(found)


def test_corpus_shape():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    by_id = {c["id"]: c for c in CASES}
    for c in CASES:
        missing = [k for k in chain.REQUIRED if k not in c]
        assert not missing, f"{c.get('id')}: missing {missing}"
        assert _ID_RE.match(c["id"]), f"{c['id']}: ids are lowercase slugs (pytest node ids)"
        assert c["chain"] in chain.CHAINS, c["id"]
        assert c["expected"] in chain.EXPECTED, c["id"]
        assert c["severity"] in chain.SEVERITIES, c["id"]
        assert "." in c["class_id"], f"{c['id']}: class_id is a dotted slug (design §3.1)"
        if c.get("red_on_base") is False:
            assert c["expected"] in ("no_fire", "keep"), f"{c['id']}: only a negative twin may skip red-on-base"
            assert c.get("twin_of") in by_id, f"{c['id']}: twin_of must name a case"
        base = (c["input"] or {}).get("base") if isinstance(c["input"], dict) else None
        if base:
            assert base in FIXTURES, f"{c['id']}: unknown fixture {base!r}"


def test_every_gate_case_points_at_a_live_aa791_case():
    from tests.unit import aa791_t9_fault_set as fs

    known = {c.case_id for c in fs.FAULT_CASES + fs.CLEAN_CASES}
    refs = [c["input"]["aa791_case"] for c in CASES if c["chain"] == "gates"]
    assert set(refs) <= known
    assert len(refs) == len(set(refs)) == len(known), "every AA-791 case is labelled exactly once"
