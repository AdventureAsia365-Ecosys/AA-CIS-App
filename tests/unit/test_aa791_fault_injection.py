"""AA-791 — the 30-piece T9 fault-injection set (tests/unit/aa791_t9_fault_set.py) in CI: every rare
gate catches each of its planted faults, and passes its clean twin. No LLM, no DB."""
import pytest

from tests.unit import aa791_t9_fault_set as fs


def test_set_is_30_faults_over_10_rare_gates_3_each():
    assert len(fs.FAULT_CASES) == 30
    assert len({c.case_id for c in fs.FAULT_CASES + fs.CLEAN_CASES}) == 40
    assert len(fs.RARE_GATES) == 10
    for gate in fs.RARE_GATES:
        assert sum(c.gate == gate for c in fs.FAULT_CASES) == 3, gate
        assert sum(c.gate == gate for c in fs.CLEAN_CASES) == 1, gate


@pytest.mark.parametrize("case", fs.FAULT_CASES, ids=lambda c: c.case_id)
def test_planted_fault_is_caught(case):
    with fs.judges_pass():
        result = fs.run_case(case)
    assert result["target"] is not None, f"{case.gate} did not run for channel {case.channel}"
    assert not result["target"]["passed"], f"{case.case_id} ({case.fault}) not caught"
    assert result["target"]["violations"]
    assert result["target"]["blocking"] is True  # every gate in the set is a blocking gate
    assert result["passed"] is False and result["first_failure"] is not None


@pytest.mark.parametrize("case", fs.CLEAN_CASES, ids=lambda c: c.case_id)
def test_clean_twin_passes_its_gate(case):
    with fs.judges_pass():
        result = fs.run_case(case)
    assert result["target"] is not None
    assert result["target"]["passed"], f"{case.case_id}: {result['target']['violations']}"


def test_recall_table_reports_full_recall():
    rows = fs.recall_table()
    assert [r["gate"] for r in rows] == fs.RARE_GATES
    assert all(r["caught"] == r["planted"] == 3 and r["clean_passed"] == r["clean_twins"] for r in rows)
