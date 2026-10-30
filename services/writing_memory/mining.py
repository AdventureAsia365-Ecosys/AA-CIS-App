"""AA-917 WM-12 — the writing_memory_mine run: cluster pass signals ($0), keep the clusters over threshold, draft one
rule per cluster on the judge route, lint it, store it as a candidate with its provenance (app ADR 0010, D9, E4).

Clusters (counted in code over the fetched pass signals, deterministic, unit-tested):
  label      what the signals share: the admin's reason code (`reason:<code>`), else the gate's taxonomy class
             (`class:<qtax class>`, survives a gate rename, AA-787; an A1 code maps through a1_codes), else the gate;
             and, for human edits, every 1-3 word phrase the edit removed (`phrase:<masked phrase>`).
  tenant     human (admin / tenant) signals of ONE tenant: >= 2 signals on >= 2 pieces of that tenant.
  platform   from tenant evidence: human tenant-scope signals of >= 3 distinct tenants (D9) — the drafter sees masked
             text only, and the rule is linted against every tenant's text (5-gram overlap, brand names);
             from platform evidence (A1, the AA master's own pieces): human signals on >= 2 pieces by >= 2 distinct
             reviewers, or A1 system gate failures on >= 5% of the A1 pieces of the last 30 days (D9: A1 only).
  Tenant system signals (T9 attempts the job rewrote) are evidence for R0 (AA-918), not mined (D9 counts edits).
A cluster with an open rule is skipped; one smaller than 3 signals cannot pass PREDICT (1 shown + 2 held out), so it
waits without a paid call; one whose last draft the lint rejected waits until new signals join it. At most 50 drafter
calls per run, under the run ($1, spend_budget) and month ($25) caps. Every mine job that spent money counts as a run
(a failed one too), and automatic runs are off unless WRITING_MEMORY_AUTO_MINE is set (config.auto_mine_enabled).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import asyncpg
import structlog

from services.quality import registry as qr
from services.writing_memory import config as cfg
from services.writing_memory import lint as lint_mod
from services.writing_memory import rules as rules_mod
from services.writing_memory.masking import excerpt, mask, removed_phrases
from services.writing_memory.reason_codes import REASON_CODES

logger = structlog.get_logger()

HUMAN = ("admin", "tenant")
MAX_PHRASES_PER_SIGNAL = 20
MAX_PHRASE_CLUSTERS_PER_OWNER = 10

_REASON_MEANING = {
    "fact_not_in_source": "a fact the source does not give",
    "optional_as_included": "an optional item written as included",
    "wrong_place_or_time": "a wrong place or time",
    "voice_off_brand": "wording off the brand's voice",
    "ai_phrasing": "generic AI phrasing",
    "format_channel": "a format or length wrong for the channel",
    "repair_residue": "leftovers of a repair step",
    "style_only": "a style change",
}


@dataclass
class Cluster:
    writer: str
    scope: str
    tenant_id: Optional[str]
    label: str
    basis: str                              # tenant | platform_tenants | platform_reviewers | a1_gate_repeat
    signals: list[dict] = field(default_factory=list)

    @property
    def cluster_key(self) -> str:
        return f"{self.writer}|{self.label}"[:200]

    def support(self) -> dict:
        return {"signals": len(self.signals),
                "pieces": len({s["source_id"] for s in self.signals}),
                "tenants": len({s["tenant_id"] for s in self.signals if s["tenant_id"]})
                if self.scope == "platform" else 0,
                "reviewers": len({s["actor_id"] for s in self.signals
                                  if s["actor_type"] == "admin" and s["actor_id"] not in (None, "unknown")})}

    def channels(self) -> list[str]:
        return sorted({s["channel"] for s in self.signals if s.get("channel")})[:8]


def gate_class(gate: Optional[str]) -> Optional[str]:
    """The first qtax class of a gate, or of an A1 code; None when neither is registered."""
    if not gate:
        return None
    classes = qr.gate_classes(gate)
    if not classes:
        entry = qr.a1_codes().get(gate) or {}
        classes = list(entry.get("classes") or ([entry["class"]] if entry.get("class") not in (None, "none") else []))
    return classes[0] if classes else None


def labels_of(sig: dict) -> list[str]:
    """The labels one signal belongs to (see the module docstring)."""
    out = []
    if sig.get("reason_code") in REASON_CODES:
        out.append(f"reason:{sig['reason_code']}")
    elif sig.get("gate"):
        cls = gate_class(sig["gate"])
        out.append(f"class:{cls}" if cls else f"gate:{sig['gate']}")
    if sig.get("actor_type") in HUMAN and sig.get("before_text") and sig.get("after_text") is not None:
        phrases = sorted(removed_phrases(sig["before_text"], sig["after_text"]), key=lambda p: (-len(p), p))
        out += [f"phrase:{p}" for p in phrases[:MAX_PHRASES_PER_SIGNAL]]
    return out


def label_meaning(label: str) -> str:
    kind, _, value = label.partition(":")
    if kind == "reason":
        return _REASON_MEANING.get(value, value)
    if kind == "class":
        info = qr.class_info(value) or {}
        return str(info.get("label") or info.get("name") or value)
    if kind == "gate":
        return qr.gate_label(value) or value
    return f"the phrase '{value}' was removed"


def _enough(sigs: list[dict]) -> bool:
    return len(sigs) >= cfg.MIN_SUPPORT and len({s["source_id"] for s in sigs}) >= cfg.MIN_SUPPORT


BASES = ("tenant", "platform_tenants", "platform_reviewers", "a1_gate_repeat")


def cluster_threshold(basis: Optional[str], *, a1_pieces_30d: int = 0) -> dict:
    """The support a cluster of this basis needs, from the config (D9): {signals, pieces, tenants, reviewers}. One
    function for the miner (build_clusters) and the R0 candidate filter (AA-918: "k equals the mining threshold", so a
    config change moves both). An unknown basis (an admin-made rule) needs MIN_SUPPORT signals on MIN_SUPPORT
    pieces."""
    th = {"signals": cfg.MIN_SUPPORT, "pieces": cfg.MIN_SUPPORT, "tenants": 0, "reviewers": 0}
    if basis == "tenant":
        th.update(signals=max(cfg.MIN_SUPPORT, cfg.TENANT_MIN_SIGNALS), pieces=max(cfg.MIN_SUPPORT,
                                                                                    cfg.TENANT_MIN_PIECES))
    elif basis == "platform_tenants":
        th["tenants"] = cfg.PLATFORM_MIN_TENANTS
    elif basis == "platform_reviewers":
        th["reviewers"] = cfg.PLATFORM_MIN_REVIEWERS
    elif basis == "a1_gate_repeat":
        th["pieces"] = max(cfg.MIN_SUPPORT, math.ceil(cfg.A1_GATE_REPEAT_SHARE * max(a1_pieces_30d, 0)))
    return th


def support_of(sigs: list[dict]) -> dict:
    """{signals, pieces, tenants, reviewers} of a signal list (reviewers: distinct known admin ids)."""
    return {"signals": len(sigs), "pieces": len({s["source_id"] for s in sigs}),
            "tenants": len({s["tenant_id"] for s in sigs if s.get("tenant_id")}),
            "reviewers": len({s["actor_id"] for s in sigs
                              if s.get("actor_type") == "admin" and s.get("actor_id") not in (None, "unknown")})}


def meets(sigs: list[dict], threshold: dict) -> bool:
    have = support_of(sigs)
    return all(have[k] >= int(threshold.get(k) or 0) for k in ("signals", "pieces", "tenants", "reviewers"))


def _dedupe_phrases(clusters: list[Cluster]) -> list[Cluster]:
    """Phrase clusters of one owner with the same signal set: keep the longest phrase; then the top N by support."""
    keep, by_owner = [], defaultdict(list)
    for c in clusters:
        if c.label.startswith("phrase:"):
            by_owner[(c.writer, c.scope, c.tenant_id, c.basis)].append(c)
        else:
            keep.append(c)
    for group in by_owner.values():
        best: dict[frozenset, Cluster] = {}
        for c in group:
            ids = frozenset(s["signal_id"] for s in c.signals)
            if ids not in best or len(c.label) > len(best[ids].label):
                best[ids] = c
        ranked = sorted(best.values(), key=lambda c: (-len(c.signals), -len(c.label), c.label))
        keep += ranked[:MAX_PHRASE_CLUSTERS_PER_OWNER]
    return keep


def build_clusters(signals: Iterable[dict], *, a1_pieces_30d: int, now: Optional[datetime] = None) -> list[Cluster]:
    """Every cluster over threshold, platform first, then by support (deterministic order)."""
    now = now or datetime.now(timezone.utc)
    tenant_groups: dict[tuple, list[dict]] = defaultdict(list)
    cross_groups: dict[tuple, list[dict]] = defaultdict(list)
    platform_human: dict[tuple, list[dict]] = defaultdict(list)
    a1_system: dict[tuple, list[dict]] = defaultdict(list)
    for sig in signals:
        if sig.get("fact_status") != "pass":
            continue                       # fail is quarantined, grey waits for an admin, pending is not filtered yet
        human = sig.get("actor_type") in HUMAN
        for label in labels_of(sig):
            if sig["scope"] == "tenant":
                if human:
                    tenant_groups[(sig["writer"], sig["tenant_id"], label)].append(sig)
                    cross_groups[(sig["writer"], label)].append(sig)
            elif human:
                platform_human[(sig["writer"], label)].append(sig)
            elif sig["writer"] == "a1" and not label.startswith("phrase:"):
                a1_system[(sig["writer"], label)].append(sig)

    out: list[Cluster] = []
    # AA-918 — every basis reads its threshold from cluster_threshold (the R0 candidate filter reads the same one)
    for (writer, tenant, label), sigs in tenant_groups.items():
        if meets(sigs, cluster_threshold("tenant")):
            out.append(Cluster(writer, "tenant", tenant, label, "tenant", sigs))
    for (writer, label), sigs in cross_groups.items():
        if meets(sigs, cluster_threshold("platform_tenants")):
            out.append(Cluster(writer, "platform", None, label, "platform_tenants", sigs))
    for (writer, label), sigs in platform_human.items():
        if meets(sigs, cluster_threshold("platform_reviewers")):
            out.append(Cluster(writer, "platform", None, label, "platform_reviewers", sigs))
    since = now - timedelta(days=cfg.A1_WINDOW_DAYS)
    a1_need = cluster_threshold("a1_gate_repeat", a1_pieces_30d=a1_pieces_30d)
    for (writer, label), sigs in a1_system.items():
        recent = [s for s in sigs if s["created_at"] is None or s["created_at"] >= since]
        if meets(recent, a1_need):
            out.append(Cluster(writer, "platform", None, label, "a1_gate_repeat", recent))

    # one cluster per (writer, scope, tenant, label): a label can qualify on two platform bases; keep the first
    seen, unique = set(), []
    for c in _dedupe_phrases(out):
        k = (c.writer, c.scope, c.tenant_id, c.cluster_key)
        if k not in seen:
            seen.add(k)
            unique.append(c)
    unique.sort(key=lambda c: (c.scope != "platform", -len(c.signals), c.cluster_key, c.tenant_id or ""))
    for c in unique:
        c.signals.sort(key=lambda s: s["signal_id"])
    return unique


def split_examples(cluster: Cluster) -> tuple[list[dict], list[dict]]:
    """(shown to the drafter, held out for PREDICT): at most DRAFT_MAX_EXAMPLES shown, always >= 2 held out when the
    cluster has >= 3 signals. Deterministic (signals are sorted by id)."""
    n = len(cluster.signals)
    shown = max(1, min(cfg.DRAFT_MAX_EXAMPLES, n - cfg.PREDICT_MIN_EXPLAINED))
    return cluster.signals[:shown], cluster.signals[shown:]


def example_of(sig: dict) -> dict:
    """A signal as the drafter sees it: masked excerpts and the masked gate diagnostic."""
    if sig.get("after_text") is None:
        before = mask(sig.get("before_text") or "")[: cfg.EXCERPT_CHARS]
        after = None
    else:
        before, after = excerpt(sig.get("before_text"), sig.get("after_text"))
    problem = mask(sig.get("problem") or "")[:300] or None
    return {"before": before, "after": after, "problem": problem}


# ── the run ─────────────────────────────────────────────────────────────────────────────────────────────────────────

_SIGNALS_SQL = """
    SELECT signal_id::text AS signal_id, writer, channel, scope, tenant_id::text AS tenant_id, field, before_text,
           after_text, gate, problem, reason_code, action, actor_type, actor_id, fact_status, source_id, created_at
      FROM shared.writing_signal
     WHERE fact_status = 'pass' AND created_at >= now() - make_interval(days => $1)
     ORDER BY signal_id
"""
_A1_PIECES_SQL = """
    SELECT count(*) FROM silver_aa_internal.generated_content WHERE created_at >= now() - make_interval(days => $1)
"""
# Tenant text for the 5-gram overlap lint (masked n-grams): the signals' texts and the tenants' pieces / versions.
_CORPUS_SQL = """
    SELECT tenant_id::text AS tenant_id, coalesce(before_text, '') || E'\\n' || coalesce(after_text, '') AS t
      FROM shared.writing_signal WHERE scope = 'tenant'
    UNION ALL
    SELECT tenant_id::text, content_text FROM acp_shared.content_piece
     WHERE tenant_id <> '00000000-0000-0000-0000-000000000001'::uuid
"""
_NAMES_SQL = """
    SELECT t.tenant_id::text AS tenant_id, t.name AS name FROM shared.tenants t
     WHERE t.tenant_id <> '00000000-0000-0000-0000-000000000001'::uuid
    UNION ALL
    SELECT b.tenant_id::text, b.brand_name FROM shared.tenant_brand_rules b
     WHERE b.is_active AND b.tenant_id <> '00000000-0000-0000-0000-000000000001'::uuid
"""
_FORBIDDEN_SQL = """
    SELECT tenant_id::text AS tenant_id, forbidden_words FROM shared.tenant_brand_rules
     WHERE is_active AND tenant_id <> '00000000-0000-0000-0000-000000000001'::uuid
"""


async def a1_piece_count(conn) -> int:
    """A1 pieces (generated_content rows) of the last 30 days: the denominator of the 5% gate-repeat rule."""
    try:
        async with conn.transaction():
            return int(await conn.fetchval(_A1_PIECES_SQL, cfg.A1_WINDOW_DAYS) or 0)
    except Exception:  # noqa: BLE001 — no generated_content in this DB: the rule uses its floor
        return 0


async def _corpora(conn) -> tuple[dict[str, set], dict[str, list[str]], list[str]]:
    """(tenant -> masked 5-grams of its text, tenant -> brand names and forbidden words, the parts that failed to
    load). A part that fails is logged and returned in the third item: `corpus_gap` then fails the overlap lint closed
    for the rules that needed it."""
    grams: dict[str, set] = defaultdict(set)
    names: dict[str, list[str]] = defaultdict(list)
    missing: list[str] = []
    for sql, kind in ((_CORPUS_SQL, "corpus"), (_NAMES_SQL, "names"), (_FORBIDDEN_SQL, "forbidden")):
        try:
            async with conn.transaction():                  # a savepoint when the caller holds a transaction
                rows = await conn.fetch(sql)
        except Exception as e:  # noqa: BLE001 — recorded as missing: the lint then fails closed (corpus_gap)
            logger.warning("aa917_mine_corpus_part_missing", part=kind, error=f"{type(e).__name__}: {str(e)[:200]}")
            missing.append(kind)
            continue
        for r in rows:
            if kind == "corpus":
                grams[r["tenant_id"]] |= {hash(g) for g in lint_mod.ngrams(mask(r["t"] or ""))}   # ints: small
            elif kind == "names":
                if r["name"]:
                    names[r["tenant_id"]].append(r["name"])
            else:
                fw = r["forbidden_words"]
                if isinstance(fw, str):
                    try:
                        fw = json.loads(fw)
                    except ValueError:
                        fw = [fw]
                names[r["tenant_id"]] += [str(w) for w in (fw or []) if w]
    return grams, names, missing


def corpus_gap(scope: str, missing: Iterable[str]) -> Optional[str]:
    """The corpus parts a rule of this scope needed but could not get (None = complete). A platform rule needs every
    part (tenant text, names, forbidden words); a tenant rule needs the names and forbidden words of the others."""
    need = ("corpus", "names", "forbidden") if scope == "platform" else ("names", "forbidden")
    gap = [m for m in missing if m in need]
    return ", ".join(gap) or None


def lint_context(cluster: Cluster, all_grams: set, names: dict[str, list[str]]) -> tuple[set, list[str]]:
    """What the overlap lint checks a cluster's rule against: a platform rule — every tenant's text and names; a tenant
    rule — no corpus (its own text is its evidence), the OTHER tenants' names (a leak across tenants)."""
    if cluster.scope == "platform":
        return all_grams, [n for v in names.values() for n in v]
    return set(), [n for t, v in names.items() if t != cluster.tenant_id for n in v]


# A run is every mine job that is queued / running (another one is on its way), succeeded with ran = true, or ended
# failed / stopped / cancelled after it spent money or made a call (S250 review: a run that crashed after paid calls
# must count, or the next daily fact filter starts the same paid run again). $1 = the asking job, never counted.
_RUNS_SQL = """
    SELECT count(*) FILTER (WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc')
               AS this_month,
           count(*) FILTER (WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
                              AND coalesce(result ->> 'trigger', payload ->> 'trigger') = 'auto') AS auto_this_month,
           max(created_at) AS last_run_at
      FROM shared.job
     WHERE kind = 'writing_memory_mine' AND ($1::uuid IS NULL OR id <> $1::uuid)
       AND coalesce(payload ->> 'mode', '') <> 'preference_summary'
       AND (status IN ('queued', 'running')
            OR (status = 'succeeded' AND (result ->> 'ran')::boolean IS TRUE)
            OR (status IN ('failed', 'stopped_budget', 'cancelled')
                AND (cost_usd > 0 OR (result ->> 'ran')::boolean IS TRUE
                     OR coalesce(progress ->> 'calls', '') ~ '^[1-9][0-9]*$')))
"""
_NEW_PASS_SQL = """
    SELECT count(*) FROM shared.writing_signal
     WHERE fact_status = 'pass' AND ($1::timestamptz IS NULL OR created_at > $1::timestamptz)
"""
_OPEN_WAVE_SQL = "SELECT count(*) FROM shared.rerun_wave WHERE status = 'open'"
TRIGGERS = ("auto", "manual")


def decide_run(trigger: str, *, runs_this_month: int, auto_runs_this_month: int, new_pass: int,
               open_waves: int, auto_enabled: bool = True) -> Optional[str]:
    """Why a run must not happen (None = it may). Pure (cost-cuts #6): an automatic run needs the env switch
    (`auto_enabled`, default off in run_gate), >= 10 new pass signals since the last run, at most 2 automatic runs a
    month and no open rerun wave (C5.4d: paid background work stays off a wave); a manual run (the admin, e.g. before
    the monthly release close) is the 3rd run of the month at most."""
    if trigger not in TRIGGERS:
        return f"unknown trigger {trigger!r}"
    if trigger == "auto" and not auto_enabled:
        return f"automatic mining is off ({cfg.AUTO_MINE_ENV} is not set)"
    if runs_this_month >= cfg.MINE_MAX_RUNS_PER_MONTH:
        return f"{runs_this_month} runs this month already (max {cfg.MINE_MAX_RUNS_PER_MONTH})"
    if trigger == "auto":
        if open_waves:
            return f"{open_waves} rerun wave(s) open"
        if auto_runs_this_month >= cfg.MINE_MAX_AUTO_RUNS_PER_MONTH:
            return f"{auto_runs_this_month} automatic runs this month already (max {cfg.MINE_MAX_AUTO_RUNS_PER_MONTH})"
        if new_pass < cfg.MINE_MIN_NEW_PASS:
            return f"{new_pass} new pass signals since the last run (needs {cfg.MINE_MIN_NEW_PASS})"
    return None


async def run_gate(conn, trigger: str, *, exclude_job_id: Optional[str] = None) -> tuple[Optional[str], dict]:
    """decide_run over the live counts; (refusal or None, the counts). `exclude_job_id`: the mine job asking (its own
    row is running and must not count against itself)."""
    runs = await conn.fetchrow(_RUNS_SQL, exclude_job_id)
    new_pass = int(await conn.fetchval(_NEW_PASS_SQL, runs["last_run_at"]) or 0)
    try:
        async with conn.transaction():
            waves = int(await conn.fetchval(_OPEN_WAVE_SQL) or 0)
    except Exception:  # noqa: BLE001 — no rerun_wave table in this DB: no wave
        waves = 0
    facts = {"runs_this_month": int(runs["this_month"] or 0), "auto_runs_this_month": int(runs["auto_this_month"] or 0),
             "new_pass": new_pass, "open_waves": waves, "auto_enabled": cfg.auto_mine_enabled(),
             "last_run_at": runs["last_run_at"].isoformat() if runs["last_run_at"] else None}
    return decide_run(trigger, **{k: facts[k] for k in ("runs_this_month", "auto_runs_this_month", "new_pass",
                                                       "open_waves", "auto_enabled")}), facts


async def run_mine(pool, ctx, *, trigger: str, max_calls: int = cfg.MINE_MAX_CALLS) -> dict:
    """One mining run (the writing_memory_mine job handler's body). `ctx` is the JobContext (progress, cost) or None."""
    from shared.cost_guard import load_run_budget
    from services.writing_memory.budget import load_month_budget
    from services.writing_memory.drafter import build_user_prompt, draft_rule

    async with pool.acquire() as conn:
        expired = await rules_mod.expire_stale(conn)
        sigs = [dict(r) for r in await conn.fetch(_SIGNALS_SQL, cfg.SIGNAL_WINDOW_DAYS)]
        a1_pieces = await a1_piece_count(conn)
        open_keys = await rules_mod.open_cluster_keys(conn)
        rejected_sets = await rules_mod.lint_rejected_signal_sets(conn)
        month = await load_month_budget(conn)
        grams, names, missing = await _corpora(conn)
    run_budget = await load_run_budget(pool, "bedrock", "writing_memory_mine", day_scope="kind")

    all_grams: set = set().union(*grams.values()) if grams else set()
    clusters = build_clusters(sigs, a1_pieces_30d=a1_pieces)
    result: dict = {"trigger": trigger, "signals": len(sigs), "clusters": len(clusters), "expired": expired,
                    "skipped_open": 0, "skipped_lint_same": 0, "waiting_small": 0, "drafted": 0, "candidates": 0,
                    "lint_rejected": 0, "redacted": 0, "store_refused": 0, "none": 0, "unusable": 0, "calls": 0,
                    "cost_usd": 0.0, "stopped": None, "corpus_missing": missing, "rules": []}
    for c in clusters:
        key = (c.writer, c.scope, c.tenant_id, c.cluster_key)
        if key in open_keys:
            result["skipped_open"] += 1
            continue
        if rejected_sets.get(key) == frozenset(s["signal_id"] for s in c.signals[:rules_mod.MAX_SIGNAL_IDS]):
            result["skipped_lint_same"] += 1    # same evidence as the draft the lint rejected: no new paid call
            continue
        if len(c.signals) < cfg.MIN_CLUSTER_FOR_DRAFT:
            result["waiting_small"] += 1
            continue
        if result["calls"] >= max_calls:
            result["stopped"] = "max_calls"
            break
        try:
            month.check(cfg.MINE_CALL_ESTIMATE_USD)
            run_budget.check(cfg.MINE_CALL_ESTIMATE_USD)
        except Exception as e:  # noqa: BLE001 — BudgetExceeded: stop cleanly, keep what was drafted
            result["stopped"] = f"budget: {e}"
            break
        shown, held = split_examples(c)
        prompt = build_user_prompt(writer=c.writer, channels=c.channels(), label=c.label,
                                   label_meaning=label_meaning(c.label), scope=c.scope,
                                   examples=[example_of(s) for s in shown])
        result["calls"] += 1
        try:
            draft = await draft_rule(pool, user_prompt=prompt, cluster_key=c.cluster_key, tenant_id=c.tenant_id)
        except Exception as e:  # noqa: BLE001 — one bad reply skips one cluster (its row is already logged)
            result["unusable"] += 1
            logger.warning("aa917_mine_draft_failed", cluster_key=c.cluster_key, error=f"{type(e).__name__}: "
                                                                                       f"{str(e)[:200]}")
            continue
        month.charge(draft.cost_usd)
        run_budget.charge(draft.cost_usd)
        result["cost_usd"] = round(result["cost_usd"] + draft.cost_usd, 6)
        if ctx is not None:
            ctx.add_cost(draft.cost_usd)
            await ctx.progress(calls=result["calls"], cost_usd=result["cost_usd"])
        if draft.is_none:
            result["none"] += 1
            continue
        result["drafted"] += 1
        corpus, banned = lint_context(c, all_grams, names)
        lint = lint_mod.run_lint(kind=draft.kind, rule_text=draft.rule_text, replacement=draft.replacement,
                                 match_phrases=draft.match_phrases, scope=c.scope,
                                 held_out=[{"before_text": s.get("before_text"), "after_text": s.get("after_text")}
                                           for s in held], corpus_ngrams=corpus, names=banned,
                                 corpus_unavailable=corpus_gap(c.scope, missing))
        lint["basis"] = c.basis
        reason = c.label.partition(":")[2] if c.label.startswith("reason:") else None
        gcls = c.label.partition(":")[2] if c.label.startswith("class:") else None
        try:
            async with pool.acquire() as conn:
                rid = await rules_mod.insert_candidate(
                    conn, kind=draft.kind, scope=c.scope, tenant_id=c.tenant_id, writer=c.writer,
                    cluster_key=c.cluster_key, rule_text=draft.rule_text, replacement=draft.replacement,
                    match_phrases=draft.match_phrases, applies_to=c.channels(), reason_code=reason, gate_class=gcls,
                    signal_ids=[s["signal_id"] for s in c.signals], support=c.support(), lint=lint,
                    drafted_by=draft.model, mine_job_id=getattr(ctx, "job_id", None))
        except (asyncpg.CheckViolationError, asyncpg.StringDataRightTruncationError) as e:
            # fit_to_store keeps every draft inside CIS 280; a store rule it missed costs this one cluster, never the
            # paid run (the drafts before and after it are kept)
            result["store_refused"] += 1
            logger.warning("aa917_mine_store_refused", cluster_key=c.cluster_key,
                           error=f"{type(e).__name__}: {str(e)[:200]}")
            continue
        if rid:
            result["candidates" if lint["ok"] else "lint_rejected"] += 1
            if (lint.get("five_gram_overlap") or {}).get("leak"):
                result["redacted"] += 1
            result["rules"].append({"rule_id": rid, "cluster_key": c.cluster_key, "scope": c.scope, "ok": lint["ok"]})
    result["month"] = month.summary()
    result["run_budget"] = run_budget.summary()
    return result


__all__ = ["BASES", "Cluster", "TRIGGERS", "a1_piece_count", "build_clusters", "cluster_threshold", "corpus_gap",
           "decide_run", "example_of", "gate_class", "label_meaning", "labels_of", "lint_context", "meets",
           "run_gate", "run_mine", "split_examples", "support_of"]
