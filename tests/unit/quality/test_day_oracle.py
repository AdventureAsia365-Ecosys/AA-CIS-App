"""AA-925 (Q-2) — the S1 day-count oracle (services/quality/day_oracle.py), pure function tables."""
import pytest

from services.quality import day_oracle as do

SRC4 = "Day 1: arrive in Paro\nDay 2: hike to Taktsang\nDay 3: drive to Punakha\nDay 4: depart"


def _out(*days):
    """Canonical itinerary text from (day, title) pairs."""
    return "\n\n".join(f"Day {d} — {t}\nBody of the day." for d, t in days)


CLEAN4 = _out((1, "Paro"), (2, "Taktsang"), (3, "Punakha"), (4, "Departure"))

CASES = [
    # id, source, duration, output, codes that must fire, codes that must not
    ("clean", SRC4, "4 Days / 3 Nights", CLEAN4, set(), set(do.DAY_ORACLE_MODES)),
    ("subday-split", "Ride the cable car up and back.", "3 hours", _out((1, "Up"), (2, "Ridge"), (3, "Down")),
     {do.DAY_SUBDAY_SPLIT}, {do.DAY_OUTPUT_EXTRA, do.DAY_COUNT_VS_DURATION}),
    ("subday-one-day-ok", "Ride the cable car up and back.", "3 hours", _out((1, "Cable car")), set(),
     set(do.DAY_ORACLE_MODES)),
    ("duplicate", SRC4, "4 days", _out((1, "a"), (2, "b"), (2, "b"), (3, "c"), (4, "d")),
     {do.DAY_OUTPUT_DUPLICATE}, {do.DAY_COUNT_VS_DURATION}),
    ("order", SRC4, "4 days", _out((1, "a"), (3, "c"), (2, "b"), (4, "d")), {do.DAY_OUTPUT_ORDER}, set()),
    ("gap", SRC4, "4 days", _out((1, "a"), (2, "b"), (4, "d")), {do.DAY_OUTPUT_GAP, do.DAY_COUNT_VS_DURATION},
     set()),
    ("source-gap-not-charged", "Day 1: a\nDay 2: b\nDay 4: d", "3 days", _out((1, "a"), (2, "b"), (4, "d")),
     set(), {do.DAY_OUTPUT_GAP, do.DAY_OUTPUT_EXTRA}),
    ("extra", SRC4, "4 days", _out((1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e")),
     {do.DAY_OUTPUT_EXTRA, do.DAY_COUNT_VS_DURATION}, set()),
    ("departure-midtrip", SRC4, "4 days", _out((1, "a"), (2, "Departure from Paro"), (3, "c"), (4, "d")),
     {do.DAY_DEPARTURE_MIDTRIP}, set()),
    ("departure-last-ok", SRC4, "4 days", CLEAN4, set(), {do.DAY_DEPARTURE_MIDTRIP}),
    ("source-contradiction", SRC4, "14 Days", CLEAN4, {do.SOURCE_DAY_CONTRADICTION}, {do.DAY_COUNT_VS_DURATION}),
    ("range-markers", "Days 1–2: valley\nDay 3: pass\nDay 4: home", "4 days", CLEAN4, set(),
     set(do.DAY_ORACLE_MODES)),
    ("no-markers-duration-ok", "A single walk through the old town.", "1 day", _out((1, "Old town")), set(),
     set(do.DAY_ORACLE_MODES)),
    ("no-markers-extra-vs-duration", "A walk in the old town, then the river.", "2 days",
     _out((1, "a"), (2, "b"), (3, "c")), {do.DAY_OUTPUT_EXTRA, do.DAY_COUNT_VS_DURATION}, set()),
    ("skipped", "Prose with no day markers.", "on request", _out((1, "a"), (2, "b")), {do.DAY_CHECK_SKIPPED},
     set(do.DAY_ORACLE_MODES) - {do.DAY_CHECK_SKIPPED}),
    ("range-duration-no-count-check", SRC4, "3–5 days", CLEAN4, set(), {do.DAY_COUNT_VS_DURATION,
                                                                       do.SOURCE_DAY_CONTRADICTION}),
    ("no-output-days", SRC4, "4 days", "", set(), set(do.DAY_ORACLE_MODES)),
]


@pytest.mark.parametrize("cid,src,duration,output,must,must_not", CASES, ids=[c[0] for c in CASES])
def test_oracle(cid, src, duration, output, must, must_not):
    codes = {f["code"] for f in do.check(src, duration, output)}
    assert must <= codes, f"{cid}: missing {must - codes}, got {codes}"
    assert not (codes & (must_not - must)), f"{cid}: unexpected {codes & (must_not - must)}"


def test_findings_carry_class_mode_and_detail():
    (f,) = [f for f in do.check(SRC4, "14 Days", CLEAN4)]
    assert f["class_id"] == "source.contradiction" and f["mode"] == "shadow" and f["detector"] == "day_oracle"
    assert "14" in f["detail"]
    for g in do.check(SRC4, "4 days", _out((1, "a"), (1, "a"), (2, "b"), (3, "c"), (4, "d"))):
        assert g["class_id"] == "struct.day_structure"


def test_every_code_ships_in_shadow_and_never_enforced_codes_stay_out():
    assert set(do.DAY_ORACLE_MODES.values()) <= {"shadow", "enforce"}
    assert not (do.enforced_codes() & do.NEVER_ENFORCED)


def test_enforced_codes_become_a1_hard_codes():
    """Flipping a code to enforce makes it an A1 hard code with a declared repair path (S218 guard) and a
    rewrite-class code. Run in a fresh interpreter: graph and admin_pipeline read the modes at import."""
    import os
    import subprocess
    import sys

    code = (
        "from services.quality import day_oracle as do\n"
        "do.DAY_ORACLE_MODES[do.DAY_OUTPUT_DUPLICATE] = 'enforce'\n"
        "from services.content_generation import graph as g\n"
        "assert do.DAY_OUTPUT_DUPLICATE in g._HARD_BLOCK_CODES\n"
        "assert g.HARD_CODE_REPAIR[do.DAY_OUTPUT_DUPLICATE].startswith('llm:')\n"
        "assert do.SOURCE_DAY_CONTRADICTION not in g._HARD_BLOCK_CODES\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ},
                         cwd=os.getcwd())
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-2000:]


def test_quality_module_imports_no_writer_module():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(do))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "re", "typing", "services.content_generation.day_parse",
                    "services.content_generation.prompts"}, mods


def test_s248_day_list_marker_is_a_range_not_missing_days():
    # golden s1-004: "Day 05 & 06" / "Day 11, 12 & 13" read as days 5 and 11 only, so a faithful day 6/12/13 was
    # charged as DAY_OUTPUT_EXTRA
    src = "Day 04: A – B\nRide.\nDay 05 & 06: B – C\nRide.\nDay 07: C – D\nRide.\nDay 08, 09 & 10: D – E\nBeach."
    out = "\n\n".join(f"Day {n} — Stage {n}\nRide on." for n in range(4, 11))
    codes = [f["code"] for f in do.check(src, "10 days", out)]
    assert "DAY_OUTPUT_EXTRA" not in codes
