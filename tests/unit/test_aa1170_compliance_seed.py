"""AA-1170 (S287) — CIS 466: the requirement catalogue and the cis.compliance.* settings, read from the migration file
(no DB).

1. The catalogue lists every requirement of each in-scope framework (counts per framework, unique ids), in our own
   words: a label is at most 80 characters, never reads like normative text ("shall", "must", "the organization") and is
   not a bare copy of another label. The standards' text is copyrighted; only identifiers are theirs (tech lead S287).
2. The eight settings are compiled with the seeded defaults, a plain label and a complete EN + VI guide.
3. No tracked file of this repo carries a control id of the private register (the register is loaded from a private
   file, never committed; examples use the invented EX- prefix)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from api.services import compliance_settings as keys
from shared import settings

REPO = Path(__file__).resolve().parents[2]
M466 = REPO / "api" / "migrations" / "466_aa1170_compliance_schema.sql"
EXPECTED = {"iso27001:2022": 123, "soc2:2017": 33, "iso42001:2023": 70, "owasp-top10:2021": 10, "owasp-llm:2025": 10,
            "eu-ai-act:2024": 7}
NORMATIVE = re.compile(r"\b(shall|must|the organi[sz]ation)\b", re.I)


def _catalogue() -> list[dict]:
    text = M466.read_text(encoding="utf-8")
    body = text.split("$catalogue$", 2)[1]
    return json.loads(body)


def test_every_requirement_of_each_framework_is_listed_once():
    rows = _catalogue()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["framework"]] = counts.get(r["framework"], 0) + 1
    assert counts == EXPECTED
    assert len({(r["framework"], r["ref"]) for r in rows}) == len(rows) == 253
    annex = {r["ref"] for r in rows if r["framework"] == "iso27001:2022" and r["ref"].startswith("A.")}
    assert len(annex) == 93 and {"A.5.1", "A.5.37", "A.6.8", "A.7.14", "A.8.34"} <= annex
    soc = [r["ref"] for r in rows if r["framework"] == "soc2:2017"]
    assert soc[0] == "CC1.1" and soc[-1] == "CC9.2"
    assert {r["ref"] for r in rows if r["framework"] == "eu-ai-act:2024"} >= {"Art.4", "Art.50(2)"}
    for fw in EXPECTED:
        orders = [r["sort_order"] for r in rows if r["framework"] == fw]
        assert orders == list(range(1, len(orders) + 1)), fw


def test_labels_are_our_own_short_wording():
    rows = _catalogue()
    for r in rows:
        title = r["title"]
        assert 3 <= len(title) <= 80, r
        assert not NORMATIVE.search(title), r
        assert title[0].isupper() and not title.endswith("."), r
    titles = [r["title"].lower() for r in rows]
    # one label per requirement inside a framework (copying a heading for many ids would hide the wording)
    for fw in EXPECTED:
        per = [r["title"].lower() for r in rows if r["framework"] == fw]
        assert len(per) == len(set(per)), fw
    assert titles


def test_the_eight_settings_are_compiled_with_the_seeded_defaults():
    expected = {
        "cis.compliance.validity_hours.root_daily": (72, "medium", "maker_checker"),
        keys.HEARTBEAT_HOURS: (36, "medium", "maker_checker"),
        keys.PARTIAL_WEIGHT: (0.5, "medium", "maker_checker"),
        keys.DRILL_MAX_AGE_DAYS: (90, "medium", "maker_checker"),
        keys.ACCESS_REVIEW_MAX_AGE_DAYS: (100, "medium", "maker_checker"),
        keys.DOCS_OVERDUE_MAX: (0, "low", "maker_checker"),
        keys.CI_SCAN_MAX_AGE_DAYS: (8, "medium", "maker_checker"),
    }
    # AA-1174 (CIS 471) added keys of its own (tests/unit/test_aa1174_obligations_seed.py); the 466 eight stay as seeded
    assert set(keys.ALL_466) == set(expected) | {keys.REFERENCE_SCORES}
    assert keys.VALIDITY_HOURS["root_daily"] == "cis.compliance.validity_hours.root_daily"
    for key, (default, impact, approval) in expected.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced) == ("cis", "compliance", "platform", True), key
        assert d.default_value == default and d.impact == impact and d.approval == approval and d.label, key
    ref = settings.definition(keys.REFERENCE_SCORES)
    assert ref.value_type == "json" and ref.default_value["iso27001:2022"] == 45.8
    assert set(ref.default_value) <= set(EXPECTED)
    for key in keys.ALL:
        guide = settings.guide(key)
        for lang in ("en", "vi"):
            assert all(guide[lang][part] for part in ("what", "raise", "lower", "when", "example")), (key, lang)
    for check_id, (fact, key, kind) in keys.FACT_LIMITS.items():
        assert key in keys.ALL and kind in ("age_days", "max") and fact and check_id
    # the image scan is graded on the date of its oldest newest scan (a CI scan that stopped running fails)
    assert keys.FACT_LIMITS["ci_image_scan_sbom"] == ("last_date", keys.CI_SCAN_MAX_AGE_DAYS, "age_days")


def test_no_private_control_id_is_tracked():
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    pattern = re.compile(r"\bAAC-[A-Z]{2,6}-\d{1,3}\b")
    hits = []
    for name in files:
        path = REPO / name
        if path.suffix not in (".py", ".sql", ".json", ".ts", ".tsx", ".md", ".yml", ".yaml", ".mjs") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(text):
            hits.append(name)
    assert hits == [], f"control ids of the private register in tracked files: {hits}"
