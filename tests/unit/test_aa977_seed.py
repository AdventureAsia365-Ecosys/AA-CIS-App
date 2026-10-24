"""AA-977 S4 — the starter content seed (data/aa_content/seed_v1.json) and the seed script's plan.

The seed must pass every rule the admin API applies at create and at publish (services/aa_content/validate.py), so
`--apply --publish` cannot fail half way on Dev. Size (plan contradiction resolved in the implementation notes):
14 groups x EN + VI = 28 items — 10 prep packs, 2 starter templates, 2 campaign blocks; one prep-pack item stays a
draft for the rule R check.
"""
from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from services.aa_content import validate

REPO = Path(__file__).resolve().parents[2]
SEED = REPO / "data" / "aa_content" / "seed_v1.json"

_spec = importlib.util.spec_from_file_location("aa977_seed", REPO / "scripts" / "aa977_seed_aa_content.py")
seed_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_script)

PREP_COUNTRIES = {"VN", "TH", "KH", "LA", "MM", "ID", "JP", "KR", "NP", "BT"}
# Visa and entry links point at the government's own site (contract: visa = an official link only).
OFFICIAL_HOSTS = re.compile(
    r"^https://(www\.)?([a-z0-9-]+\.)*(gov\.vn|go\.th|gov\.kh|gov\.la|gov\.mm|go\.id|go\.jp|go\.kr|gov\.np|gov\.bt"
    r"|bhutan\.travel)(/|$)")
LINK = re.compile(r"https?://[^\s)\]>]+")


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return seed_script.load_seed(SEED)[0]


def test_seed_size_and_shape(items):
    data = json.loads(SEED.read_text())
    groups = data["groups"]
    assert len(groups) == 14 and len(items) == 28
    assert Counter(g["kind"] for g in groups) == {"prep_pack": 10, "starter_template": 2, "campaign_block": 2}
    assert all(set(g["items"]) == {"en", "vi"} for g in groups)                     # every group in both languages
    assert len({g["group_key"] for g in groups}) == 14
    prep_codes = [g["applies_to"]["country_codes"] for g in groups if g["kind"] == "prep_pack"]
    assert all(len(c) == 1 for c in prep_codes) and {c[0] for c in prep_codes} == PREP_COUNTRIES
    assert data["actor"] == seed_script.ACTOR


def test_every_item_passes_the_api_rules(items):
    assert seed_script.check_seed(items) == []


def test_prep_packs_link_visa_advice_to_official_sites(items):
    for it in (i for i in items if i["kind"] == "prep_pack"):
        body = it["body_markdown"]
        label = f"{it['group_key']}:{it['locale']}"
        assert validate.visa_sections_without_link(body) == [], label
        assert validate._mentions_visa(body), f"{label}: a prep pack covers entry and visa"
        links = LINK.findall(body)
        assert links, label
        for url in links:
            assert OFFICIAL_HOSTS.match(url), f"{label}: {url} is not an official government site"


def test_no_other_links_and_no_visa_card_wording(items):
    for it in (i for i in items if i["kind"] != "prep_pack"):
        assert not LINK.search(it["body_markdown"]), it["group_key"]


def test_exactly_one_deliberate_draft_a_prep_pack_vi(items):
    kept = [i for i in items if i["keep_draft"]]
    assert len(kept) == 1 and kept[0]["kind"] == "prep_pack" and kept[0]["locale"] == "vi"


def test_check_seed_reports_problems(items):
    bad = [dict(items[0]), dict(items[0])]
    bad[0]["body_markdown"] = "## Visa\n\nYou need one."
    problems = seed_script.check_seed(bad)
    assert any("listed twice" in p for p in problems)
    assert any("visa_needs_official_link" in p for p in problems if items[0]["kind"] == "prep_pack")


def _row(group_key, locale, *, created_by="seed:AA-977", published=None, draft=1, retired=None):
    return {"content_id": f"{group_key}-{locale}", "group_key": group_key, "locale": locale, "kind": "prep_pack",
            "created_by": created_by, "retired_at": retired, "updated_at": None, "published_version": published,
            "draft_version": draft}


def test_plan_on_an_empty_database(items):
    p = seed_script.plan(items, [])
    assert len(p["to_create"]) == 28 and p["existing"] == []
    assert len(p["to_publish"]) == 27 and p["kept_draft"] == ["prep-pack-bhutan:vi"]
    assert p["skipped_publish"] == []


def test_plan_is_idempotent_and_never_publishes_someone_elses_draft(items):
    existing = [
        _row("prep-pack-vietnam", "en"),                                     # seeded draft v1 -> publish
        _row("prep-pack-vietnam", "vi", published=1, draft=None),            # already published
        _row("prep-pack-thailand", "en", created_by="admin:42"),             # a human made it
        _row("prep-pack-thailand", "vi", published=1, draft=2),              # human draft v2 on top
        _row("prep-pack-laos", "en", retired="2026-10-25"),                  # retired
        _row("prep-pack-bhutan", "vi"),                                      # the kept draft
    ]
    p = seed_script.plan(items, existing)
    assert len(p["to_create"]) == 22
    assert "prep-pack-vietnam:en" in p["to_publish"]
    reasons = {s["item"]: s["reason"] for s in p["skipped_publish"]}
    assert reasons["prep-pack-vietnam:vi"].startswith("already published")
    assert reasons["prep-pack-thailand:en"] == "not created by the seed"
    assert reasons["prep-pack-thailand:vi"].startswith("already published")
    assert reasons["prep-pack-laos:en"] == "retired"
    assert p["kept_draft"] == ["prep-pack-bhutan:vi"]
    assert len(p["to_publish"]) + len(p["skipped_publish"]) + len(p["kept_draft"]) == 28


def test_plan_skips_a_newer_seeded_draft(items):
    p = seed_script.plan(items, [_row("campaign-newsletter-intro", "en", draft=3)])
    assert {"item": "campaign-newsletter-intro:en", "reason": "draft is v3, not the seed's v1"} in p["skipped_publish"]


def test_edit_demo_appends_one_review_line():
    assert seed_script.demo_body("Text.\n\n", "2026-10-25") == (
        "Text.\n\n_Last reviewed by the Adventure Asia team on 2026-10-25._")


def test_seed_file_hash_is_reported():
    _, sha = seed_script.load_seed(SEED)
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
