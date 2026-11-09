"""AA-1055 (S274) — the pure parts of the read-only research quality report (scripts/aa1055/research_quality.py).

The queries run against the real schema in tests/integration/test_aa1055_research_quality_db.py. Here: the volume
agreement maths, the sample cap and line shape, the JOB id check, and that the script only reads (no write
statement, a READ ONLY session, no provider client). Invented names only.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts.aa1055 import research_quality as rq

SCRIPT = Path(rq.__file__)


def test_volume_agreement_counts_equal_near_and_median():
    got = rq.volume_agreement([(50, 50), (110, 100), (0, 0), (300, 100)])
    assert got == {"n": 4, "equal_share": "0.500", "near_share": "0.750", "median_rel_diff": "0.050"}
    assert rq.volume_agreement([]) == {"n": 0, "equal_share": "null", "near_share": "null",
                                       "median_rel_diff": "null"}
    assert rq.rel_diff(5, 0) == 5.0                     # a live 0 compares against 1


def test_agreement_is_per_market_and_needs_the_same_market():
    bought = [{"keyword": "zarvok lake", "market": "US", "search_volume": 40},
              {"keyword": "zarvok lake", "market": "UK", "search_volume": 10},
              {"keyword": "melnith gorge", "market": "US", "search_volume": 7}]
    live = {("zarvok lake", "US"): (40, None), ("melnith gorge", "AU"): (7, None)}
    got = rq.agreement_by_market(bought, live)
    assert list(got) == ["US"] and got["US"]["n"] == 1 and got["US"]["equal_share"] == "1.000"


@pytest.mark.parametrize("raw,n", [(None, 30), ("", 30), ("12", 12), ("500", 30), ("-3", 0), ("x", 30)])
def test_sample_never_exceeds_thirty(raw, n):
    assert rq.sample_size(raw) == n


def test_sample_line_keeps_three_fields():
    assert rq.sample_line("zarvok|lake\nhike", "Zarvok Lake", False) == "zarvok/lake hike|Zarvok Lake|kept"
    assert rq.sample_line("lake hotels", "Zarvok Lake", True).endswith("|rejected")


def test_job_id_must_be_a_uuid():
    assert rq.job_id_from(" 6434F55C-0000-4000-8000-000000000001 ") == "6434f55c-0000-4000-8000-000000000001"
    for bad in (None, "", "6434f55c"):
        with pytest.raises(SystemExit):
            rq.job_id_from(bad)


def test_script_only_reads():
    src = SCRIPT.read_text(encoding="utf-8")
    sql = " ".join(v.upper() for k, v in vars(rq).items() if k.endswith("_SQL") and isinstance(v, str))
    assert sql and not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b", sql)
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in src
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert not names & {"DataForSEOClient", "LLMClient", "decide", "enqueue"}


def test_metric_lines_print_no_keyword_text(capsys):
    rq.out("volume.US.bought_keywords", 3)
    rq.out("serp.US.paa_ge3_share", rq.share(1, 4))
    assert capsys.readouterr().out == "RQ volume.US.bought_keywords=3\nRQ serp.US.paa_ge3_share=0.250\n"
    assert rq.share(1, 0) == "null" and rq.money(None) == "0.0000"
