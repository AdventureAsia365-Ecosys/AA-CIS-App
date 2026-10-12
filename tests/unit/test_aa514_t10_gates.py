"""AA-514 — services/acp_content_writing/quality_gates.py's 2 new gates
(gate_promises_an_option, gate_seo_surface) + gate_structural_variance()'s route-aware variance
check. Ported word lists/thresholds are read verbatim from the Ms. Thư repo (see
docs/claude_audit/AA-514-step0-investigation.md) — these tests confirm the PORT behaves per that
source, not a re-decided variant."""
from services.acp_content_writing import quality_gates as qg


class TestGatePromisesAnOption:
    def test_offered_atom_stated_as_definite_fails(self):
        atom_text = "The temple visit is optional, at your own expense, weather permitting."
        content = "You will visit the temple at dawn."
        result = qg.gate_promises_an_option(content, atom_text)
        assert result["passed"] is False
        assert result["repairable"] is False  # ADR 0023 — never auto-fixed
        assert result["blocking"] is False  # AA-519 — flag-not-block, ADR 0023/0026

    def test_offered_atom_properly_hedged_passes(self):
        atom_text = "The temple visit is optional, at your own expense, weather permitting."
        content = "There is time to visit the temple at dawn, if you wish."
        result = qg.gate_promises_an_option(content, atom_text)
        assert result["passed"] is True

    def test_non_offered_atom_never_flagged(self):
        atom_text = "The temple was built in 1820 by King Anouvong."
        content = "You will visit the temple at dawn, built in 1820."
        result = qg.gate_promises_an_option(content, atom_text)
        assert result["passed"] is True

    def test_route_aware_only_flags_the_offered_segment_own_sentence(self):
        """A Route piece with 2 Segments, only ONE offered — the OTHER Segment's unhedged
        sentence must NOT be flagged, even though the piece overall contains an offered moment."""
        route_segments = [
            ("atom_a", "Wat Sisaket was built in 1820."),  # not offered
            ("atom_b", "The kayak trip is optional, at an additional cost."),  # offered
        ]
        content = (
            "You will visit Wat Sisaket at dawn. [R:atom_a] "
            "You will kayak the bay at sunset. [R:atom_b]"
        )
        result = qg.gate_promises_an_option(content, "", route_segments)
        assert result["passed"] is False
        assert len(result["violations"]) == 1
        assert "atom_b" in result["violations"][0]

    def test_route_aware_hedged_offered_segment_passes(self):
        route_segments = [
            ("atom_a", "Wat Sisaket was built in 1820."),
            ("atom_b", "The kayak trip is optional, at an additional cost."),
        ]
        content = (
            "You will visit Wat Sisaket at dawn. [R:atom_a] "
            "There is time to kayak the bay at sunset, if you wish. [R:atom_b]"
        )
        result = qg.gate_promises_an_option(content, "", route_segments)
        assert result["passed"] is True


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


class TestGateStructuralVarianceRouteAware:
    def test_route_aware_variance_measured_between_segment_sections_not_arbitrary_h2s(self):
        route_segments = [("atom_a", "text a"), ("atom_b", "text b")]
        # Section "One" and "Two" both cite atom_a (same Segment) — an H2-generic check would see
        # 2 differently-sized sections and might pass; the route-aware check must map BOTH to the
        # SAME Segment (atom_a) and see only 1 real Segment-section, not 2.
        body = (
            "## One\n" + ("word " * 50) + "[R:atom_a]\n\n"
            "## Two\n" + ("word " * 20) + "[R:atom_a]"
        )
        result = qg.gate_structural_variance(body, route_segments)
        assert any("fewer than 2 Segment-mapped" in v for v in result["violations"])

    def test_route_aware_variance_passes_with_2_distinct_segment_sections_varying(self):
        route_segments = [("atom_a", "text a"), ("atom_b", "text b")]
        body = (
            "## One\n" + ("word " * 200) + "[R:atom_a]\n\n"
            "## Two\n" + ("word " * 50) + "[R:atom_b]"
        )
        result = qg.gate_structural_variance(body, route_segments)
        assert not any("Segment-section" in v for v in result["violations"])

    def test_route_aware_variance_fails_when_segment_sections_too_similar(self):
        route_segments = [("atom_a", "text a"), ("atom_b", "text b")]
        body = (
            "## One\n" + ("word " * 100) + "[R:atom_a]\n\n"
            "## Two\n" + ("word " * 95) + "[R:atom_b]"
        )
        result = qg.gate_structural_variance(body, route_segments)
        assert any("Segment-section" in v for v in result["violations"])

    def test_no_route_segments_falls_back_to_generic_h2_check_unchanged(self):
        """Regression — a non-Route blog piece (route_segments=None) must behave byte-identical
        to before this build: any >=3 H2 sections, generic length-ratio check."""
        section = "word " * 50
        body = f"## One\n{section}\n\n## Two\n{section}\n\n## Three\n{section}"
        result = qg.gate_structural_variance(body)
        assert any("notably longer" in v for v in result["violations"])

    def test_single_segment_falls_back_to_generic_check(self):
        route_segments = [("atom_a", "text a")]  # only 1 -> treated like None
        section = "word " * 50
        body = f"## One\n{section}\n\n## Two\n{section}\n\n## Three\n{section}"
        result = qg.gate_structural_variance(body, route_segments)
        assert any("notably longer" in v for v in result["violations"])
        assert not any("Segment-mapped" in v or "Segment-section" in v for v in result["violations"])
