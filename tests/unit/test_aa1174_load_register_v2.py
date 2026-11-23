"""AA-1174 — scripts/compliance/load_register.py format 2 (law frameworks, their obligations and the applicability
matrix) on an invented register built from the format-1 example: the sections validate, every refusal case (a standard
or seeded framework, a non-official or non-https source, a bad verdict or market, a title that reads like normative text
or claims compliance, an identifier), mappings onto the law obligations of the same file, and the DRY RUN plan (laws no
longer listed are deactivated, obligations no longer listed set not applicable, never deleted). No DB."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.compliance import load_register as lr

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "scripts" / "compliance" / "register.example.json"
KINDS = {f: "standard" for f in lr.FRAMEWORKS} | {"eu-ai-act:2024": "law"}


def v2() -> dict:
    reg = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    reg["format"] = 2
    reg["frameworks"] = [{"framework": "ex-law:2025", "title": "Example personal data law", "jurisdiction": "VN",
                          "citation": "Example Law 1/2025", "source_url": "https://vbpl.vn/example",
                          "in_force_on": "2026-01-01", "sort_order": 10, "active": True}]
    reg["requirements"] = [{"framework": "ex-law:2025", "ref": "Art.20", "section": "Example law",
                            "title": "File the cross-border transfer dossier", "sort_order": 1,
                            "counsel_review": True, "applies_as": "both",
                            "source_url": "https://vbpl.vn/example#art20",
                            "doc_url": "https://github.com/ExampleOrg/repo/blob/master/docs/legal/x.md"}]
    reg["applicability"] = [{"market": "VN", "segment": "staff", "framework": "ex-law:2025",
                             "test": "The company processes staff data in Vietnam", "threshold": None,
                             "verdict": "applies", "source_url": "https://vbpl.vn/example",
                             "source_title": "Example Law 1/2025, Art. 1", "assessed_on": "2026-11-23",
                             "counsel_review": True, "note": None},
                            {"market": "US-CA", "segment": "traveller", "framework": "eu-ai-act:2024",
                             "test": "Invented row on a seeded law", "threshold": "none", "verdict": "not_applicable",
                             "source_url": "https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
                             "source_title": "Regulation (EU) 2024/1689", "assessed_on": "2026-11-23"}]
    reg["mappings"].append({"control_id": reg["controls"][0]["control_id"], "framework": "ex-law:2025",
                            "ref": "Art.20", "coverage": "partial", "note": None})
    return reg


def cat(reg: dict) -> set[tuple[str, str]]:
    return {(m["framework"], m["ref"]) for m in reg["mappings"] if m["framework"] != "ex-law:2025"}


def test_v2_validates_and_maps_onto_its_own_obligations():
    reg = v2()
    assert lr.validate(reg, cat(reg), KINDS) == []
    p = lr.plan(reg, {c["control_id"] for c in reg["controls"]},
                {"frameworks": {"ex-law:2025", "ex-old:2020"}, "requirements": {("ex-law:2025", "Art.20"),
                                                                                ("ex-law:2025", "Art.9")}})
    assert p["frameworks_upsert"] == 1 and p["frameworks_deactivate"] == ["ex-old:2020"]
    assert p["requirements_upsert"] == 1 and p["requirements_not_applicable"] == 1 and p["applicability"] == 2
    assert p["deactivate"] == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["frameworks"][0].update(framework="iso27001:2022"), "seeded or standard"),
    (lambda r: r["frameworks"][0].update(framework="eu-ai-act:2024"), "seeded or standard"),
    (lambda r: r["frameworks"][0].update(source_url="https://blog.example.com/law"), "official host"),
    (lambda r: r["frameworks"][0].update(source_url="http://vbpl.vn/x"), "official host"),
    (lambda r: r["frameworks"][0].update(scored=True), "never scored"),
    (lambda r: r["frameworks"][0].update(title="A fully compliant law"), "claims compliance"),
    (lambda r: r["requirements"][0].update(framework="iso27001:2022", ref="A.5.1"), "not a law framework"),
    (lambda r: r["requirements"][0].update(title="The organisation shall file a dossier"), "normative"),
    (lambda r: r["requirements"][0].update(title="AA complies with this article"), "claims compliance"),
    (lambda r: r["requirements"][0].update(doc_url="https://example.com/x"), "github.com"),
    (lambda r: r["requirements"][0].update(applies_as="owner"), "applies_as"),
    (lambda r: r["requirements"][0].pop("section"), "section is missing"),
    (lambda r: r["requirements"][0].update(sort_order=0), "sort_order"),
    (lambda r: r["requirements"][0].update(section="see account 123456789012"), "an account ID"),
    (lambda r: r["applicability"][0].update(verdict="maybe"), "verdict"),
    (lambda r: r["applicability"][0].update(market="vn"), "market"),
    (lambda r: r["applicability"][0].update(segment="visitor"), "segment"),
    (lambda r: r["applicability"][0].update(source_url="https://example.com/x"), "primary source"),
    (lambda r: r["applicability"][0].update(note="contact dpo@example.com"), "an e-mail address"),
    (lambda r: r["applicability"][0].update(test="We are in compliance"), "claims compliance"),
    (lambda r: r["applicability"][0].update(assessed_on="yesterday"), "assessed_on"),
    (lambda r: r["applicability"].append(dict(r["applicability"][0])), "listed twice"),
    (lambda r: r["mappings"][-1].update(ref="Art.99"), "has no such ref"),
])
def test_a_bad_v2_register_is_refused(mutate, needle):
    reg = v2()
    good = cat(reg)
    bad = copy.deepcopy(reg)
    mutate(bad)
    problems = lr.validate(bad, good, KINDS)
    assert any(needle in p for p in problems), problems


def test_every_law_obligation_reaches_the_page_with_a_link():
    """No doc_url is fine while a mapped active control has a required automatic check (its evidence row links); with
    only manual controls (or none), or a not-applicable item aside, a missing doc_url is refused."""
    reg = v2()
    reg["requirements"][0]["doc_url"] = None
    reg["controls"][0]["checks"] = [{"check_id": "repo_rulesets", "required": True}]
    reg["controls"][0]["active"] = True
    assert lr.validate(reg, cat(reg), KINDS) == []
    for mutate in (lambda r: r["controls"][0].update(checks=[]),
                   lambda r: r["controls"][0].update(checks=[{"check_id": "repo_rulesets", "required": False}]),
                   lambda r: r["controls"][0].update(active=False),
                   lambda r: r["mappings"].pop()):
        bad = copy.deepcopy(reg)
        mutate(bad)
        assert any("no doc_url" in p for p in lr.validate(bad, cat(reg), KINDS))
    aside = copy.deepcopy(reg)
    aside["controls"][0]["checks"] = []
    aside["requirements"][0]["applicable"] = False
    assert lr.validate(aside, cat(reg), KINDS) == []


def test_law_sections_need_format_2_and_unknown_formats_are_refused():
    reg = v2()
    reg["format"] = 1
    assert "frameworks, requirements and applicability need format 2" in lr.validate(reg, cat(reg), KINDS)
    assert lr.validate({"format": 3}, set()) == ["the register file is not format 1 or 2"]


def test_official_hosts_accept_sub_domains_only():
    assert lr._host_ok("https://www.legislation.gov.uk/ukpga/2018/12")
    assert lr._host_ok("https://eur-lex.europa.eu/eli/reg/2016/679/oj")
    assert not lr._host_ok("https://eur-lex.europa.eu.attacker.example/x")
    assert not lr._host_ok("https://notvbpl.vn/x")
    assert lr._host_ok("https://vbpl.moj.gov.vn/TW/x.doc") and lr._host_ok("https://congbao.chinhphu.vn/van-ban/x.htm")
    assert not lr._host_ok("https://gov.vn.example.com/x")


def test_vietnam_sources_are_the_gazette_or_a_legal_database():
    """A ministry's or a province's introduction document of a law is a secondary source; the page labels every law
    link "Official source", so only the legal document databases and the official gazette pass for Vietnam."""
    assert lr._host_ok("https://datafiles.chinhphu.vn/cpp/files/vbpq/2025/01/luat60.pdf")
    assert lr._host_ok("https://vanban.chinhphu.vn/?pageid=27160&docid=1") and lr._host_ok("https://vbpl.vn/x")
    for url in ("https://sbv.gov.vn/documents/intro.docx", "https://sotuphap.daklak.gov.vn/intro.pdf",
                "https://xaydungchinhsach.chinhphu.vn/news.htm", "https://chinhphu.vn/news.htm"):
        assert not lr._host_ok(url), url
