"""AA-1170 — scripts/compliance/load_register.py (the private register loader) on the invented example register, and
the public-repo guard of the compliance files: no account ID, ARN, S3 URI, e-mail address or private control id, and
no copied standard text (the register titles are short, in our own words). No DB."""
from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from scripts.compliance import load_register as lr

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "scripts" / "compliance" / "register.example.json"


def example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def catalogue(reg: dict) -> set[tuple[str, str]]:
    return {(m["framework"], m["ref"]) for m in reg["mappings"]}


def test_the_example_validates_and_plans():
    reg = example()
    assert lr.validate(reg, catalogue(reg)) == []
    assert all(c["control_id"].startswith("EX-") for c in reg["controls"])
    p = lr.plan(reg, {"EX-CHG-01", "EX-OLD-01"})
    assert p["update"] == ["EX-CHG-01"] and p["deactivate"] == ["EX-OLD-01"] and "EX-VUL-01" in p["insert"]
    assert p["mappings"] == len(reg["mappings"])


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["controls"][0].update(control_id="bad id"), "control_id does not match"),
    (lambda r: r["controls"][0].update(owner_role="someone@example.com"), "an e-mail address"),
    (lambda r: r["controls"][0].update(intent="see account 123456789012"), "an account ID"),
    (lambda r: r["controls"][0].update(intent="role arn:aws:iam::x"), "an ARN"),
    (lambda r: r["controls"][0].update(intent="file in s3://somewhere"), "an S3 URI"),
    (lambda r: r["controls"][0].update(title="x"), "title must be"),
    (lambda r: r["controls"][0].update(frequency="hourly"), "frequency"),
    (lambda r: r["controls"][0].update(checks=[{"check_id": "Bad"}]), "a check needs"),
    (lambda r: r["mappings"][0].update(ref="Z.9.9"), "has no such ref"),
    (lambda r: r["mappings"][0].update(framework="gdpr"), "unknown framework"),
    (lambda r: r["mappings"][0].update(coverage="most"), "coverage"),
    (lambda r: r["mappings"].append(dict(r["mappings"][0])), "listed twice"),
    (lambda r: r["controls"].append(dict(r["controls"][0])), "listed twice"),
])
def test_a_bad_register_is_refused(mutate, needle):
    reg = example()
    cat = catalogue(reg)
    bad = copy.deepcopy(reg)
    mutate(bad)
    problems = lr.validate(bad, cat)
    assert any(needle in p for p in problems), problems


def test_wrong_format_is_refused():
    assert lr.validate({"format": 2}, set()) == ["the register file is not format 1"]


# ── public-repo guard ───────────────────────────────────────────────────────────────────────────────────────────────

GUARDED = ("api/services/compliance.py", "api/services/compliance_score.py", "api/services/compliance_settings.py",
           "api/routers/admin_platform_compliance.py", "scripts/compliance/load_register.py",
           "scripts/compliance/register.example.json", "frontend/app/admin/platform/compliance",
           "frontend/lib/compliance.ts", "docs/implementation-notes/AA-1170.md")
ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
NORMATIVE = re.compile(r"\b(shall|the organi[sz]ation)\b", re.I)


def _files() -> list[Path]:
    out = []
    for name in GUARDED:
        p = REPO / name
        out += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else ([p] if p.exists() else [])
    return out


def test_no_identifiers_in_the_compliance_files():
    files = _files()
    assert len(files) >= 7
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not ACCOUNT_ID.search(text), f
        assert "arn:aws:" not in text, f
        assert not re.search(r"s3://[a-z0-9]", text), f
        assert not re.search(r"aa-cis-bronze", text), f
        assert not re.search(r"\bAAC-[A-Z]{2,6}-\d{1,3}\b", text), f   # the private register's control ids


def test_example_register_is_our_own_short_wording():
    for c in example()["controls"]:
        assert len(c["title"]) <= 80 and not NORMATIVE.search(c["title"] + " " + (c.get("intent") or "")), c


def test_no_register_file_is_tracked_besides_the_example():
    files = subprocess.run(["git", "ls-files", "scripts/compliance"], cwd=REPO, capture_output=True, text=True,
                           check=True).stdout.split()
    assert sorted(Path(f).name for f in files if f.endswith(".json")) in ([], ["register.example.json"])
