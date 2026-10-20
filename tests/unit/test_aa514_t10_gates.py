"""AA-514 — services/acp_content_writing/quality_gates.py's gate_seo_surface (AA-787 dropped the other
two AA-514 pieces, gate_promises_an_option and the route-aware F3 variance check; F17_trip_fact covers the
offered-moment failure, see test_aa773_offered_status.py). Thresholds are read verbatim from the
Ms. Thư repo (see docs/claude_audit/AA-514-step0-investigation.md) — these tests confirm the PORT
behaves per that source, not a re-decided variant."""
from services.acp_content_writing import quality_gates as qg


class TestGateSeoSurface:
    def test_all_fields_missing_fails_with_3_violations(self):
        result = qg.gate_seo_surface(None, None, None, None)
        assert result["passed"] is False
        assert result["repairable"] is True  # joins F2_banned_patterns's fixable group
        assert len(result["violations"]) == 3

    def test_valid_fields_pass(self):
        result = qg.gate_seo_surface(
            seo_title="Wat Sisaket Travel Guide: Vientiane's Oldest Temple",
            meta_description="x" * 130 + " for your Laos trip today.",
            slug="wat-sisaket-guide",
            keyword=None,
        )
        assert result["passed"] is True

    def test_stub_title_fails(self):
        # AA-768 (S233) — "Bhutan's Craft Bazaar" (21 chars) shipped on 6/20 S232 blogs
        result = qg.gate_seo_surface(
            seo_title="Bhutan's Craft Bazaar", meta_description="x" * 130 + ".", slug="a-slug", keyword=None,
        )
        assert result["passed"] is False
        assert "stub" in result["violations"][0]

    def test_title_too_long_fails(self):
        result = qg.gate_seo_surface(
            seo_title="x" * 61, meta_description="x" * 130 + ".", slug="a-slug", keyword=None,
        )
        assert any("truncates" in v for v in result["violations"])

    def test_meta_description_wrong_length_fails(self):
        result = qg.gate_seo_surface(
            seo_title="A Title", meta_description="Too short.", slug="a-slug", keyword=None,
        )
        assert any("120-158" in v for v in result["violations"])

    def test_meta_description_missing_terminal_punctuation_fails(self):
        result = qg.gate_seo_surface(
            seo_title="A Title", meta_description="x" * 135, slug="a-slug", keyword=None,
        )
        assert any("complete sentence" in v for v in result["violations"])

    def test_slug_not_kebab_case_fails(self):
        result = qg.gate_seo_surface(
            seo_title="A Title", meta_description="x" * 130 + ".", slug="Not Kebab Case!",
            keyword=None,
        )
        assert any("kebab" in v for v in result["violations"])

    def test_missing_keyword_in_title_and_meta_fails(self):
        result = qg.gate_seo_surface(
            seo_title="A Title With No Keyword", meta_description="x" * 130 + ".",
            slug="a-slug", keyword="laos temples",
        )
        assert any("keyword" in v for v in result["violations"])

    def test_keyword_present_passes_keyword_check(self):
        result = qg.gate_seo_surface(
            seo_title="Laos Temples Travel Guide",
            meta_description="Explore the best laos temples on your next trip " + "x" * 80 + ".",
            slug="laos-temples-guide", keyword="laos temples",
        )
        assert not any("keyword" in v for v in result["violations"])
