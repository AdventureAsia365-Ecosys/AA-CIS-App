"""AA-919 — an INVENTED golden-set fixture with the AA-729 shape (24 samples), for the derive / importer / store / guard
tests. No real tour, tenant or label text: every sentence is made up, the tenants are random ids with "zz" slugs.

Built with dict(...) keywords on purpose: a JSON-style literal of the fixture's top-level keys would trip the
golden-set text guard's marker scan (services/golden_set/text_guard.py), which must stay green on this repo.
"""
from __future__ import annotations

import copy
import uuid

TENANT_A = "0a919a00-0000-4000-8000-00000000000a"
TENANT_B = "0a919b00-0000-4000-8000-00000000000b"
SLUG_A, SLUG_B = "zz-fake-agency", "zz-other-agency"
SCHEMA = "aa729-golden-set"


def _tour(i: int, days: int, dense: bool) -> dict:
    numbers = " ".join(f"Stop {k} lasts {k + 2} hours at {k + 1}0 metres." for k in range(1, 6)) if dense else ""
    return dict(
        name=f"Invented Hill Loop {i}", subtitle="A made-up route for tests", country="Testland",
        duration=f"{days} days" if days else "",
        summary=f"Pretend walkers follow pretend valleys past pretend villages on loop number {i}.",
        description=("This fictional journey crosses imaginary meadows and invented ridges with nothing real in it. "
                     + numbers),
        highlights=[f"Imaginary lake number {i}", "A fictional tea house"],
        itineraries=[{"day": 1, "title": "Arrive", "description": "Meet the invented guide at the pretend station."}],
        inclusions=["Made-up meals"], exclusions=["Invented flights"], price="",
    )


def _s1(i: int, verdict: str, days, dense: bool, tags) -> dict:
    return dict(
        sample_id=f"s1-{i:03d}", kind="s1", stratum="s1", group="Testland", country="Testland",
        tour_type="trek_hike", days=days, tags=list(tags), pick_reason="fake",
        refs=dict(tour_id=str(uuid.UUID(int=i)), published_tour_id=None),
        input=dict(tour=_tour(i, days or 0, dense), seo=dict(keywords=["invented trek"]), brand_rules_ref="s1:default"),
        output=dict(name=f"Rewritten invented loop {i}", summary="An invented rewrite of an invented tour for tests."),
        output_post_edit=False,
        pipeline_verdict=dict(passed=verdict == "pass", score="7.50" if verdict == "pass" else "5.00",
                              failure_codes=[] if verdict == "pass" else ["F_FAKE"]),
        label=dict(verdict=verdict, grade="light", reason="An invented reason for a pretend label.",
                   evidence=["pretend quote"], source="panel", strength="panel"),
        label_source="agent_panel",
    )


def _t2(i: int, verdict: str, post_edit: bool) -> dict:
    return dict(
        sample_id=f"t2-{i:03d}", kind="t2", stratum="t2", group=SLUG_A, country="Testland", tour_type="trek_hike",
        days=10, tags=["known_failure"] if verdict == "fail" else [], pick_reason="fake",
        refs=dict(version_id=str(uuid.UUID(int=100 + i)), tenant_id=TENANT_A, tenant_slug=SLUG_A),
        input=dict(tour=_tour(100 + i, 10, i == 1), seo=dict(keywords=["pretend"]), brand_rules_ref=f"t2:{SLUG_A}",
                   tenant_id=TENANT_A, rewrite_language="en-GB", brand_loaded=True),
        output=dict(name=f"Tenant rewrite {i}", summary="A pretend tenant rewrite with invented words only."),
        output_post_edit=post_edit,
        pipeline_verdict=dict(passed=verdict == "pass", score="8.00"),   # strings, as in AA-729
        label=dict(verdict=verdict, reason="Invented.", evidence=[], source="panel", strength="panel"),
        label_source="tenant_edit" if post_edit else "agent_panel",
    )


def _t9(i: int, channel: str, verdict: str, tenant: str, slug: str, post_edit: bool = False) -> dict:
    prefix = "t9b" if channel == "blog" else "t9l"
    return dict(
        sample_id=f"{prefix}-{i:03d}", kind="t9", stratum="t9_blog" if channel == "blog" else "t9_linkedin",
        channel=channel, group=slug, country="unknown", tags=["held"], pick_reason="fake",
        refs=dict(piece_id=str(uuid.UUID(int=200 + i)), tenant_id=tenant, tenant_slug=slug),
        input=dict(context=dict(channel=channel, tenant_id=tenant, atom_text="An invented moment at a pretend falls.",
                                cta="Ask the pretend agency about this invented trip.",
                                brand_audience=dict(brand_type="Invented pretend trekking outfit",
                                                    forbidden_words=["nestled among the pretend hills"])),
                   tenant_id=tenant, piece_id=str(uuid.UUID(int=300 + i)), context_source="job_payload"),
        output=dict(content_text="Pretend travellers walk to a made-up waterfall and drink invented tea together."),
        output_post_edit=post_edit, pipeline_verdict=dict(passed=verdict == "pass", held_reason=None),
        label=dict(verdict=verdict, reason="Invented.", evidence=[], source="panel", strength="panel"),
        label_source="admin_edit" if post_edit else "agent_panel",
    )


def _inj_s1(i: int) -> dict:
    s = _s1(900 + i, "fail", None, False, ["prompt_injection"])
    s.update(sample_id=f"inj-s1-{i}", kind="injection_s1", stratum="injection", group="synthetic", refs={},
             pipeline_verdict=None, label_source="synthetic",
             injection_spec=dict(canary="ZZCANARY", urls=[], forbidden_phrases=[], attack_id=f"a{i}"))
    s.pop("tour_type")
    return s


def _inj_t9(i: int, host: dict) -> dict:
    s = copy.deepcopy(host)
    s.update(sample_id=f"inj-t9-{i}", kind="injection_t9", stratum="injection", group="synthetic",
             refs=dict(host_sample_id=host["sample_id"], tenant_slug=host["refs"]["tenant_slug"]),
             pipeline_verdict=None, label_source="synthetic", tags=["prompt_injection"], output_post_edit=False,
             label=dict(verdict="fail", reason="Invented.", evidence=[], source="synthetic", strength="synthetic"),
             injection_spec=dict(canary="ZZCANARY", urls=[], forbidden_phrases=[], attack_id=f"t{i}"))
    s["input"]["context_source"] = "synthetic_seed_on_host"
    return s


def fake_fixture(version: str = "1.0.0") -> dict:
    """24 samples: s1 8 (6 fail, 2 pass; days 15, 9, 8, None, …; 3 fact-dense), t2 4 (tenant A, 1 post_edit),
    t9 blog 4 (tenant A, 1 post_edit), t9 linkedin 4 (tenant B), injection 4 (2 s1, 2 t9 hosted on blog / linkedin)."""
    s1 = [_s1(1, "fail", 15, True, ["cycle_bike", "long_18d"]), _s1(2, "fail", 9, True, ["known_failure"]),
          _s1(3, "fail", 8, False, []), _s1(4, "fail", None, False, ["poor_raw"]),
          _s1(5, "fail", 3, True, []), _s1(6, "fail", 12, False, []),
          _s1(7, "pass", 10, False, []), _s1(8, "pass", 2, False, ["water"])]
    t2 = [_t2(1, "fail", False), _t2(2, "fail", True), _t2(3, "pass", False), _t2(4, "fail", False)]
    blog = [_t9(1, "blog", "fail", TENANT_A, SLUG_A), _t9(2, "blog", "pass", TENANT_A, SLUG_A, post_edit=True),
            _t9(3, "blog", "fail", TENANT_A, SLUG_A), _t9(4, "blog", "pass", TENANT_A, SLUG_A)]
    social = [_t9(i, "linkedin", "pass" if i % 2 else "fail", TENANT_B, SLUG_B) for i in range(1, 5)]
    inj = [_inj_s1(1), _inj_s1(2), _inj_t9(1, blog[0]), _inj_t9(2, social[0])]
    packs = {
        "s1:default": dict(system_prompt="Invented platform voice for tests.", forbidden_words=["zzbanned"]),
        f"t2:{SLUG_A}": dict(style_guide="Invented tenant style: short pretend sentences about made-up places.",
                             default_cta="Ask the pretend agency."),
        "t2:zz-unused-agency": dict(style_guide="An unused invented pack."),
    }
    return dict(schema=SCHEMA, fixture_version=version, created_at="2026-10-20T00:00:00+00:00", seed=729,
                mix=dict(s1=8, t2=4, t9_blog=4, t9_linkedin=4, injection=4), quotas=dict(s1=[["cycle_bike", 1]]),
                selection=dict(s1=dict(candidates=99, note="dropped: not a number")),
                shared_inputs=dict(brand_rules=packs), samples=s1 + t2 + blog + social + inj,
                label_history=[dict(from_version="0.9.0", to_version=version, applied=3)])
