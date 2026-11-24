"""AA-1173 — CIS 478 as text, without a database: contract class (no migrate directive line), self-registering, the
five settings seeded with EN + VI guides, and no account ID, ARN, bucket URI or e-mail address in a public file."""
from __future__ import annotations

import json
import re
from pathlib import Path

M478 = Path(__file__).resolve().parents[2] / "api" / "migrations" / "478_aa1173_compliance_exceptions_reviews.sql"
KEYS = {"cis.compliance.validity_hours.aws_nightly", "cis.compliance.validity_hours.prowler_weekly",
        "cis.compliance.prowler_critical_high_max", "cis.compliance.exception_max_days",
        "cis.compliance.review_max_age_days"}


def _seed() -> list[dict]:
    text = M478.read_text(encoding="utf-8")
    return json.loads(text.split("FROM jsonb_to_recordset($seed$", 1)[1].split("$seed$::jsonb", 1)[0])


def test_contract_and_self_registering():
    text = M478.read_text(encoding="utf-8")
    assert not re.search(r"^--\s*migrate:", text, re.M)
    assert "VALUES ('478'" in text and text.rstrip().endswith("COMMIT;")


def test_five_settings_with_guides():
    rows = {r["key"]: r for r in _seed()}
    assert set(rows) == KEYS
    for key, r in rows.items():
        assert r["area"] == "compliance" and r["enforced"] is True and r["approval"] == "maker_checker", key
        for lang in ("en", "vi"):
            assert set(r["guide"][lang]) == {"what", "raise", "lower", "when", "example"}, (key, lang)
        assert r["min_value"] <= r["default_value"] <= r["max_value"], key


def test_no_sensitive_text_in_the_file():
    text = M478.read_text(encoding="utf-8")
    assert not re.search(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])", text)
    assert "arn:aws" not in text.lower() and "s3://" not in text.lower()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
