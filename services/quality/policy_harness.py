"""AA-1047 (root ADR 0020, W1) — the contradiction harness: run one policy example through every fixer, gate and
rule that reads its field, and say what the system decides.

The examples are the fire / no-fire twins in services/quality/writing_policy.yaml (`examples`, synthetic only). For
each example every detector of its field gives `block`, `warn` or `pass`; the system's verdict is the most severe of
them (`combined`). The contradiction suite (tests/unit/quality/test_aa1047_contradictions.py) needs `combined` ==
the example's `expect` for 100% of them, AND every detector the example names in `all` (the detectors that own its
rule: every body fire twin names them) must reach `expect` on its own: `combined` alone hides a lax detector behind a
strict one, and two owners that disagree about one sentence are exactly the S271 code-vs-code contradiction. Fixers
are run too: a fixer's output must satisfy the policy (`fixed`), e.g. ensure_cta leaves the CTA line once, the slug
fold cuts at a word boundary, the meta fitter lands in the band or leaves the text for the gate to report.

Detectors per field (no model call, no I/O; `meals` stubs the Jev fallback as fail-closed):

- body: claim_rules.apply_rules (with the example's `claim` row: the type an extraction call would give; every body
  example has one), F1 gate_grounding, F17 / F18 gate_trip_facts, F12 gate_scope (the scope lexicon), and the admin
  sentence check sentence_repair.check_replacement (the edit / suggest path of /admin/content-quality; it runs F1,
  F2, F11, F19 and the lexicon checks, as on main; S272: not F17 / F18)
- meta / title / slug: F4 gate_seo_surface (that field only), the S1 band checks (s1_checks.check_meta,
  SEO_TITLE_MAX), the T9 acceptance (service._meta_ok) and the T9 short meta (S272: a meta inside the T9 writer aim
  but under the band goes to the fit call and, when the fit leaves it, stays as written for F4 to hold, as on main;
  code never composes a country tail; `route` adds route moments' seed blocks, as a T9 route piece's seed), the slug
  fold
  (service._ascii_slug) and the publish slug (idempotency.blog_slug) as fixers
- cta: claim_rules.check_cta; fixer hygiene.ensure_cta
- faq: drop_unanswerable_faq (topic needs a source line), F16 gate_faq_contract
- itinerary: S1 day oracle (s1_checks.check_days) for day counts; the brand audit meal-line check for meals
  (the example's `writers`, default A1 and T2; S272 fail-closed: the source-day meal line is exempt on A1 only)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from unittest import mock

from services.quality import writing_policy as wp

SEVERITY = {"pass": 0, "warn": 1, "block": 2}
BRAND = "Northpine Travel"
DEFAULT_CTA = "Ask Northpine Travel to plan your days in Orvine."
CTA_CHANNEL = "linkedin"   # the closing window of a short synthetic text: the last two paragraphs (check_cta)


@dataclass
class Outcome:
    example_id: str
    expect: str
    verdicts: dict[str, str] = field(default_factory=dict)    # detector -> block | warn | pass
    notes: dict[str, Any] = field(default_factory=dict)       # detector -> its findings (for a failure message)
    fixed: dict[str, bool] = field(default_factory=dict)      # fixer -> its output satisfies the policy

    @property
    def combined(self) -> str:
        return max(self.verdicts.values(), key=SEVERITY.get, default="pass")

    @property
    def ok(self) -> bool:
        return self.combined == self.expect and all(self.fixed.values())


def _seed(spec: dict) -> str:
    from services.acp_shared.atom_seed import compose_seed
    return compose_seed(spec["label"], spec.get("evidence"), spec.get("hook"), spec.get("season"),
                        list(spec.get("siblings") or []), place=spec.get("place"), country=spec.get("country"),
                        day=spec.get("day"), card=spec.get("card"))


def grounding_text(seed: str, facts: str = "") -> str:
    """The gates' SOURCE for one example, as service._grounding_text shapes it (seed, Facts, brand lines)."""
    parts = [seed, facts, f"BRAND: {BRAND}", "BRAND CORE IDEA: Small-group walking journeys.",
             "AUDIENCE (who the brand writes for — not a fact about this trip): Walkers aged 25-50",
             "TRIP: Velmora Highlands Walk", "DESTINATION: Velmora"]
    return "\n\n".join(p for p in parts if p)


def _sev_of_gate(g: dict) -> str:
    if g.get("passed"):
        return "pass"
    return "block" if g.get("blocking") else "warn"


def _apply_rules(text: str, grounding: str, claim: Optional[dict], cta: str) -> tuple[str, list]:
    from services.acp_content_writing import claim_rules as cr
    lines = cr.number_source_lines(grounding)
    sents = cr.split_piece(text)
    rows = {}
    if claim:
        for s in sents:
            rows[s.sid] = cr.ClaimRow(s.sid, type=claim.get("type", "generic"), ref=claim.get("ref", ""),
                                      quote=claim.get("quote", ""), places=tuple(claim.get("places") or ()))
    verdicts = cr.apply_rules(rows, lines, sents, cta=cta)
    sev = "pass"
    for v in verdicts:
        s = "block" if v.blocking else ("warn" if v.problem else "pass")
        sev = max(sev, s, key=SEVERITY.get)
    return sev, [(v.sentence, v.type, v.problem) for v in verdicts if v.problem]


def _body(ex: dict, out: Outcome, seed: str, grounding: str, cta: str) -> None:
    from services.acp_content_writing import hygiene, quality_gates as qg, sentence_repair
    from services.acp_content_writing.trip_facts import TripFactContext
    text = ex["text"]
    out.verdicts["apply_rules"], out.notes["apply_rules"] = _apply_rules(text, grounding, ex.get("claim"), cta)
    f1 = qg.gate_grounding(text, grounding)
    out.verdicts["F1_grounding"], out.notes["F1_grounding"] = _sev_of_gate(f1), f1["violations"]
    ctx = TripFactContext(seed=seed, cta=cta, channel="blog", brand_name=BRAND,
                          trip_places=tuple(ex.get("trip_places") or ()))
    for g in qg.gate_trip_facts(text, ctx):
        out.verdicts[g["gate"]], out.notes[g["gate"]] = _sev_of_gate(g), g["violations"]
    hyg = hygiene.HygieneContext(seed=seed, scope=ex.get("scope"), trip_name="Velmora Highlands Walk",
                                 brand_name=BRAND, cta=cta, trip_places=tuple(ex.get("trip_places") or ()))
    with mock.patch.dict("os.environ", {qg.F12_SCOPE_MODE_ENV: "block"}):
        f12 = qg.gate_scope(text, hyg)       # the policy's severity (block class), whatever the run-time mode
    out.verdicts["F12_scope"], out.notes["F12_scope"] = _sev_of_gate(f12), f12["violations"]
    # the admin sentence edit / suggest check (sentence_repair.check_replacement): the same sentence must not be
    # refused there when the pipeline only warns on it (or the reverse)
    wi = {"seed": seed, "scope": ex.get("scope"), "trip": "Velmora Highlands Walk", "cta": cta,
          "trip_places": list(ex.get("trip_places") or ()), "facts": ex.get("facts") or None}
    checks = sentence_repair.check_replacement(text, grounding, {"brand_name": BRAND}, writer_input=wi)
    out.verdicts["check_replacement"] = ("block" if any(c["blocking"] for c in checks)
                                         else "warn" if checks else "pass")
    out.notes["check_replacement"] = checks


def _seo(ex: dict, out: Outcome, seed: str) -> None:
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import service as svc
    from services.acp_publish.idempotency import blog_slug
    from services.content_generation.seo_meta_utils import SEO_TITLE_MAX
    from services.quality import s1_checks
    fld, text = ex["field"], ex["text"]
    title = text if fld == "title" else "Corran Monastery and the Old Library of Orvine"
    meta = text if fld == "meta" else ("Walk to Corran Monastery above Orvine, see its working library and follow "
                                       "the old pilgrim path back down to the lake shore.")
    slug = text if fld == "slug" else "corran-monastery-orvine"
    g = qg.gate_seo_surface(title, meta, slug, None)
    prefix = {"title": "SEO title", "meta": "meta description", "slug": "slug"}[fld]
    mine = [v for v in g["violations"] if v.startswith(prefix) or (fld == "slug" and v == "no slug")]
    out.verdicts["F4_seo_surface"], out.notes["F4_seo_surface"] = ("block" if mine else "pass"), mine
    if fld == "meta":
        from services.content_generation.seo_meta_utils import fit_seo_meta_final
        found = [f for f in s1_checks.check_meta({"seo_meta": meta}) if f["code"] == s1_checks.META_LENGTH]
        out.verdicts["s1_meta_band"], out.notes["s1_meta_band"] = ("block" if found else "pass"), found
        # fixer: the S1 deterministic fit lands the meta in the band, or leaves it for the gate to report
        lo, hi = wp.band("meta")
        new = fit_seo_meta_final(meta, {"duration": "2 days", "country": "Velmora", "name": "Velmora Highlands Walk"})
        out.fixed["meta_fit_band"] = lo <= len(new) <= hi or new == meta
        # the T9 acceptance of a writer / fit meta (service._meta_ok: the same band as F4)
        out.verdicts["t9_meta_ok"] = "pass" if svc._meta_ok(meta, None) else "block"
        # S272: the T9 writer aims under the band floor (writer_aims.meta.t9); a complete meta inside that aim but
        # under the band goes to the fit call (stubbed here to return nothing) and stays as written, so F4 holds it,
        # as on main: no deterministic country tail (withdrawn after three review rounds found wrong-country tails)
        aim_lo, _aim_hi = wp.writer_aim("t9")
        if aim_lo <= len(meta) < lo:
            import asyncio
            from uuid import uuid4
            ctx = {"destination": ex.get("destination", "Velmora"), "atom_text": seed,
                   "trip_name": "Velmora Highlands Walk"}
            with mock.patch.object(svc, "fit_seo_fields", return_value=({}, 0.0)) as fit:
                got, _cost = asyncio.run(svc._fit_seo_meta({"seo_title": title, "meta_description": meta}, None,
                                                           body="b", context=ctx, request_id=uuid4()))
            out.fixed["t9_short_meta_goes_to_the_fit_call"] = fit.called
            out.fixed["t9_short_meta_not_composed"] = got.get("meta_description") == meta
    if fld == "title":
        out.verdicts["s1_title_cap"] = "block" if len(title) > SEO_TITLE_MAX else "pass"
    if fld == "slug":
        folded = svc._ascii_slug(text)
        lo, hi = 0, wp.slug_max()
        out.fixed["ascii_slug_boundary"] = (lo < len(folded) <= hi and _at_boundary(folded, text))
        stem = blog_slug(text, "p:blog:abcdef0123456789").rsplit("-", 1)[0]
        out.fixed["blog_slug_boundary"] = len(stem) <= hi and _at_boundary(stem, text)


def _at_boundary(cut: str, original: str) -> bool:
    """`cut` is the whole kebab slug or ends where one of its words ends."""
    import re
    folded = re.sub(r"[^a-z0-9]+", "-", original.lower()).strip("-")
    return folded == cut or folded.startswith(cut + "-")


def _cta(ex: dict, out: Outcome, cta: str) -> None:
    from services.acp_content_writing import claim_rules as cr, hygiene
    text = ex["text"]
    chk = cr.check_cta(text, cta, CTA_CHANNEL)
    out.verdicts["check_cta"], out.notes["check_cta"] = ("pass" if chk.ok else "block"), chk.problem
    fixed, _log = hygiene.ensure_cta(text, cta)
    after = cr.check_cta(fixed, cta, CTA_CHANNEL)
    count = hygiene.cta_norm(fixed).count(hygiene.cta_norm(cta))
    out.fixed["ensure_cta_once"] = after.ok and count == wp.compiled()["cta"]["count"]


def _faq(ex: dict, out: Outcome, seed: str, facts: str) -> None:
    from services.acp_content_writing import hygiene, quality_gates as qg
    text = ex["text"]
    own = hygiene.split_seed(seed)[0]
    kept, dropped = qg.drop_unanswerable_faq(text, own, facts)
    out.verdicts["faq_topic_drop"], out.notes["faq_topic_drop"] = ("warn" if dropped else "pass"), dropped
    f16 = qg.gate_faq_contract(kept)
    out.verdicts["F16_faq"], out.notes["F16_faq"] = _sev_of_gate(f16), f16["violations"]


def _itinerary(ex: dict, out: Outcome) -> None:
    from services.content_generation import brand_audit_node as ba
    from services.quality import s1_checks
    tour = {"itineraries": ex["source_itinerary"], "duration": ex.get("duration", "")}
    text = ex["text"]
    if ex["topic"] == "day_count":
        found = s1_checks.check_days(tour, {"itineraries": text})
        out.verdicts["day_oracle"], out.notes["day_oracle"] = ("block" if found else "pass"), found
        return
    for writer in ex.get("writers") or wp.EXAMPLE_WRITERS:
        with mock.patch.object(ba, "_jev_clears_meal_claims", return_value=False):  # a Jev miss is fail-closed
            ok = ba.meal_time_claims_supported(text, tour, is_tenant_rewrite=writer == "t2")
        out.verdicts[f"meal_check_{writer}"] = "pass" if ok else "block"


def run_example(ex: dict, seeds: dict) -> Outcome:
    """The Outcome of one policy example (see the module docstring)."""
    out = Outcome(example_id=ex["id"], expect=ex["expect"])
    seed = _seed(seeds[ex.get("seed", "monastery")])
    if ex.get("route"):
        # a T9 route piece: one seed block per moment, joined by blank lines (service._reread_seed)
        seed = "\n\n".join([seed] + [_seed(seeds[k]) for k in ex["route"]])
    facts = ex.get("facts", "")
    grounding = grounding_text(seed, facts)
    cta = ex.get("cta", DEFAULT_CTA)
    fld = ex.get("field", "body")
    if fld == "body":
        _body(ex, out, seed, grounding, cta)
    elif fld in ("meta", "title", "slug"):
        _seo(ex, out, seed)
    elif fld == "cta":
        _cta(ex, out, cta)
    elif fld == "faq":
        _faq(ex, out, seed, facts)
    elif fld == "itinerary":
        _itinerary(ex, out)
    else:  # pragma: no cover — the schema test pins the fields
        raise ValueError(f"unknown example field {fld!r}")
    return out


def run_all(source: Optional[dict] = None) -> list[Outcome]:
    src = source if source is not None else wp.load_source()
    seeds = src.get("example_seeds") or {}
    return [run_example(ex, seeds) for ex in src.get("examples") or []]


__all__ = ["DEFAULT_CTA", "Outcome", "grounding_text", "run_all", "run_example"]
