"""S279 thresholds L1 (CIS 407) — the compiled settings defaults equal the migration seed (audit section 5.4 check 4).

``shared/settings/compiled_defaults.json`` is what the settings reader serves when the database cannot be read, so it
must be the seed of ``shared.setting_definition`` exactly: ``scripts/settings/gen_compiled_defaults.py --check`` fails
when they differ (fix: run the script and commit the file). The seed rows also obey the table's CHECKs here, so a bad
row fails in unit CI before the migration replay. No DB."""
import json
import re

import pytest

from scripts.settings import gen_compiled_defaults as gen

VALUE_TYPES = {"int", "numeric", "bool", "enum", "duration_s", "usd", "pct", "list", "json"}
NUMERIC = {"int", "numeric", "duration_s", "usd", "pct"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){2,}$")


def test_compiled_defaults_match_the_migration_seed():
    assert gen.main(["--check"]) == 0, "run python3 scripts/settings/gen_compiled_defaults.py and commit the file"


def test_the_seed_comes_from_407_and_names_its_source():
    rows, sources = gen.seed_rows()
    assert "407_platform_settings_registry.sql" in sources
    doc = json.loads(gen.OUT.read_text(encoding="utf-8"))
    assert "407_platform_settings_registry.sql" in doc["_generated"]
    assert [r["key"] for r in doc["definitions"]] == sorted(r["key"] for r in rows)


def _rows():
    return gen.seed_rows()[0]


def test_every_seed_row_obeys_the_definition_checks():
    for r in _rows():
        k = r["key"]
        assert KEY_RE.match(k) and len(k) <= 120, k
        assert r["app"] in ("cis", "tripplanner", "booking", "crm") and k.startswith(r["app"] + "."), k
        assert 3 <= len(r["label"]) <= 120 and 20 <= len(r["description"]) <= 2000, k
        assert r["value_type"] in VALUE_TYPES, k
        assert r["scope_kind"] in ("platform", "platform+tenant") and r["impact"] in ("high", "medium", "low"), k
        assert r["approval"] in ("single", "maker_checker") and 5 <= r["cache_s"] <= 3600, k
        assert r["env_override"] is None or re.match(r"^[A-Z][A-Z0-9_]{2,63}$", r["env_override"]), k
        lo, hi, d = r["min_value"], r["max_value"], r["default_value"]
        if lo is not None and hi is not None:
            assert lo <= hi, k
        if r["value_type"] in NUMERIC:
            assert isinstance(d, (int, float)) and not isinstance(d, bool), k
            assert (lo is None or d >= lo) and (hi is None or d <= hi), k
            if r["value_type"] in ("int", "duration_s"):
                assert float(d).is_integer(), k
        elif r["value_type"] == "enum":
            assert isinstance(r["allowed_values"], list) and len(r["allowed_values"]) >= 2 and d in r["allowed_values"]
        elif r["value_type"] == "bool":
            assert isinstance(d, bool), k
        elif r["value_type"] == "json" and isinstance(d, dict):
            assert all((lo is None or v >= lo) and (hi is None or v <= hi) for v in d.values()), k


@pytest.mark.parametrize("key,value", [
    ("cis.t7.blog_min_seed_words", 600),            # the Terraform value in force (code default was 0)
    ("cis.t7.blog_chars_per_seed_word", 5.8),
    ("cis.t9.repeat_drop_near_exact", 0.9),
    ("cis.t9.max_attempts", 3),
    ("cis.a1.attempts_before_review", 3),
    ("cis.t2.version_usd_max", 0.25),
    ("cis.writing_memory.month_cap_usd", 25),
    ("cis.publish.daily_limit_default", 20),
    ("cis.billing.rewrite_over_quota_mode", "soft"),
    ("cis.retention.llm_call_log_days", 180),
    ("cis.erasure.tenant_retention_days", 30),
    ("cis.settings.two_person_approval", False),
])
def test_seeded_from_todays_effective_values(key, value):
    by_key = {r["key"]: r for r in _rows()}
    assert by_key[key]["default_value"] == value


def test_the_trash_window_is_badged_not_enforced():
    by_key = {r["key"]: r for r in _rows()}
    assert by_key["cis.content.trash_retention_days"]["enforced"] is False
    # CIS 411 / 417 / 419 / 420 / 423 / 425 / 426 seeded their keys not enforced until the code that reads them was
    # deployed; CIS 427 (AA-1051 + AA-1096 follow-up, S281) marks enforced every one that code on main reads. Only the
    # trash window (no purge job reads it) and the learner retention window (the learn_retention job has not shipped)
    # stay not enforced. A new key seeded not enforced is added here with the migration that seeds it.
    # AA-1090 (CIS 428): the TripPlanner custom-domain limits stay not enforced until the AA-1090 code is deployed.
    assert sorted(k for k, r in by_key.items() if not r["enforced"]) == sorted(NOT_ENFORCED)
    for key in (*LEARN_KEYS, *TWO_STEP_KEYS):
        assert by_key[key]["enforced"] is (key not in NOT_ENFORCED), key


# AA-1090 (CIS 428): the TripPlanner custom-domain limits, until the editor and the tripplanner_custom_domain job are deployed.
TP_DOMAIN_KEYS = ("cis.tripplanner.custom_domain_max_per_tenant", "cis.tripplanner.custom_domain_edge_max",
                  "cis.tripplanner.custom_domain_validation_timeout_hours", "cis.tripplanner.custom_domain_poll_seconds",
                  "cis.tripplanner.custom_domain_edge_pending_hours", "cis.tripplanner.custom_domain_renewal_grace_days")

# AA-1146 (CIS 458): the agency-copy refresh cap and its alert, until the refresh job of AA-1146 part 2 ships.
T2_RERUN_KEYS = ("cis.content.t2_rerun_monthly_cap_usd", "cis.content.t2_rerun_alert_pct")

NOT_ENFORCED = ("cis.content.trash_retention_days", "cis.learn.learner_retention_days", *TP_DOMAIN_KEYS,
                *T2_RERUN_KEYS)


TWO_STEP_KEYS = ("cis.quality.claim_two_step_route_tp_min_pct", "cis.quality.claim_two_step_judge_tp_keep_min_pct",
                 "cis.quality.claim_two_step_judge_fp_drop_min_pct", "cis.quality.claim_fresh_pool_sample_max")


LEARN_KEYS = ("cis.learn.quiz_pass_mark_pct", "cis.learn.final_quiz_pass_mark_pct", "cis.learn.quiz_retake_delay_hours",
              "cis.learn.quiz_items_per_attempt", "cis.learn.final_quiz_items_per_module", "cis.learn.recert_months",
              "cis.learn.recert_paths", "cis.learn.video_url_ttl_seconds", "cis.learn.learner_retention_days",
              "cis.learn.review_target_retention", "cis.learn.review_max_cards_per_day",
              "cis.learn.verify_requests_per_minute",
              # CIS 425 (AA-1105, S281): the academy web's per-visitor verify limit, like the twelve CIS 420 keys.
              "cis.learn.verify_visitor_requests_per_minute")


def test_the_measured_ratio_needs_evidence():
    by_key = {r["key"]: r for r in _rows()}
    assert by_key["cis.t7.blog_chars_per_seed_word"]["evidence_required"] is True
    assert "3.364" in by_key["cis.t7.blog_chars_per_seed_word"]["description"]


def test_render_is_deterministic_and_a_later_row_replaces_an_earlier_one(tmp_path):
    seed = ('-- settings-seed:begin\nSELECT $seed$[{}]$seed$;\n-- settings-seed:end\n')
    row = {f: None for f in gen.FIELDS}
    row.update(key="cis.zz.alpha", app="cis", default_value=1)
    later = dict(row, default_value=2)
    (tmp_path / "001_a.sql").write_text(seed.format(json.dumps(row)), encoding="utf-8")
    (tmp_path / "002_b.sql").write_text(seed.format(json.dumps(later)), encoding="utf-8")
    (tmp_path / "003_none.sql").write_text("SELECT 1;\n", encoding="utf-8")
    rows, sources = gen.seed_rows(tmp_path)
    assert sources == ["001_a.sql", "002_b.sql"] and rows == [dict(later, guide=None)]
    assert gen.render(tmp_path) == gen.render(tmp_path)


def test_a_row_missing_a_field_fails_the_generator(tmp_path):
    (tmp_path / "001_a.sql").write_text(
        '-- settings-seed:begin\nSELECT $seed$[{"key": "cis.zz.beta"}]$seed$;\n-- settings-seed:end\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        gen.seed_rows(tmp_path)


# AA-1096 l23 (CIS 416): the guide block sets a key's plain label and guide; a later re-seed keeps the guide.
GUIDE = {"en": {"what": "w", "raise": "r", "lower": "l", "when": "n", "example": "e"}}


def _files(tmp_path, *texts):
    for i, text in enumerate(texts, 1):
        (tmp_path / f"{i:03d}_x.sql").write_text(text, encoding="utf-8")


def _seed_sql(row):
    return f"-- settings-seed:begin\nSELECT $seed${json.dumps([row])}$seed$;\n-- settings-seed:end\n"


def _guide_sql(rows):
    return f"-- settings-guide:begin\nSELECT $guide${json.dumps(rows)}$guide$;\n-- settings-guide:end\n"


def test_a_guide_block_sets_label_and_guide_and_a_reseed_keeps_the_guide(tmp_path):
    row = {f: None for f in gen.FIELDS}
    row.update(key="cis.zz.gamma", app="cis", label="Old code label", default_value=1)
    _files(tmp_path, _seed_sql(row), _guide_sql([{"key": "cis.zz.gamma", "label": "Plain label", "guide": GUIDE}]),
           _seed_sql(dict(row, label="Re-seeded label", default_value=2)))
    rows, sources = gen.seed_rows(tmp_path)
    assert sources == ["001_x.sql", "002_x.sql", "003_x.sql"]
    assert rows[0]["label"] == "Re-seeded label" and rows[0]["default_value"] == 2 and rows[0]["guide"] == GUIDE


def test_a_seed_row_may_carry_its_guide(tmp_path):
    row = {f: None for f in gen.FIELDS}
    row.update(key="cis.zz.delta", app="cis", label="Delta", default_value=1, guide=GUIDE)
    _files(tmp_path, _seed_sql(row))
    assert gen.seed_rows(tmp_path)[0][0]["guide"] == GUIDE


def test_a_guide_of_an_unknown_key_or_without_label_fails_the_generator(tmp_path):
    _files(tmp_path, _guide_sql([{"key": "cis.zz.nobody", "label": "X", "guide": GUIDE}]))
    with pytest.raises(SystemExit):
        gen.seed_rows(tmp_path)
    row = {f: None for f in gen.FIELDS}
    row.update(key="cis.zz.eps", app="cis", default_value=1)
    (tmp_path / "001_x.sql").write_text(_seed_sql(row) + _guide_sql([{"key": "cis.zz.eps", "guide": GUIDE}]),
                                        encoding="utf-8")
    with pytest.raises(SystemExit):
        gen.seed_rows(tmp_path)
