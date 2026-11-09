"""AA-1049 W3 (root ADR 0020, eval item R0b) — free offline T9 replay with a declared-diff report.

No LLM, no DB, no network. It runs over stored T9 pieces: the golden fixture (root docs/eval/aa729/fixture-<v>.json,
the T9 samples) and/or a read-only export of acp_shared.content_piece rows with their repair_log. The stored gate
verdicts are the cassette: the F9 judge is never called; a piece whose F9 verdict is missing or was an outage is a
cassette MISS and is reported as "needs rejudge", never as a pass. An SEO-fit model call is a cassette miss too
(`fit_seo_fields` raises): the piece is counted as a miss (never a hit) and only the FITTED answer is left out of the
diff: the request sent to the fit (its kwargs) and the fields the soft-fail path ships during a fit outage are
deterministic and are compared.

Per piece it records, through the code of the checkout it runs in:

- input: the production input builder (piece_store.production_input, or service._grounding_text on a checkout
  without the seam) and its sha;
- route: sentence_repair.plan over the stored ledger (gates + sentences), hold_failure, never_holds, the style-only
  repair choice, the cut gates;
- seo (blog): the SEO-fit calls _fit_seo_meta would make, the request it would send and the fields it would keep (a
  call = an SEO miss), called with the production arguments: the HygieneContext run_write_background builds from the
  sample context (scope places, trip places, brand, CTA, channel spec, Facts) and the taken titles of the same
  tenant's other blog pieces in the replay set (production reads its 50 most recent; the replay set stands in for
  them). Plus a VERDICT that does not depend on the checkout's _fit_seo_meta flow: an independent copy of main's
  acceptance tests (band + keyword, scope places, taken title, unsupported products, date authority, rarity,
  authenticity) over the writer's values and the delivered values;
- guard (pieces with a repair_log): the AA-528 decision for each consecutive pair of attempt ledgers, main's gate-name
  view of both ledgers (service._regression_failures) and their hold gate (quality_gates.hold_failure) on every
  checkout, with the stable-key evidence (keys added / removed, key counts, gate sets) when the checkout has
  stable_keys;
- contract: the REPORT-mode final contract over the stored text; for a content_piece export with a stored contract
  (writer_input.final_contract), the gated sha vs the sha _finalize_piece stored (a mismatch = a post-gate edit in
  production) and vs the row's text now (a later tenant edit, reported only). Golden samples carry no stored
  contract: sha "n/a". The replay never re-runs the post-gate tail of run_write_background; the post-gate-edit
  guard of the code itself is tests/unit/quality/test_chain_order.py (+ the dry-store seam unit test).

Two runs make the diff: one on a main checkout (`--out base.json`), one on the branch (`--base base.json`). The
script itself only needs the modules of the checkout it runs in, so the branch copy of this file runs against main:

    PYTHONPATH=<main checkout> python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> --out base.json
    PYTHONPATH=<branch>        python <branch>/scripts/quality/replay_aa1049_t9.py --golden <fixture> \\
        --base base.json --out branch.json --report report.md

The report classifies every difference as a DECLARED diff (DECLARED below: one id per change this train makes on
purpose, checked against the declared SHAPE, not just the direction) or UNDECLARED. Exit code 1 when any difference
is undeclared, a piece is missing, a stored contract shows a post-gate edit, or the SEO context of a piece could not
be built (the replay would then check looser predicates than production). A flipped guard decision also reports the
piece status each side continues with (`guard_status_flips`).

S273 (trial follow-up): with T9_SEO_BUILDER=on a meta under the band floor is completed in code by one cited post
sentence (D-seo-3) or sent to the one fit call with the band stated (D-seo-4). Stored bodies (golden fixture,
content_piece export) have their citation tags stripped at store time, so a replay over them can show D-seo-4 only;
D-seo-3 is covered by unit tests and by a synthetic export whose body keeps its tags.

Defaults vs opt-in (S272 orchestrator decision): with every AA-1049 switch at its default (T9_REGRESSION_GUARD_MODE
unset = gate_names, T9_SEO_BUILDER unset = off) the branch must equal main: 0 declared and 0 undeclared diffs. The
two declared-diff families belong to opt-in switches only: D-seo-* to T9_SEO_BUILDER=on, D-guard-* to
T9_REGRESSION_GUARD_MODE=stable_keys. A diff is labelled with a declared id only when the run was started with
`--opt-in` AND the switch of that family is on in this run (recorded under "flags"); otherwise it is UNDECLARED
("opt-in diff without --opt-in"), so an opt-in run can never pass as a default-equals-main run.

Known limits of the declared-diff classifier (review S272 round 3; they matter only for an --opt-in run):

- SEO: the acceptance verdict (`_seo_predicates`) is computed with the checkout's own band and predicates
  (service._seo_title_ok / _meta_ok, hygiene, seo_grounding, trip_facts, authenticity). The only cross-check against
  main is that both sides judge the WRITER's values the same, so a loosened predicate that still fails the writer's
  value (for example a lower meta floor) certifies its own shortened value as D-seo-1. Not every predicate change shows
  as UNDECLARED; the unit tests of the predicates (test_aa1047_contradictions.py and the band tests) are the guard.
- Guard: D-guard-1 / D-guard-2 are decided on the branch's own stable-key COUNTS (stable_keys output). The gate sets
  are checked against main's gate-name view, the counts are not: a key-folding bug that keeps every gate name can
  certify itself. test_aa1049_stable_keys.py is the guard for the key extraction.
- Guard: the decision is taken through service.guard_decision when the checkout has it (main: the same rule written
  out here); the AA-780 floor-only exception and the revert wiring of the call site are not replayed (pinned by
  test_aa450 TestDiscardedAttempts and test_aa780), so a floor-only attempt-1 pair can differ from what production did.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

SCHEMA = "aa1049-t9-replay/1"
DECLARED = {
    "D-seo-1": ("opt-in T9_SEO_BUILDER=on: a title/meta the writer got wrong (it fails main's acceptance tests) is "
                "fixed in code by a whole-sentence / separator prefix of the writer's value that passes them: 0 "
                "SEO-fit calls when both fields then pass; else the fit is asked only about the rest and a fit outage "
                "ships the fixed field"),
    "D-seo-2": ("opt-in T9_SEO_BUILDER=on: at most one SEO-fit call per attempt (the retry call is gone); unit tests "
                "only: a replayed fit call is a cassette miss that aborts before main's retry, so the replay can "
                "never show it"),
    "D-seo-3": ("opt-in T9_SEO_BUILDER=on (S273): a meta the writer got wrong is completed in code: the writer's meta "
                "(or a whole-sentence prefix of it) plus ONE whole prose sentence of the post that cites a source line "
                "of the piece ([R:]/[F:] tag of its atom, route moments or Facts, tag removed), passing main's "
                "acceptance tests; the replay sees it only on bodies that still carry their tags (stored bodies are "
                "stripped: unit tests)"),
    "D-seo-4": ("opt-in T9_SEO_BUILDER=on (S273): the one SEO-fit call for a meta under the band floor states the "
                "policy band (`meta_band` = writing_policy band 'meta'); the rest of the request is main's"),
    "D-guard-1": ("opt-in T9_REGRESSION_GUARD_MODE=stable_keys: a same-gate swap of flagged sentences (same gate "
                  "set, as many keys or more) is reverted"),
    "D-guard-2": ("opt-in T9_REGRESSION_GUARD_MODE=stable_keys: clearing more findings than it adds is kept (the "
                  "piece status can flip: held->approved when the kept rewrite clears every hold gate, "
                  "approved->held when it traded style findings for a hold gate)"),
}
SEO_FIELDS = ("seo_title", "meta_description", "slug")
NOT_DECLARED = {
    "MISS": "an SEO-fit cassette miss on both sides with the same request and the same soft-fail fields: only the "
            "fitted answer is not compared",
}


class CassetteMiss(RuntimeError):
    """A model call the replay has no recorded answer for (never counted as a pass)."""


def _read_json(path: str) -> Any:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def load_golden(path: str) -> list[dict]:
    out = []
    for s in _read_json(path).get("samples") or []:
        if s.get("kind") != "t9":
            continue
        o = s.get("output") or {}
        ctx = (s.get("input") or {}).get("context") or {}
        out.append({"id": s["sample_id"], "channel": s.get("channel") or ctx.get("channel"), "context": ctx,
                    "content_text": o.get("content_text") or "", "seo_title": o.get("seo_title"),
                    "meta_description": o.get("meta_description"), "slug": o.get("slug"),
                    "gate_ledger": _json(o.get("gate_ledger")) or [], "repair_log": None,
                    "status": (s.get("pipeline_verdict") or {}).get("status")})
    return out


def load_pieces(path: str) -> list[dict]:
    out = []
    for r in _read_json(path) or []:
        wi = _json(r.get("writer_input")) or {}
        out.append({"id": str(r.get("piece_id") or r.get("id")), "channel": r.get("channel") or wi.get("channel"),
                    "context": wi.get("context") or wi, "writer_input": wi,
                    "content_text": r.get("content_text") or "", "seo_title": r.get("seo_title"),
                    "meta_description": r.get("meta_description"), "slug": r.get("slug"),
                    "gate_ledger": _json(r.get("gate_ledger")) or [], "repair_log": _json(r.get("repair_log")) or [],
                    "status": r.get("status")})
    return out


# ------------------------------------------------------------------------------------------------------- sections

def _input(p: dict) -> dict:
    try:
        from services.acp_content_writing import piece_store
        return {k: v for k, v in piece_store.production_input(p["context"]).items() if k != "grounding_text"}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 — an unbuildable input is reported, not fatal
        return {"error": type(e).__name__}
    try:
        import hashlib
        from services.acp_content_writing import service
        text = service._grounding_text(p["context"])
        return {"grounding_sha": hashlib.sha256(text.encode()).hexdigest()[:16]}
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


def _route(p: dict) -> dict:
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import sentence_repair, service
    ledger = p["gate_ledger"]
    plan = sentence_repair.plan(p["content_text"], ledger)
    hold = qg.hold_failure(ledger)
    failed = [g for g in qg.expand_ledger(ledger) if not g.get("passed")]
    return {
        "plan": None if plan is None else [[it.get("gate"), it.get("sentence", "")[:120]] for it in plan],
        "hold_failure": hold["gate"] if hold else None,
        "never_holds": {g["gate"]: qg.never_holds(g) for g in failed},
        "style_only_repair": service._style_only_repair(ledger),
        "truth_cut_gates": sorted(service.TRUTH_CUT_GATES), "cut_finding_gate": service.F17_CUT_GATE,
    }


def _cassette_hit(p: dict) -> bool:
    f9 = [g for g in p["gate_ledger"] if isinstance(g, dict) and g.get("gate") == "F9_cta_fact"]
    return bool(f9) and not any(g.get("degraded") or any("judge unavailable" in str(v) for v in g.get("violations")
                                                            or []) for g in f9)


SEO_CHECKED = ("seo_title", "meta_description")


def _hygiene(ctx: dict):
    """The HygieneContext run_write_background builds (service.py, after _add_date_spans), from the sample context."""
    from services.acp_content_writing import hygiene, service
    ba = ctx.get("brand_audience") or {}
    spec = service.channel_spec(ctx.get("channel"), ctx.get("brand_audience"))
    return hygiene.HygieneContext(seed=ctx.get("atom_text"), scope=ctx.get("scope"), trip_name=ctx.get("trip_name"),
                                  brand_name=ba.get("brand_name"), cta=ctx.get("cta"),
                                  hashtags_max=spec.hashtags_max if spec else None,
                                  first_line_max=spec.first_line_max if spec else None,
                                  audience=ba.get("customer_segment"), facts=ctx.get("facts_text") or "",
                                  trip_places=tuple(ctx.get("trip_places") or ()))


def _seo_predicates(ctx: dict, keyword: Optional[str], body: str, hyg, taken: list[str]) -> dict:
    """An independent copy of main's _fit_seo_meta acceptance tests (S234 rounds 2-4, AA-819), built from the
    module-level checks both checkouts have (imported from their own modules, not through service's names). The
    verdict it gives does not depend on the checkout's _fit_seo_meta flow, so a branch that ships a value its own flow
    accepted without checking (or with a loosened closure) shows a failing verdict."""
    from services.acp_content_writing import authenticity, hygiene, seo_grounding, service, trip_facts
    from services.acp_content_writing.quality_gates import title_taken
    lex = hyg.lexicon if hyg is not None else None
    seed = ctx.get("atom_text") or ""

    def title_ok(v) -> bool:
        return bool(service._seo_title_ok(v, keyword) and (lex is None or not hygiene.places_named(lex, v))
                    and not title_taken(v, taken) and not authenticity.field_hits(v))

    def meta_ok(v) -> bool:
        return bool(service._meta_ok(v, keyword) and (lex is None or not hygiene.places_named(lex, v))
                    and not seo_grounding.meta_unsupported_products(v, seed, body)
                    and not trip_facts.date_authority_claims("", seed, extra_texts=(("meta", v),))
                    and not trip_facts.rarity_claims("", hygiene.split_seed(seed)[0], extra_texts=(("meta", v),))
                    and not authenticity.field_hits(v))
    return {"seo_title": title_ok, "meta_description": meta_ok}


def _fit_request(kw: dict) -> dict:
    """The deterministic request of one SEO-fit call (the body as a sha: it is the piece's own text)."""
    import hashlib
    out = {k: v for k, v in kw.items() if k != "body"}
    out["body_sha"] = hashlib.sha256(str(kw.get("body") or "").encode()).hexdigest()[:16]
    return out


def _seo(p: dict, taken: Optional[list[str]] = None) -> Optional[dict]:
    if p["channel"] != "blog":
        return None
    from services.acp_content_writing import service
    calls: list[dict] = []

    def miss(**kw):
        calls.append(_fit_request(kw))
        raise CassetteMiss("t9_repair SEO fit")

    seo = {"seo_title": p["seo_title"], "meta_description": p["meta_description"], "slug": p["slug"]}
    ctx = dict(p["context"])
    taken = list(taken or [])
    try:
        service._add_date_spans(ctx)
        hyg, ctx_error = _hygiene(ctx), None
    except Exception as e:  # noqa: BLE001 — reported (exit 1): the replay would check looser predicates
        hyg, ctx_error = None, type(e).__name__
    keyword, body = ctx.get("keyword"), p["content_text"]
    with patch.object(service, "fit_seo_fields", side_effect=miss):
        out, _ = asyncio.run(service._fit_seo_meta(dict(seo), keyword, body=body, context=ctx, request_id="replay",
                                                   hyg=hyg, taken_titles=taken))
    ok = _seo_predicates(ctx, keyword, body, hyg, taken)
    cited = _cited_post_sentences(body, ctx)
    return {"fit_calls": len(calls), "miss": bool(calls), **{k: out.get(k) for k in SEO_FIELDS},
            "completion": _completion_shape(p["meta_description"], out.get("meta_description"), cited),
            "writer": {k: p[k] for k in SEO_FIELDS}, "fit_request": calls[0] if calls else None,
            "verdict": {"writer": {k: ok[k](p[k]) for k in SEO_CHECKED}, "out": {k: ok[k](out.get(k))
                                                                            for k in SEO_CHECKED}},
            "taken_titles": len(taken), "context_error": ctx_error}


OPT_IN = {"seo": ("T9_SEO_BUILDER", "on"), "guard": ("T9_REGRESSION_GUARD_MODE", "stable_keys")}


def run_flags() -> dict:
    """The AA-1049 switches as this checkout reads them (main has neither: both report their default)."""
    from services.acp_content_writing import service
    seo = service.seo_builder_mode() if hasattr(service, "seo_builder_mode") else "off"
    guard = service.regression_guard_mode() if hasattr(service, "regression_guard_mode") else "gate_names"
    return {"T9_SEO_BUILDER": seo, "T9_REGRESSION_GUARD_MODE": guard}


def _opted_families(flags: dict, opt_in: bool) -> set:
    """The declared-diff families this run may label: only with --opt-in, and only those whose switch is on."""
    if not opt_in:
        return set()
    return {fam for fam, (env, on) in OPT_IN.items() if (flags or {}).get(env) == on}


def _family(declared: str) -> Optional[str]:
    return "seo" if declared.startswith("D-seo") else "guard" if declared.startswith("D-guard") else None


def _guard(p: dict) -> Optional[list]:
    """One entry per consecutive pair of attempt ledgers: {"revert": the guard's decision in this checkout, "keys": the
    stable-key evidence (None on a checkout without stable_keys or in gate_names mode)}."""
    if not p.get("repair_log"):
        return None
    from services.acp_content_writing import service
    ledgers = [e.get("gate_ledger") for e in p["repair_log"] if isinstance(e, dict) and e.get("gate_ledger")]
    ledgers.append(p["gate_ledger"])
    from services.acp_content_writing import quality_gates as qg
    try:
        from services.acp_content_writing import stable_keys
        keys_mode = service.regression_guard_mode() == service.GUARD_STABLE_KEYS
    except (ImportError, AttributeError):
        stable_keys, keys_mode = None, False

    def hold(ledger):
        h = qg.hold_failure(ledger)
        return h["gate"] if h else None
    out = []
    for prev, cur in zip(ledgers, ledgers[1:]):
        # main's gate-name view and hold gate of both ledgers, on every checkout: the classifier checks the branch's
        # stable-key evidence against the base row's view, never against the branch's own (a key-extraction bug
        # would certify itself)
        pb, cb = service._regression_failures(prev), service._regression_failures(cur)
        row = {"gates": {"prev": sorted(pb), "cur": sorted(cb)}, "hold": {"prev": hold(prev), "cur": hold(cur)}}
        decide = getattr(service, "guard_decision", None)
        if decide is not None:  # the production decision function (before the AA-780 floor-only exception)
            new, revert = decide(prev, cur)[:2]
            revert = bool(new) and bool(revert)
        else:                   # main: its call-site rule, written out
            revert = bool(cb - pb) and len(cb) >= len(pb)
        if keys_mode:
            c = stable_keys.compare(prev, cur)
            out.append({"revert": revert, **row, "keys": {
                "new": sorted(map(list, c["new"])), "removed": sorted(map(list, c["removed"])),
                "prev_keys": c["prev_keys"], "cur_keys": c["cur_keys"],
                "prev_gates": sorted(c["prev_gates"]), "cur_gates": sorted(c["cur_gates"])}})
        else:
            out.append({"revert": revert, **row, "keys": None})
    return out


def _stored_sha(p: dict) -> dict:
    """The stored contract's shas of a content_piece export row: gated (text_sha, written at gate time), stored (what
    _finalize_piece received) and now (the row's text). No stored contract (a golden sample, a pre-W1 piece): n/a."""
    from services.quality import final_contract as fc
    rec = (p.get("writer_input") or {}).get("final_contract")
    if not isinstance(rec, dict) or not rec.get("text_sha"):
        return {"sha": "n/a"}
    gated, now = rec["text_sha"], fc.text_sha(p["content_text"])
    stored = rec.get("stored_sha") or now
    return {"sha": "match" if gated == stored else "post_gate_edit", "edited_since_store": stored != now}


def _contract(p: dict) -> dict:
    from services.acp_content_writing import service
    rec = service._t9_final_contract(
        p["content_text"], channel=p["channel"], cta=p["context"].get("cta"),
        seo_meta={k: p[k] for k in SEO_FIELDS}, gate_ledger=p["gate_ledger"], held_reason=None,
        status=p.get("status") or "approved")
    return {"verdict": (rec or {}).get("verdict"), "findings": sorted(str(f.get("code") if isinstance(f, dict) else f)
                                                                   for f in (rec or {}).get("findings") or [])}


def _taken_titles(p: dict, pieces: list[dict]) -> list[str]:
    """Production's _sibling_titles: the SEO titles of the same tenant's other pieces on the same channel (blog only),
    here from the replay set, sorted by piece id (at most 50, as production); [] without a tenant (as production)."""
    tenant = (p.get("context") or {}).get("tenant_id")
    if not tenant or p["channel"] != "blog":
        return []
    return [q["seo_title"] for q in sorted(pieces, key=lambda q: q["id"])
            if q is not p and q["id"] != p["id"] and q["channel"] == p["channel"]
            and (q.get("context") or {}).get("tenant_id") == tenant and isinstance(q.get("seo_title"), str)][:50]


def replay(pieces: list[dict]) -> dict:
    rows = {}
    for p in pieces:
        seo = _seo(p, _taken_titles(p, pieces))
        f9_hit = _cassette_hit(p)
        rows[p["id"]] = {"channel": p["channel"], "f9_hit": f9_hit, "seo_miss": bool(seo and seo["miss"]),
                         "cassette_hit": f9_hit and not (seo and seo["miss"]), "input": _input(p),
                         "route": _route(p), "seo": seo, "guard": _guard(p), "contract": _contract(p),
                         "stored": _stored_sha(p)}
    return {"schema": SCHEMA, "flags": run_flags(), "rows": rows}


# ---------------------------------------------------------------------------------------------------------- diff

def _seo_prefix(writer: Optional[str], value: Optional[str]) -> bool:
    """D-seo-1's shape: the value is the writer's own value or a non-empty prefix of it (whole sentences / the part
    before a separator: a shortening never adds a character)."""
    return value == writer or bool(value and writer and writer.startswith(value.rstrip()))


def _source_ids(ctx: dict) -> set:
    """The ids a citation tag of this piece may name (its atom, route moments, Facts), read from the sample context
    here, not through seo_builder (the code under review)."""
    import re
    ids = {str(ctx.get("atom_id") or "").strip()}
    ids |= {str(seg[0]).strip() for seg in ctx.get("route_segments") or () if isinstance(seg, (list, tuple)) and seg}
    ids |= {m.group(1).strip() for m in re.finditer(r"\[Fact id=([^\]]+)\]", ctx.get("facts_text") or "")}
    return {i for i in ids if i}


def _cited_post_sentences(body: str, ctx: dict) -> list[str]:
    """D-seo-3's anchor: the prose sentences of the post (outside the FAQ) whose citation tags all name a source line
    of the piece, tags removed (hygiene.sentence_spans + citation_tags, both on main)."""
    from services.acp_content_writing import hygiene
    from services.acp_shared.citation_tags import TAG_RE
    known = _source_ids(ctx)
    out = []
    for sp in hygiene.sentence_spans(body or ""):
        ids = [i.strip() for t in TAG_RE.findall(sp["text"]) for i in t.split(",")]
        if sp["kind"] == "prose" and not sp["in_faq"] and ids and known and all(i in known for i in ids):
            out.append(sp["clean"])
    return out


def _completion_shape(writer: Optional[str], value: Optional[str], cited: list[str]) -> bool:
    """D-seo-3's shape: value = B + ' ' + S where B is the writer's meta or a prefix of it ending a sentence and S is
    one cited post sentence that B does not already hold."""
    if not isinstance(value, str) or not isinstance(writer, str):
        return False
    w = " ".join(writer.split())
    for sent in cited:
        if value.endswith(" " + sent):
            base = value[:-len(sent) - 1]
            if (base and base.endswith((".", "!", "?")) and w.startswith(base) and sent.lower() not in base.lower()):
                return True
    return False


def _seo_fix_problem(changed: list[str], cur: dict, writer: dict, verdict: dict) -> tuple[Optional[str], set]:
    """(None, kinds) when every changed field has a declared shape: the writer's value fails main's acceptance tests
    and the new value passes them, and it is a strict prefix of the writer's value (kind 'prefix', D-seo-1) or, for
    the meta, its completion by one cited post sentence (kind 'completion', D-seo-3). Else (the reason, kinds)."""
    if not changed:
        return "no field changed", set()
    kinds: set = set()
    for k in changed:
        if cur.get(k) == writer.get(k):
            return f"{k} is not a prefix of the writer's value", kinds
        if _seo_prefix(writer.get(k), cur.get(k)):
            kinds.add("prefix")
        elif k == "meta_description" and cur.get("completion"):
            kinds.add("completion")
        else:
            return f"{k} is neither a prefix of the writer's value nor its completion by a cited sentence", kinds
        if verdict["writer"].get(k):
            return f"{k}: the writer's value already passed", kinds
        if not verdict["out"].get(k):
            return f"{k}: the value kept fails main's acceptance tests", kinds
    return None, kinds


def _fix_id(kinds: set) -> str:
    return "D-seo-3" if "completion" in kinds else "D-seo-1"


def _band_problem(kb: Optional[dict], kc: Optional[dict]) -> Optional[str]:
    """None when the branch's `meta_band` is main's, or absent on main and D-seo-4's shape: the policy meta band,
    sent only for a meta under its floor."""
    bb, bc = (kb or {}).get("meta_band"), (kc or {}).get("meta_band")
    if bc is None:
        return None if bb is None else "fit request lost main's meta_band"
    if bb is not None:
        return None if list(bb) == list(bc) else "fit request meta_band changed"
    from services.quality import writing_policy
    band = list(writing_policy.band("meta"))
    if list(bc) != band:
        return f"fit request meta_band {list(bc)} is not the policy band {band}"
    if len((kc or {}).get("meta_description") or "") >= band[0]:
        return "fit request states the band for a meta that is not under the floor"
    return None


def _without_band(k: Optional[dict]) -> dict:
    return {x: y for x, y in (k or {}).items() if x != "meta_band"}


def _fit_request_problem(kb: Optional[dict], kc: Optional[dict], cur: dict, changed: list[str]) -> Optional[str]:
    """None when the branch's fit request is main's with only the code-fixed fields replaced: the fit is asked about
    the values the code kept, forbidden words are a subset of main's (a fixed field has none left), the taken-title
    list is main's or gone with a fixed title, everything else identical."""
    if not kb or not kc:
        return "no fit request recorded"
    band = _band_problem(kb, kc)
    if band:
        return band
    for k in set(kb) | set(kc):
        if k in ("seo_title", "meta_description", "forbidden_words", "taken_titles", "meta_band"):
            continue
        if kb.get(k) != kc.get(k):
            return f"fit request {k} changed"
    for k in SEO_CHECKED:
        if kc.get(k) != cur.get(k) or (k not in changed and kc.get(k) != kb.get(k)):
            return f"fit request {k} is not the value the code kept"
    if not set(kc.get("forbidden_words") or []) <= set(kb.get("forbidden_words") or []):
        return "fit request has forbidden words main did not send"
    if kc.get("taken_titles") != kb.get("taken_titles") and not ("seo_title" in changed
                                                                  and kc.get("taken_titles") is None):
        return "fit request taken_titles changed"
    return None


def _classify_seo(base: dict, cur: dict) -> tuple[str, str]:
    vb, vc = base.get("verdict"), cur.get("verdict")
    if not vb or not vc:
        return "UNDECLARED", "no acceptance verdict recorded"
    if vb["writer"] != vc["writer"]:
        return "UNDECLARED", "the acceptance tests judge the writer's values differently from main"
    if cur.get("slug") != base.get("slug"):
        return "UNDECLARED", "slug changed"
    writer = cur.get("writer") or base.get("writer") or {}
    bc, cc = base.get("fit_calls", 0), cur.get("fit_calls", 0)
    if cc == 0 and bc > 0:
        changed = [k for k in SEO_CHECKED if cur.get(k) != writer.get(k)]
        bad, kinds = _seo_fix_problem(changed, cur, writer, vc)
        if bad is None and not all(vc["out"].values()):
            bad = "0 fit calls but a delivered field fails main's acceptance tests"
        if bad:
            return "UNDECLARED", f"0 fit calls where main made one: {bad}"
        return _fix_id(kinds), "fixed in code (prefix / cited-sentence completion of the writer's value), no fit call"
    if cc > 0 and bc > 0:
        changed = [k for k in SEO_CHECKED if cur.get(k) != base.get(k)]
        kb, kc = base.get("fit_request"), cur.get("fit_request")
        if not changed and kb == kc:
            return "MISS", "SEO-fit cassette miss on both sides, same request and soft-fail fields"
        if not changed:
            band = _band_problem(kb, kc)
            if band is None and _without_band(kb) == _without_band(kc) and (kc or {}).get("meta_band") is not None:
                return "D-seo-4", "SEO-fit cassette miss on both sides; the branch request states the policy meta band"
            return "UNDECLARED", f"fit outage path differs from main: {band or 'the fit request changed'}"
        bad, kinds = _seo_fix_problem(changed, cur, writer, vc)
        bad = bad or _fit_request_problem(kb, kc, cur, changed)
        if bad:
            return "UNDECLARED", f"fit outage path differs from main: {bad}"
        return _fix_id(kinds), ("fixed in code (prefix / cited-sentence completion of the writer's value); the fit is "
                                "asked only about the rest")
    if cc > 0 or bc > 0:
        return "UNDECLARED", "a fit call on one side only (not the D-seo-1 shape)"
    return "UNDECLARED", "different SEO fields"


def _classify_guard_pair(base: dict, cur: dict) -> tuple[str, str]:
    """One flipped guard decision. D-guard-1 (branch reverts, main kept): the new keys are all in gates the previous
    attempt already failed and the gate set is unchanged (a same-gate swap). D-guard-2 (branch keeps, main reverted):
    the rewrite has fewer keys. Anything else, or no stable-key evidence, is UNDECLARED."""
    k = cur.get("keys")
    if not k:
        return "UNDECLARED", "guard flipped without stable-key evidence"
    main_view = base.get("gates")
    if not main_view:
        return "UNDECLARED", "no main gate view in the base row"
    if sorted(k["prev_gates"]) != list(main_view["prev"]) or sorted(k["cur_gates"]) != list(main_view["cur"]):
        return "UNDECLARED", "stable-key evidence disagrees with main's gate view"
    prev_g, cur_g = set(k["prev_gates"]), set(k["cur_gates"])
    new_g = {g for g, _ in k["new"]}
    if cur["revert"] and not base["revert"]:
        if (k["new"] and new_g <= prev_g and cur_g == prev_g and len(cur_g) >= len(prev_g)
                and k["cur_keys"] >= k["prev_keys"]):
            return "D-guard-1", "stable keys revert a same-gate swap"
        return "UNDECLARED", (f"revert not a same-gate swap (new gates {sorted(new_g - prev_g)}, gates "
                              f"{len(prev_g)}->{len(cur_g)}, keys {k['prev_keys']}->{k['cur_keys']})")
    if base["revert"] and not cur["revert"]:
        if k["cur_keys"] < k["prev_keys"]:
            return "D-guard-2", "stable keys keep a rewrite with fewer findings"
        return "UNDECLARED", f"keep without fewer keys ({k['prev_keys']}->{k['cur_keys']})"
    return "UNDECLARED", "guard changed"


def _revert(x: Any) -> Any:
    return x.get("revert") if isinstance(x, dict) else x


def _status_flip(base: dict, cur: dict) -> Optional[str]:
    """The piece status each side continues with after a flipped guard decision, by main's hold view of the attempt it
    keeps (revert = the previous attempt): 'held->approved' / 'approved->held', or None. Exact for the last pair; an
    earlier pair only says which attempt the next round starts from (the stored later attempts followed main)."""
    hold = base.get("hold")
    if not hold:
        return None
    main = "held" if hold["prev" if base["revert"] else "cur"] else "approved"
    branch = "held" if hold["prev" if cur["revert"] else "cur"] else "approved"
    return None if main == branch else f"{main}->{branch}"


def _classify(section: str, base: Any, cur: Any) -> tuple[str, str]:
    """(declared id or 'UNDECLARED' / 'MISS', note) for one differing section (guard: one attempt pair) of a piece."""
    if section == "seo" and base and cur:
        return _classify_seo(base, cur)
    if section == "guard" and isinstance(base, dict) and isinstance(cur, dict):
        return _classify_guard_pair(base, cur)
    return "UNDECLARED", f"{section} changed"


def diff(base: dict, cur: dict, opt_in: bool = False) -> dict:
    out = _diff(base, cur)
    families = _opted_families(cur.get("flags") or {}, opt_in)
    for x in out["diffs"]:
        fam = _family(x["declared"])
        if fam is not None and fam not in families:
            env, on = OPT_IN[fam]
            x["note"] = (f"{x['declared']} shape, but an opt-in diff needs --opt-in and {env}={on} in this run "
                         f"({x['note']})")
            x["declared"] = "UNDECLARED"
    return {**out, "flags": {"base": base.get("flags"), "cur": cur.get("flags")}, "opt_in": sorted(families)}


def _diff(base: dict, cur: dict) -> dict:
    out: list[dict] = []
    missing = sorted(set(base["rows"]) ^ set(cur["rows"]))
    for pid in sorted(set(base["rows"]) & set(cur["rows"])):
        b, c = base["rows"][pid], cur["rows"][pid]
        for section in ("input", "route", "seo", "guard", "contract"):
            bs, cs = b.get(section), c.get(section)
            if section == "input" and "fs_sha" not in (bs or {}):
                b_in = {k: v for k, v in (cs or {}).items() if k in (bs or {})}
                if b_in == bs:
                    continue
            if section == "seo" and bs and cs:
                keys = ("fit_calls", "miss", *SEO_FIELDS, "verdict", "fit_request")
                if {k: bs.get(k) for k in keys} != {k: cs.get(k) for k in keys} or (bs.get("miss")
                                                                                   and cs.get("miss")):
                    did, note = _classify_seo(bs, cs)
                    out.append({"piece": pid, "section": section, "declared": did, "note": note})
                continue
            if section == "guard" and isinstance(bs, list) and isinstance(cs, list) and len(bs) == len(cs):
                for i, (bp, cp) in enumerate(zip(bs, cs)):
                    bp = bp if isinstance(bp, dict) else {"revert": bp, "keys": None}
                    cp = cp if isinstance(cp, dict) else {"revert": cp, "keys": None}
                    entry = {"piece": pid, "section": f"guard[{i}]"}
                    if bp.get("gates") != cp.get("gates") or bp.get("hold") != cp.get("hold"):
                        out.append({**entry, "declared": "UNDECLARED",
                                    "note": "the gate-name view or the hold gate of an attempt changed"})
                    elif _revert(bp) != _revert(cp):
                        did, note = _classify_guard_pair(bp, cp)
                        flip = _status_flip(bp, cp)
                        out.append({**entry, "declared": did, "note": note + (f"; status {flip}" if flip else ""),
                                    **({"status_flip": flip} if flip else {})})
                continue
            if bs != cs:
                did, note = _classify(section, bs, cs)
                out.append({"piece": pid, "section": section, "declared": did, "note": note})
    return {"diffs": out, "missing": missing}


def summary(cur: dict, d: Optional[dict]) -> dict:
    rows = cur["rows"].values()
    s = {"pieces": len(cur["rows"]), "by_channel": dict(Counter(r["channel"] for r in rows)),
         "cassette_hits": sum(r["cassette_hit"] for r in rows),
         "cassette_misses_f9_needs_rejudge": sum(not r["f9_hit"] for r in rows),
         "cassette_misses_seo_fit": sum(r["seo_miss"] for r in rows),
         "input_errors": sum(1 for r in rows if "error" in (r["input"] or {})),
         "stored_sha_na": sum(1 for r in rows if r["stored"]["sha"] == "n/a"),
         "stored_sha_match": sum(1 for r in rows if r["stored"]["sha"] == "match"),
         "post_gate_edit": sum(1 for r in rows if r["stored"]["sha"] == "post_gate_edit"),
         "edited_since_store": sum(1 for r in rows if r["stored"].get("edited_since_store")),
         "seo_fit_calls": sum((r["seo"] or {}).get("fit_calls", 0) for r in rows),
         "seo_context_errors": sum(1 for r in rows if (r["seo"] or {}).get("context_error")),
         "seo_taken_titles_max": max([(r["seo"] or {}).get("taken_titles", 0) for r in rows] or [0]),
         "guard_pairs": sum(len(r["guard"] or []) for r in rows)}
    if d is not None:
        ids = Counter(x["declared"] for x in d["diffs"])
        s["declared"] = {k: v for k, v in ids.items() if k in DECLARED}
        s["not_compared"] = {k: v for k, v in ids.items() if k in NOT_DECLARED}
        s["undeclared"] = sum(v for k, v in ids.items() if k not in DECLARED and k not in NOT_DECLARED)
        s["missing_pieces"] = len(d["missing"])
        s["guard_status_flips"] = dict(Counter(x["status_flip"] for x in d["diffs"] if x.get("status_flip")))
        s["opt_in"] = d.get("opt_in") or []
    s["flags"] = cur.get("flags") or {}
    return s


def report_md(s: dict, d: Optional[dict]) -> str:
    lines = ["# AA-1049 T9 replay — declared-diff report", "", f"schema `{SCHEMA}`", ""]
    lines += [f"- {k}: {v}" for k, v in s.items()]
    if d is not None:
        lines += ["", "## Differences", "", "| piece | section | declared | note |", "|---|---|---|---|"]
        lines += [f"| {x['piece']} | {x['section']} | {x['declared']} | {x['note']} |" for x in d["diffs"]] or ["|"]
        lines += ["", "## Declared diffs", ""] + [f"- **{k}** {v}" for k, v in {**DECLARED, **NOT_DECLARED}.items()]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--golden", action="append", default=[])
    ap.add_argument("--pieces", action="append", default=[])
    ap.add_argument("--base", help="the replay JSON of the main checkout to diff against")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report")
    ap.add_argument("--opt-in", action="store_true",
                    help="label diffs of an opt-in switch that is on in this run (T9_SEO_BUILDER=on, "
                         "T9_REGRESSION_GUARD_MODE=stable_keys) with their declared id; without it every diff is "
                         "UNDECLARED (a default run must equal main)")
    a = ap.parse_args(argv)
    pieces = [p for g in a.golden for p in load_golden(g)] + [p for f in a.pieces for p in load_pieces(f)]
    cur = replay(pieces)
    d = diff(_read_json(a.base), cur, opt_in=a.opt_in) if a.base else None
    s = summary(cur, d)
    Path(a.out).write_text(json.dumps({**cur, "summary": s, "diff": d}, indent=1, default=str), encoding="utf-8")
    if a.report:
        Path(a.report).write_text(report_md(s, d), encoding="utf-8")
    print(json.dumps(s, indent=1))
    bad = s.get("undeclared", 0) or s["post_gate_edit"] or s.get("missing_pieces", 0) or s["seo_context_errors"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
