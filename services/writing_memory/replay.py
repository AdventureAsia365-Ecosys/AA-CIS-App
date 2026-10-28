"""AA-918 WM-15 — R0: the $0 replay of one writing rule over stored text, gate ledgers, repair logs and writing
signals (app ADR 0010 decision 6, D11; the counterfactual method of AA-865). No model call, nothing written but the
result on the rule (`shared.writing_rule.lint.r0`).

What R0 answers for a rule (its phrases: a lexicon rule's phrase plus its match_phrases, masked like the miner's):
  signals      the writing signals of the rule's writer and owner (90 days) it matches: a human edit that removed one of
               its phrases (lint.explains), or a system signal (a discarded T9 attempt, an A1 gate failure) whose text
               held one;
  recurrence   the matching signals of the last 30 days, held against the mining threshold of the rule's cluster basis
               (`mining.cluster_threshold`: the candidate filter's k IS the mining threshold, WM-15 scope 2). A rule
               under it is not `r2_eligible` — it stays visible and is reconsidered next month;
  pieces       (T9 writers) the stored pieces of the writer's channels and owner: how many texts (final text,
               discarded attempts, the writer's text before a human edit) hold the phrase — how often the rule would
               fire — and which stored failures (final ledger, every repair_log attempt, discarded attempts) quote it:
               the past failures it would have prevented;
  baseline     the sanity check (WM-15 done-when 1): the deterministic T10 gates re-run on each stored final text and
               discarded attempt, with no rule, against the ledger stored with that text — per gate, how often the
               replay agrees. The F9 judge verdicts are the stored ones (run_quality_gates `f9_results`) and the
               cross-tenant lookup is not run, so F9 / F10 are not compared;
  L0 verdict   (a lexicon rule of a T9 writer) each text with the phrase, re-run three ways: as stored with no rule
               (base), with the rule in the pack (on: the gate now flags the phrase), and with the phrase replaced by
               the replacement (or cut for a ban) with the rule on (counterfactual). `l0_ok` = the phrase occurs in
               stored text, the counterfactual holds it 0 times, and no blocking gate fails in the counterfactual that
               passed in the base (0 new hard stops) — plus a valid phrase and a replacement that adds nothing.

The result holds counts, gate names and ids only — never text — so a platform rule row still holds no tenant text
(decision 10). `rule_sha` ties it to the rule's text: an edit makes it stale, and activation needs a fresh one.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import structlog

from services.writing_memory import config as cfg
from services.writing_memory import l0
from services.writing_memory.lint import explains
from services.writing_memory.masking import contains_phrase
from services.writing_memory.rules import owner_filter

logger = structlog.get_logger()

R0_VERSION = 1
R0_MAX_PIECES = 300              # the newest stored pieces of the writer and owner (Dev holds ~300 T9 pieces in all)
R0_MAX_TEXTS_PER_PIECE = 6       # final text + discarded attempts + before-edit texts, deduplicated
RECURRENCE_DAYS = 30             # WM-15 scope 2: "recurred >= k times in 30 days"
_SAMPLE_IDS = 10                 # piece ids kept per list in the result
# A pause between pieces: the gate replay is CPU-bound Python in a thread, and the worker's event loop (wave jobs run
# beside it) gets a clean slot of the GIL each time. 300 pieces cost ~6 s more in all.
R0_YIELD_SECONDS = 0.02

# gates a replay cannot re-decide: the F9 judge (stored verdicts are passed through) and the cross-tenant lookup
_NOT_COMPARED_PREFIXES = ("F9_", "F10_")


# ── small pure helpers ──────────────────────────────────────────────────────────────────────────────────────────────

def rule_sha(rule: dict) -> str:
    """The identity of what R0 replayed: kind, writer, owner, text, replacement, phrases, channels. A text edit
    changes it (the stored R0 is then stale); a status change does not."""
    key = json.dumps([rule.get("kind"), rule.get("writer"), rule.get("scope"), rule.get("tenant_id"),
                      rule.get("rule_text"), rule.get("replacement"), list(rule.get("match_phrases") or []),
                      sorted(rule.get("applies_to") or [])], default=str)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def phrases_for(rule: dict) -> list[str]:
    """What the rule targets: a lexicon rule's phrase first, then its match_phrases (deduplicated, non-empty)."""
    out = [l0.phrase_of(rule)] if rule.get("kind") == "lexicon" else []
    out += [p for p in rule.get("match_phrases") or [] if p and p.strip().upper() != "NONE"]
    return list(dict.fromkeys(p.strip() for p in out if p and p.strip()))


def _gate_re(phrase: str) -> re.Pattern:
    # the F2_brand_forbidden match (quality_gates._forbidden_hits): whole phrase, case-insensitive
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def phrase_count(text: Optional[str], phrase: str) -> int:
    return len(_gate_re(phrase).findall(text or "")) if phrase else 0


def counterfactual(text: str, phrase: str, replacement: Optional[str]) -> str:
    """The text as if the writer had followed the rule: the phrase replaced (or cut, for a ban), whitespace tidied."""
    out = _gate_re(phrase).sub(replacement or "", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
    return re.sub(r"(^|\n)[ \t]+", r"\1", out)


_QUOTE_RE = re.compile(r"[\"'“”‘’`]")


def _unquoted(text: Optional[str]) -> str:
    # masking.tokens keeps a closing quote on a word ("special'"), and gate violations quote their sentence
    return _QUOTE_RE.sub(" ", text or "")


def matches_text(text: Optional[str], phrases: Iterable[str]) -> bool:
    return any(contains_phrase(_unquoted(text), _unquoted(p)) for p in phrases)


def signal_matches(sig: dict, phrases: list[str]) -> bool:
    """A human edit that removed a phrase, or a system signal (no after text) whose text or gate problem holds one."""
    if sig.get("actor_type") in ("admin", "tenant") and sig.get("after_text") is not None:
        return explains([_unquoted(p) for p in phrases], _unquoted(sig.get("before_text")),
                        _unquoted(sig.get("after_text")))
    return matches_text(sig.get("before_text"), phrases) or matches_text(sig.get("problem"), phrases)


def stored_failures(piece: dict) -> list[tuple[str, bool, str]]:
    """(gate, blocking, violation) of every failed entry stored on a piece: the final ledger, each repair_log attempt
    (its violations and its ledger snapshot) and each discarded attempt's ledger, on the legacy view."""
    from services.acp_content_writing.quality_gates import expand_ledger

    ledgers = [piece.get("gate_ledger") or []]
    for e in piece.get("repair_log") or []:
        if not isinstance(e, dict):
            continue
        if e.get("gate_targeted") and e.get("violations"):
            ledgers.append([{"gate": e["gate_targeted"], "passed": False, "blocking": True,
                             "violations": e["violations"]}])
        if isinstance(e.get("gate_ledger"), list):
            ledgers.append(e["gate_ledger"])
    for d in piece.get("discarded_attempts") or []:
        if isinstance(d, dict) and isinstance(d.get("gate_ledger"), list):
            ledgers.append(d["gate_ledger"])
    out = []
    for led in ledgers:
        for g in expand_ledger([x for x in led if isinstance(x, dict)]):
            if g.get("passed", True):
                continue
            for v in g.get("violations") or []:
                out.append((str(g.get("gate")), bool(g.get("blocking", True)), str(v)))
    return list(dict.fromkeys(out))


def blocking_failed(ledger: list) -> set[str]:
    from services.acp_content_writing.quality_gates import expand_ledger
    return {g["gate"] for g in expand_ledger(ledger or []) if g.get("blocking", True) and not g.get("passed", True)}


def _compared(gate: str) -> bool:
    return not str(gate).startswith(_NOT_COMPARED_PREFIXES)


def baseline_compare(stored: list, replayed: list) -> dict[str, tuple[bool, bool]]:
    """gate -> (stored passed, replayed passed) for the gates both ledgers hold (legacy view), F9 / F10 excluded."""
    from services.acp_content_writing.quality_gates import expand_ledger
    s = {g.get("gate"): bool(g.get("passed")) for g in expand_ledger(stored or []) if isinstance(g, dict)}
    r = {g.get("gate"): bool(g.get("passed")) for g in expand_ledger(replayed or []) if isinstance(g, dict)}
    return {g: (s[g], r[g]) for g in s.keys() & r.keys() if g and _compared(g)}


# ── the gate replay (sync, CPU only) ────────────────────────────────────────────────────────────────────────────────

def _stored_f9(ledger: list) -> list[dict]:
    """The stored F9 verdicts of a ledger, or passes (an old piece without them)."""
    from services.acp_content_writing import quality_gates as qg
    from services.quality import registry as qr
    by = {g.get("gate"): g for g in qg.expand_ledger(ledger or []) if isinstance(g, dict)}
    out = []
    for gate, blocking in ((qr.F9_CTA_FACT, True), (qr.F9_BRAND_STYLE, False)):
        out.append(copy.deepcopy(by[gate]) if gate in by else qg._result(gate, [], blocking=blocking))
    return out


class PieceReplay:
    """The gate inputs of one stored T9 piece, rebuilt from its `writer_input` the way the writer builds them
    (service.run_write_background; scripts/aa729/score.py::t9_gate_replay does the same offline)."""

    def __init__(self, writer_input: dict, channel: str, seo: Optional[dict] = None):
        from services.acp_content_writing import service as svc
        wi = writer_input or {}
        angle = wi.get("angle") or {}
        ctx = {"atom_text": wi.get("seed") or "",
               "route_segments": [(m.get("atom_id"), m.get("seed")) for m in wi.get("route_moments") or []
                                  if isinstance(m, dict)],
               "facts_text": wi.get("facts") or "", "scope": wi.get("scope"),
               "brand_audience": wi.get("brand_pack") or {}, "brand_rubric_text": wi.get("brand_rubric") or "",
               "chosen": angle, "channel": wi.get("channel") or channel, "keyword": wi.get("keyword"),
               "cta": wi.get("cta"), "trip_name": wi.get("trip"), "destination": wi.get("destination"),
               "trip_places": wi.get("trip_places") or []}
        svc._add_date_spans(ctx)
        self.channel = ctx["channel"]
        ba = ctx["brand_audience"]
        facts_text = ctx["facts_text"]
        spec = svc.channel_spec(self.channel, ba)
        hyg = svc.hygiene.HygieneContext(seed=ctx["atom_text"], scope=ctx.get("scope"), trip_name=ctx.get("trip_name"),
                                         brand_name=ba.get("brand_name"), cta=ctx.get("cta"),
                                         hashtags_max=spec.hashtags_max if spec else None,
                                         first_line_max=spec.first_line_max if spec else None,
                                         audience=ba.get("customer_segment"), facts=facts_text,
                                         trip_places=tuple(ctx.get("trip_places") or ()))
        trip_ctx = svc.trip_facts.TripFactContext(
            seed=ctx["atom_text"], source=f"{hyg.lexicon.own_text}\n{facts_text}", audience=ba.get("customer_segment"),
            cta=ctx.get("cta"), trip_places=tuple(ctx.get("trip_places") or ()), channel=self.channel,
            brand_name=ba.get("brand_name"))
        briefs = svc.hygiene.brief_texts(ba, ctx.get("chosen"))
        ctx["writer_trip_name"] = svc.hygiene.scoped_trip_name(ctx.get("trip_name"), ctx.get("scope"), hyg.lexicon)
        # The rule-off base: the pack as the tenant wrote it. A piece written while an L0 rule was active stored the
        # pack with the learned phrases on its forbidden list; replaying with them would count that base as rule-on.
        self.stored_pack = ba
        self.pack = l0.without_learned(ba)
        self.atom_text = svc._grounding_text(ctx)
        self.cta = ctx.get("cta")
        self.trip_name = ctx.get("trip_name")
        seo = seo or {}
        blog = self.channel == "blog"
        self._kw = dict(atom_text=self.atom_text, cta=ctx.get("cta"),
                        brand_rubric_text=ctx.get("brand_rubric_text") or "", channel=self.channel,
                        keyword=ctx.get("keyword"), seo_title=seo.get("seo_title") if blog else None,
                        meta_description=seo.get("meta_description") if blog else None,
                        slug=seo.get("slug") if blog else None, cannibalization_match=None, hygiene_ctx=hyg,
                        sibling_texts=[], trip_ctx=trip_ctx, brief_texts=briefs, taken_titles=[])

    def run(self, text: str, *, pack: Optional[dict], f9: list[dict], stock_blocking: bool) -> list[dict]:
        from services.acp_content_writing.quality_gates import run_quality_gates
        out = run_quality_gates(content_text=text or "", brand_pack=pack, f9_results=f9,
                                stock_blocking=stock_blocking, **self._kw)
        return out["gate_ledger"]


def _forbidden_violations(ledger: list, phrase: str) -> int:
    """F2_brand_forbidden findings that quote `phrase` (legacy view)."""
    from services.acp_content_writing.quality_gates import expand_ledger
    from services.quality import registry as qr
    n = 0
    for g in expand_ledger(ledger or []):
        if g.get("gate") == qr.F2_BRAND_FORBIDDEN:
            n += sum(1 for v in g.get("violations") or [] if phrase_count(str(v), phrase))
    return n


def replay_piece(piece: dict, rule: Optional[dict], texts: list[tuple[str, str, Optional[list], bool]],
                 *, entry: Optional[dict], cache: Optional[dict] = None) -> dict:
    """One piece: the baseline comparison of each text that has a stored ledger, and, with an L0 `entry`
    ({phrase, replacement}), the base / on / counterfactual runs of each text holding the phrase.
    `texts`: (kind, text, stored ledger or None, stock_blocking). `cache` (one per job): the rebuilt inputs and the
    rule-off runs of each piece, shared by every rule of the job (a run costs ~0.3 s of CPU).
    Rule off = the pack without the learned phrases a stored pack may carry (`l0.without_learned`); the baseline
    compares the stored ledger with a run on the pack as stored (what the writer ran)."""
    out = {"baseline": {}, "texts": 0, "with_phrase": 0, "source_exempt": 0, "fires": 0,
           "before": 0, "after": 0, "new_hard_stops": Counter(), "error": None}
    cache = {} if cache is None else cache
    pid = str(piece.get("piece_id"))
    try:
        pr = cache.get(("inputs", pid))
        if pr is None:
            pr = cache[("inputs", pid)] = PieceReplay(piece.get("writer_input") or {}, piece.get("channel") or "blog",
                                                      {k: piece.get(k) for k in ("seo_title", "meta_description",
                                                                                 "slug")})
    except Exception as e:  # noqa: BLE001 — a piece whose stored input cannot be rebuilt is counted, never fatal
        out["error"] = f"inputs: {type(e).__name__}: {str(e)[:160]}"
        return out
    from services.acp_content_writing import quality_gates as qg
    f9 = _stored_f9(piece.get("gate_ledger") or [])
    on_pack = l0.apply_to_pack(pr.pack, [{"rule_id": "r0", "scope": (rule or {}).get("scope"), **entry}]) \
        if entry else None
    for _kind, text, stored, stock_blocking in texts:
        out["texts"] += 1
        key = ("base", pid, hashlib.sha256((text or "").encode("utf-8")).hexdigest(), stock_blocking)
        try:
            base = cache.get(key)
            if base is None:
                base = cache[key] = pr.run(text, pack=pr.pack, f9=f9, stock_blocking=stock_blocking)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"replay: {type(e).__name__}: {str(e)[:160]}"
            continue
        if stored is not None:
            # The sanity check replays what the writer ran: with a learned phrase on the stored pack, that pack.
            as_written = base
            if pr.stored_pack is not pr.pack:
                skey = ("stored",) + key[1:]
                try:
                    as_written = cache.get(skey)
                    if as_written is None:
                        as_written = cache[skey] = pr.run(text, pack=pr.stored_pack, f9=f9,
                                                          stock_blocking=stock_blocking)
                except Exception as e:  # noqa: BLE001
                    out["error"] = f"replay: {type(e).__name__}: {str(e)[:160]}"
                    as_written = None
            for gate, pair in (baseline_compare(stored, as_written).items() if as_written is not None else ()):
                agg = out["baseline"].setdefault(gate, [0, 0, 0, 0])   # compared, agree, stored_fail, replay_fail
                agg[0] += 1
                agg[1] += int(pair[0] == pair[1])
                agg[2] += int(not pair[0])
                agg[3] += int(not pair[1])
        if not entry:
            continue
        phrase = entry["phrase"]
        n = phrase_count(text, phrase)
        if not n:
            continue
        out["with_phrase"] += 1
        out["before"] += n
        if qg.forbidden_exempt(phrase, pr.atom_text):
            out["source_exempt"] += 1     # the seed has it: the gate never flags it (its own predicate), the writer may
            continue                      # use it
        try:
            on = pr.run(text, pack=on_pack, f9=f9, stock_blocking=stock_blocking)
            cf_text = counterfactual(text, phrase, entry.get("replacement"))
            cf = pr.run(cf_text, pack=on_pack, f9=f9, stock_blocking=stock_blocking)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"replay: {type(e).__name__}: {str(e)[:160]}"
            continue
        if _forbidden_violations(on, phrase) > _forbidden_violations(base, phrase):
            out["fires"] += 1
        out["after"] += phrase_count(cf_text, phrase)
        for gate in sorted(blocking_failed(cf) - blocking_failed(base)):
            out["new_hard_stops"][gate] += 1
    return out


# ── loading ─────────────────────────────────────────────────────────────────────────────────────────────────────────

_SIGNALS_SQL = """
    SELECT signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, field, before_text,
           after_text,
           gate, problem, action, actor_type, actor_id, fact_status, source_table, source_id, created_at
      FROM shared.writing_signal
     WHERE writer = $1 AND created_at >= now() - make_interval(days => $2)
       AND ($3::uuid IS NULL OR tenant_id = $3::uuid)
     ORDER BY created_at DESC
"""
# The newest T9 pieces of the writer's channels and owner. A platform rule replays every tenant's pieces: it applies to
# every tenant; the result keeps counts and ids only.
_PIECES_SQL = """
    SELECT piece_id::text AS piece_id, tenant_id::text AS tenant_id, channel, status, attempt_number, content_text,
           gate_ledger, repair_log, discarded_attempts, writer_input, seo_title, meta_description, slug, created_at
      FROM acp_shared.content_piece
     WHERE status IN ('approved', 'held')
       AND (coalesce(channel, 'blog') = 'blog') = ($1 = 't9_blog')
       AND (cardinality($2::text[]) = 0 OR channel = ANY($2::text[]))
       AND ($3::uuid IS NULL OR tenant_id = $3::uuid)
       AND created_at >= now() - make_interval(days => $4)
     ORDER BY created_at DESC
     LIMIT $5
"""


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


async def load_evidence(conn, rule: dict) -> tuple[list[dict], list[dict]]:
    """(signals, pieces) of the rule's writer and owner: a tenant rule reads its tenant only, a platform rule every
    owner (decision 10: the result never quotes them)."""
    scope, tenant = owner_filter(rule.get("tenant_id")) if rule.get("scope") == "tenant" else ("platform", None)
    sigs = [dict(r) for r in await conn.fetch(_SIGNALS_SQL, rule["writer"], cfg.SIGNAL_WINDOW_DAYS, tenant)]
    pieces: list[dict] = []
    if rule["writer"] in l0.L0_WRITERS:
        rows = await conn.fetch(_PIECES_SQL, rule["writer"], list(rule.get("applies_to") or []), tenant,
                                cfg.SIGNAL_WINDOW_DAYS, R0_MAX_PIECES)
        for r in rows:
            p = dict(r)
            for k in ("gate_ledger", "repair_log", "discarded_attempts", "writer_input"):
                p[k] = _json(p[k])
            pieces.append(p)
    return sigs, pieces


def texts_of(piece: dict, signals_by_piece: dict[str, list[dict]]) -> list[tuple[str, str, Optional[list], bool]]:
    """(kind, text, stored ledger, stock_blocking) of one piece: its final text (with its stored ledger), each
    discarded attempt (with its own), and the writer's text before each human edit of it (no ledger stored)."""
    out, seen = [], set()

    def add(kind, text, ledger, attempt):
        key = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        if not (text or "").strip() or key in seen or len(out) >= R0_MAX_TEXTS_PER_PIECE:
            return
        seen.add(key)
        out.append((kind, text, ledger, int(attempt or 1) <= 1))
    add("final", piece.get("content_text"), piece.get("gate_ledger") or [], piece.get("attempt_number"))
    for d in piece.get("discarded_attempts") or []:
        if isinstance(d, dict):
            add("discarded", d.get("content_text"), d.get("gate_ledger") if isinstance(d.get("gate_ledger"), list)
                else None, d.get("attempt_number"))
    for s in signals_by_piece.get(piece["piece_id"], []):
        if s.get("actor_type") in ("admin", "tenant") and s.get("field") in (None, "content_text"):
            add("before_edit", s.get("before_text"), None, 1)
    return out


# ── the run ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def l0_verdict(rule: dict, *, before: int, after: int, new_hard_stops: dict) -> tuple[bool, Optional[str]]:
    """(l0_ok, why not) of a rule. Only a lexicon rule of a T9 writer has an L0 path."""
    if rule.get("kind") != "lexicon":
        return False, "not a lexicon rule (it waits for a release)"
    if rule.get("writer") not in l0.L0_WRITERS:
        return False, f"no L0 reader for writer {rule.get('writer')} yet (T9 only)"
    phrase = l0.phrase_of(rule)
    problem = l0.phrase_problem(phrase)
    if problem:
        return False, problem
    rep = l0.replacement_check(phrase, rule.get("replacement"))
    if not rep["ok"]:
        return False, f"the replacement adds {rep['detail']}"
    if before == 0:
        return False, "the phrase appears in no stored text of this writer and owner: nothing to prove"
    if after:
        return False, f"the phrase is still there {after} time(s) after the replacement"
    if new_hard_stops:
        return False, "new hard stops: " + ", ".join(f"{g} x{n}" for g, n in sorted(new_hard_stops.items()))
    return True, None


async def run_r0(pool, rule: dict, *, a1_pieces_30d: Optional[int] = None,
                 now: Optional[datetime] = None, cache: Optional[dict] = None) -> dict:
    """R0 for one rule ($0): the result (also what `rules.store_r0` keeps on the rule). `cache`: see replay_piece."""
    from services.writing_memory import mining

    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        sigs, pieces = await load_evidence(conn, rule)
        if a1_pieces_30d is None:
            a1_pieces_30d = await mining.a1_piece_count(conn) if rule["writer"] == "a1" else 0
    phrases = phrases_for(rule)
    usable = [s for s in sigs if s.get("fact_status") != "fail"]          # quarantined signals prove nothing
    matched = [s for s in usable if signal_matches(s, phrases)]
    since = now - timedelta(days=RECURRENCE_DAYS)
    recent = [s for s in matched if s.get("created_at") is None or s["created_at"] >= since]
    basis = (rule.get("lint") or {}).get("basis") if isinstance(rule.get("lint"), dict) else None
    k = mining.cluster_threshold(basis, a1_pieces_30d=a1_pieces_30d or 0)
    have = mining.support_of(recent)

    by_piece: dict[str, list[dict]] = {}
    for s in sigs:
        if s.get("source_table") == "content_piece" and s.get("source_id"):
            by_piece.setdefault(str(s["source_id"]), []).append(s)
    entry = None
    if rule.get("kind") == "lexicon" and rule["writer"] in l0.L0_WRITERS and not l0.phrase_problem(l0.phrase_of(rule)):
        entry = {"phrase": l0.phrase_of(rule), "replacement": rule.get("replacement") or None}

    prevented: Counter = Counter()
    prevented_blocking = 0
    prevented_pieces: list[str] = []
    totals = {"pieces": len(pieces), "with_writer_input": 0, "texts": 0, "with_phrase": 0, "source_exempt": 0,
              "fires": 0, "errors": 0}
    before = after = 0
    hard: Counter = Counter()
    hard_pieces: list[str] = []
    fire_pieces: list[str] = []
    baseline: dict[str, list[int]] = {}
    pieces_compared = pieces_agree = 0
    for p in pieces:
        hit = False
        for gate, blocking, v in stored_failures(p):
            if matches_text(v, phrases):
                prevented[gate] += 1
                prevented_blocking += int(blocking)
                hit = True
        if hit and len(prevented_pieces) < _SAMPLE_IDS:
            prevented_pieces.append(p["piece_id"])
        if not isinstance(p.get("writer_input"), dict) or not p["writer_input"]:
            continue
        totals["with_writer_input"] += 1
        res = await asyncio.to_thread(replay_piece, p, rule, texts_of(p, by_piece), entry=entry, cache=cache)
        await asyncio.sleep(R0_YIELD_SECONDS)
        totals["errors"] += int(bool(res["error"]))
        for key in ("texts", "with_phrase", "source_exempt", "fires"):
            totals[key] += res[key]
        before += res["before"]
        after += res["after"]
        if res["fires"] and len(fire_pieces) < _SAMPLE_IDS:
            fire_pieces.append(p["piece_id"])
        if res["new_hard_stops"]:
            hard.update(res["new_hard_stops"])
            if len(hard_pieces) < _SAMPLE_IDS:
                hard_pieces.append(p["piece_id"])
        if res["baseline"]:
            pieces_compared += 1
            pieces_agree += int(all(v[0] == v[1] for v in res["baseline"].values()))
        for gate, v in res["baseline"].items():
            agg = baseline.setdefault(gate, [0, 0, 0, 0])
            for i in range(4):
                agg[i] += v[i]
    compared = sum(v[0] for v in baseline.values())
    agree = sum(v[1] for v in baseline.values())
    ok, why = l0_verdict(rule, before=before, after=after, new_hard_stops=dict(hard))
    eligible = mining.meets(recent, k)
    return {
        "version": R0_VERSION, "computed_at": now.isoformat(), "rule_sha": rule_sha(rule), "cost_usd": 0.0,
        "phrases": len(phrases),
        "signals": {"window_days": cfg.SIGNAL_WINDOW_DAYS, "read": len(sigs), "usable": len(usable),
                    "matched": len(matched),
                    "explained_edits": sum(1 for s in matched if s.get("actor_type") in ("admin", "tenant")),
                    "system": sum(1 for s in matched if s.get("actor_type") == "system")},
        "recurrence": {"window_days": RECURRENCE_DAYS, "basis": basis, "k": k, "have": have},
        "r2_eligible": eligible,
        "r2_reason": None if eligible else "the cluster recurred under the mining threshold in the last 30 days",
        "pieces": totals,
        "fire_piece_ids": fire_pieces,
        "prevented": {"by_gate": dict(prevented), "blocking": prevented_blocking,
                      "total": sum(prevented.values()), "piece_ids": prevented_pieces},
        "phrase": {"before": before, "after": after},
        "new_hard_stops": dict(hard), "new_hard_stop_piece_ids": hard_pieces,
        "baseline": {"pieces_compared": pieces_compared, "pieces_agree": pieces_agree,
                     "gate_checks": compared, "gate_agree": agree,
                     "agreement": round(agree / compared, 4) if compared else None,
                     "by_gate": {g: {"compared": v[0], "agree": v[1], "stored_fail": v[2], "replay_fail": v[3]}
                                 for g, v in sorted(baseline.items())}},
        "l0_applicable": entry is not None,
        "l0_ok": ok, "l0_reason": why,
    }


async def run_baseline(pool, *, writers: Iterable[str] = l0.L0_WRITERS) -> dict:
    """The sanity check alone (no rule): per T9 writer, the replay of every stored text with a ledger against that
    ledger. Same code path as a rule's baseline (a rule-less R0)."""
    out, cache = {}, {}
    for w in writers:
        fake = {"rule_id": None, "kind": "writer_instruction", "writer": w, "scope": "platform", "tenant_id": None,
                "rule_text": "", "match_phrases": [], "applies_to": [], "lint": {}}
        res = await run_r0(pool, fake, a1_pieces_30d=0, cache=cache)
        out[w] = {"pieces": res["pieces"], "baseline": res["baseline"]}
    return out


__all__ = ["R0_MAX_PIECES", "R0_VERSION", "R0_YIELD_SECONDS", "RECURRENCE_DAYS", "PieceReplay", "baseline_compare",
           "blocking_failed", "counterfactual", "l0_verdict", "load_evidence", "matches_text", "phrase_count",
           "phrases_for", "replay_piece", "rule_sha", "run_baseline", "run_r0", "signal_matches", "stored_failures",
           "texts_of"]
