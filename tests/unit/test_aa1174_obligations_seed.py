"""AA-1174 (S288) — CIS 471, read from the migration file (no DB).

1. The ten new settings are compiled with the seeded defaults, a plain label and a complete EN + VI guide, and equal the
   keys api/services/compliance_settings.py names (ALL_471); every graded privacy check has a key.
2. The framework seed is the six frameworks of CIS 466, all scored; the EU AI Act stays scored (its card is unchanged)
   but is kind law. No law row is seeded in the public migration: law frameworks, obligations and the applicability
   matrix come from the private register (load_register.py format 2).
3. The file carries no account ID, ARN, S3 URI or e-mail address (the nightly schema dump publishes DDL comments)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from api.services import compliance_settings as keys
from shared import settings

REPO = Path(__file__).resolve().parents[2]
M471 = REPO / "api" / "migrations" / "471_aa1174_compliance_obligations.sql"
GUIDE_KEYS = ("what", "raise", "lower", "when", "example")


def _seed() -> list[dict]:
    text = M471.read_text(encoding="utf-8")
    return json.loads(text.split("$seed$", 2)[1])


def test_the_ten_settings_are_named_and_compiled():
    rows = {r["key"]: r for r in _seed()}
    assert set(rows) == set(keys.ALL_471) and len(rows) == 10
    for key, r in rows.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced) == ("cis", "compliance", "platform", True), key
        assert d.default_value == r["default_value"] and d.label == r["label"], key
        for lang in ("en", "vi"):
            assert all(r["guide"][lang].get(k) for k in GUIDE_KEYS), (key, lang)
        assert r["approval"] == ("single" if key == keys.APPLICABILITY_REVIEW_MAX_AGE_DAYS else "maker_checker"), key
    assert rows[keys.LEGAL_DOCS_OVERDUE_MAX]["default_value"] == 0
    assert rows[keys.CAMPAIGN_CONSENT_VIOLATIONS_MAX]["default_value"] == 0
    assert rows[keys.RETENTION_NEVER_LIVE_MAX]["default_value"] == 0


def test_every_privacy_check_is_graded_by_a_setting():
    graded = set(keys.FACT_LIMITS)
    assert set(keys.LEGAL_DOC_CHECKS) <= graded
    assert {"last_erasure_drill", "retention_runs_recent", "retention_live_enabled", "consent_capture_present",
            "campaign_consent_enforced"} <= graded
    for cid, (_fact, key, kind) in keys.FACT_LIMITS.items():
        assert key in keys.ALL and kind in ("age_days", "max"), cid
    assert set(keys.VALIDITY_HOURS) == {"root_daily", "cis_db", "crm_ops"}


def test_framework_seed_is_the_six_of_466_and_no_law_row():
    text = M471.read_text(encoding="utf-8")
    seeded = re.findall(r"\('([a-z0-9-]+:[0-9a-z.-]+)', '(standard|law)'", text)
    assert dict(seeded) == {"iso27001:2022": "standard", "soc2:2017": "standard", "iso42001:2023": "standard",
                            "owasp-top10:2021": "standard", "owasp-llm:2025": "standard", "eu-ai-act:2024": "law"}
    from api.services import compliance_score as cs   # noqa: PLC0415
    assert set(dict(seeded)) == set(getattr(cs, "DEFAULT_SCORED", cs.FRAMEWORKS))


def test_no_sensitive_text_in_the_file():
    text = M471.read_text(encoding="utf-8")
    assert not re.search(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])", text)
    assert "arn:aws" not in text.lower() and "s3://" not in text.lower()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
