"""AA-1154 (A0) — the rule-based playbooks of the CIS admin assistant: evidence in, ranked action cards out. Pure.

``signals(snapshot, limits)`` lists what the assistant sees beyond the status page's Needs-attention list (the page
does not show an AI connection refusal yet): an AI connection refusing at account level (its circuit refusing, or at
least ``refused_trigger_calls`` account-level refusals in the window), a throttled connection (its circuit open on
throttling, or at least ``throttled_trigger_calls`` throttled calls), a spend spike (spend per hour above
``spend_spike_floor_usd_h`` and at least ``spend_spike_factor`` x its hourly median of ``spend_baseline_days`` days).

``cards(snapshot, limits)`` picks the playbook of the item (``choose``) and returns its cards in rank order:

- ``llm_access_denied`` (the 29/10 lesson first: check the account level before model by model), ``llm_throttled``,
  ``llm_cost_spike``, ``queue_stuck``, ``server_errors``, and ``generic`` for every other item;
- every playbook ends with, or contains, the "do nothing and watch" card, so each card's "if we do nothing" compares
  with it; a card whose precondition does not hold now ranks after those whose does; copy-only (R4) cards rank last;
- a card without an evidence link is dropped (S285 advisory section 4);
- each card: reason, evidence ids (resolved to links by the caller), risk (``risk.assess``), predictions each with a
  sourced confidence (``code`` / ``measured`` / ``history`` / ``not_enough_data``; never a percentage), "if we do
  nothing", way back, cost, who approves (the capability of the API it calls; R3 needs a written reason of at least
  ``reason_min_chars``), the call it would make (a DRY RUN or preview first where one exists), what A2 checks after,
  and when it expires (``card_ttl_min``). No card is ever executed here (tech lead AI5).

``not_proposed`` lists the actions a playbook leaves out on purpose, with the reason (the 29/10 example: switching the
refused connection off by hand, resetting the anomaly breaker, retrying every failed job).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from api.services.ops_assist import catalog, risk
from api.services.ops_assist.outcome import m
from api.services.ops_assist.evidence import ACCESS_CLASSES, baseline_label, stages_by_connection
from shared.llm_client.route_evidence import BREAK_GLASS_MIN_CHARS

SEV_ACCESS = "sev2"
SEV_THROTTLE = "sev3"
SEV_SPEND = "sev3"


# App names as the status page shows them (ops_status.sections.APP_LABELS; a card never shows the lower-case key).
APP_LABELS = {"cis": "CIS", "tripplanner": "TripPlanner", "booking": "Booking", "crm": "Partner CRM",
              "shared": "Shared"}


def app_label(app: Optional[str]) -> str:
    return APP_LABELS.get(str(app or ""), str(app or ""))


def hhmm(t: Optional[str]) -> str:
    """"09:58 UTC" for an ISO time, never the raw ISO string in a card. Pure."""
    try:
        return datetime.fromisoformat(str(t)).astimezone(timezone.utc).strftime("%H:%M UTC")
    except (TypeError, ValueError):
        return str(t or "?")


# ── signals ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _conn_label(s: dict, key: str) -> str:
    for c in s.get("connections") or []:
        if c["connection_key"] == key:
            return c.get("label") or key
    return key


def new_refusals(conn: dict, calls: dict) -> int:
    """Account-level refusals in the window on models that are not a known refusal of the connection (a model the
    account has no grant for, AA-900: acc3's refused GPT models) — those never open a signal. Pure."""
    known = set(conn.get("known_refused_models") or [])
    by_model = calls.get("refused_by_model") or {}
    if by_model:
        return sum(int(v) for m, v in by_model.items() if m not in known)
    return int(calls.get("access_refused") or 0)


def signals(s: dict, limits: dict) -> list[dict]:
    """The assistant's own signals of a snapshot (ids ``llm-conn-<key>``, ``llm-throttle-<key>``,
    ``llm-spend-<key>``), most severe first. Pure."""
    out: list[dict] = []
    calls = s.get("calls") or {}
    trigger = int(limits["refused_trigger_calls"])
    throttle_trigger = int(limits["throttled_trigger_calls"])
    for c in s.get("connections") or []:
        key = c["connection_key"]
        circ = c.get("circuit") or {}
        n = calls.get(key) or {}
        refusing = bool(circ.get("refusing")) and circ.get("error_class") in ACCESS_CLASSES
        if refusing or new_refusals(c, n) >= trigger:
            out.append({"id": f"llm-conn-{key}", "severity": SEV_ACCESS, "app": "shared", "kind": "llm_access_denied",
                        "connection_key": key,
                        "title": f"AI connection {c.get('label') or key} refuses calls at account level",
                        "detail": (f"Circuit {circ.get('state')} ({circ.get('error_class')}); " if refusing else "")
                        + f"{new_refusals(c, n)} account-level refusals in {s['window']['minutes']} min"})
            continue
        throttled = int((n.get("classes") or {}).get("throttled") or 0)
        circuit_throttled = circ.get("state") not in (None, "closed") and circ.get("error_class") == "throttled"
        if circuit_throttled or throttled >= throttle_trigger:
            out.append({"id": f"llm-throttle-{key}", "severity": SEV_THROTTLE, "app": "shared",
                        "kind": "llm_throttled", "connection_key": key,
                        "title": f"AI connection {c.get('label') or key} is throttled",
                        "detail": f"{throttled} throttled calls in {s['window']['minutes']} min"})
    factor = float(limits["spend_spike_factor"])
    floor = float(limits["spend_spike_floor_usd_h"])
    for key, sp in sorted((s.get("spend") or {}).items()):
        base, now = float(sp.get("baseline_hour_usd") or 0.0), float(sp.get("per_hour_usd") or 0.0)
        if now > floor and now >= factor * base:
            out.append({"id": f"llm-spend-{key}", "severity": SEV_SPEND, "app": "shared", "kind": "llm_cost_spike",
                        "connection_key": key, "title": f"AI spend on {_conn_label(s, key)} is {now:g} USD/h",
                        "detail": f"{baseline_label(s).capitalize()} {base:g} USD/h; limit {factor:g} x"})
    out.sort(key=lambda x: x["severity"])
    return out


def _signal(s: dict, limits: dict, sid: str) -> Optional[dict]:
    for x in signals(s, limits):
        if x["id"] == sid:
            return x
    return None


def choose(s: dict, limits: dict) -> tuple[str, dict]:
    """(playbook, context) for the snapshot's item. Pure."""
    iid = s.get("item_id") or ""
    for prefix, book in (("llm-conn-", "llm_access_denied"), ("llm-throttle-", "llm_throttled"),
                         ("llm-spend-", "llm_cost_spike")):
        if iid.startswith(prefix):
            return book, {"connection_key": iid[len(prefix):], "signal": _signal(s, limits, iid)}
    if iid.startswith("ai-"):
        return "llm_cost_spike", {"app": iid[3:], "connection_key": _top_spender(s)}
    if iid.startswith("check-llm"):
        access = [x for x in signals(s, limits) if x["kind"] == "llm_access_denied"]
        if access:
            return "llm_access_denied", {"connection_key": access[0]["connection_key"], "signal": access[0]}
    if iid.startswith("queue-"):
        return "queue_stuck", {"queue": iid[len("queue-"):]}
    for prefix in ("errors-", "p95-", "health-", "svc-", "rollout-", "4xx-"):
        if iid.startswith(prefix):
            item = s.get("item") or {}
            return "server_errors", {"app": item.get("app") or iid[len(prefix):]}
    return "generic", {}


def _top_spender(s: dict) -> Optional[str]:
    spend = s.get("spend") or {}
    if not spend:
        return None
    return max(spend, key=lambda k: float(spend[k].get("per_hour_usd") or 0.0))


# ── card building ────────────────────────────────────────────────────────────────────────────────────────────────

def pred(text: str, source: str, basis: str) -> dict:
    """One predicted outcome with its sourced confidence: ``code`` (a fixed consequence of the API), ``measured`` (a
    DRY RUN, a preview or the ledger of the window), ``history`` (right k of n, only from ``history_min_n``) or
    ``not_enough_data``. Pure."""
    assert source in ("code", "measured", "history", "not_enough_data")
    return {"text": text, "confidence": source, "basis": basis}


def track_record(s: dict, action_id: str, limits: dict) -> dict:
    """The action's own record of predicted against actual outcomes (A2 writes it; none in A0). Pure."""
    h = (s.get("history") or {}).get(action_id) or {}
    n, hits = int(h.get("n") or 0), int(h.get("hits") or 0)
    need = int(limits["history_min_n"])
    if n >= need:
        return pred(f"This action's prediction was right {hits} of {n} times.", "history", f"{n} measured outcomes")
    return pred("No track record yet for this action.", "not_enough_data",
                f"{n} measured outcome(s); a record is shown from {need}")


def approval(action: catalog.Action, level: str, limits: dict) -> dict:
    """Who approves: the capability of the route the card calls (tech lead AI7: no new approval layer). Pure."""
    from api.core.staff_authz import READ_ONLY_ROLES, ROLE_PERMS

    if action.copy_only:
        return {"who": "The tech lead runs it from the runbook (MFA); the assistant never does.",
                "capability": [], "roles": [], "needs_reason": False, "reason_min_chars": None, "second_person": False}
    if not action.call:
        return {"who": "Nobody: nothing is changed.", "capability": [], "roles": [], "needs_reason": False,
                "reason_min_chars": None, "second_person": False}
    roles = sorted(r for r, perms in ROLE_PERMS.items()
                   if r not in READ_ONLY_ROLES and any(c in perms for c in action.capability))
    needs_reason = level == "R3"
    caps = " or ".join(action.capability)
    who = f"One person with {caps} ({', '.join(roles)}), under the rule of the API it calls. No second person."
    if needs_reason:
        who += f" A written reason of at least {int(limits['reason_min_chars'])} characters."
    return {"who": who, "capability": list(action.capability), "roles": roles, "needs_reason": needs_reason,
            "reason_min_chars": int(limits["reason_min_chars"]) if needs_reason else None,
            "second_person": False}


def _route(r: Optional[catalog.Route], **params: str) -> Optional[dict]:
    if r is None:
        return None
    path = r.path
    for k, v in params.items():
        path = path.replace("{" + k + "}", str(v))
    return {"method": r.method, "path": path}


def make_card(s: dict, limits: dict, action_id: str, *, reason: str, evidence: list[str], predictions: list[dict],
              if_nothing: str, cost_usd: Optional[float], cost_basis: str, body: Optional[dict] = None,
              params: Optional[dict] = None, stages: Optional[int] = None, change: Optional[dict] = None,
              precondition: Optional[str] = None, precondition_met: bool = True, title: Optional[str] = None,
              copy_text: Optional[str] = None, way_back_body: Optional[dict] = None,
              dry_run_body: Optional[dict] = None, measures: Optional[list[dict]] = None) -> dict:
    a = catalog.get(action_id)
    params = params or {}
    rk = risk.assess(a, limits, stages=stages, cost_usd=cost_usd, change=change)
    as_of = datetime.fromisoformat(s["as_of"])
    call = _route(a.call, **params)
    if call is not None and body is not None:
        call["body"] = body
    dry = _route(a.dry_run, **params)
    back = _route(a.way_back, **params)
    if back is not None and way_back_body is not None:
        back["body"] = way_back_body
    if dry is not None and dry_run_body is not None:
        dry["body"] = dry_run_body
    elif dry is not None and action_id in ("llm_probe", "retry_llm_denied"):
        dry["body"] = {**(body or {}), "dry_run": True}
    elif dry is not None and body is not None:
        dry["body"] = body
    return {
        "id": f"{s.get('item_id')}:{action_id}", "action": action_id, "title": title or a.title,
        "reason": reason, "evidence": evidence, "risk": rk,
        "predictions": predictions + [track_record(s, action_id, limits)],
        "if_nothing": if_nothing,
        "way_back": {"text": a.way_back_text, "call": back},
        "cost": {"usd": cost_usd, "basis": cost_basis},
        "approval": approval(a, rk["level"], limits),
        "call": call, "dry_run": dry, "copy_only": a.copy_only, "runbook": a.runbook, "copy_text": copy_text,
        "precondition": precondition, "precondition_met": precondition_met,
        "check_after": list(a.check_after), "measures": list(measures or []),
        "expires_at": (as_of + timedelta(minutes=int(limits["card_ttl_min"]))).isoformat(),
    }


def not_proposed(action_id: str, why: str) -> dict:
    a = catalog.get(action_id)
    return {"action": action_id, "title": a.title, "why": why}


def rank(cards: list[dict], evidence_ids: set[str]) -> list[dict]:
    """Drop a card without a known evidence link, then keep the playbook order with unmet preconditions after the met
    ones and copy-only (R4) cards last; number them. Pure."""
    kept = []
    for c in cards:
        c["evidence"] = [e for e in c["evidence"] if e in evidence_ids]
        if c["evidence"]:
            kept.append(c)
    ordered = sorted(enumerate(kept), key=lambda ic: (ic[1]["copy_only"], not ic[1]["precondition_met"], ic[0]))
    out = []
    for n, (_, c) in enumerate(ordered, start=1):
        out.append({**c, "rank": n})
    return out


# ── shared parts ─────────────────────────────────────────────────────────────────────────────────────────────────

def _models_of(s: dict, key: str) -> set[str]:
    """The models whose traffic ``key`` lost: those refused on it in the window, else those it is tried first for.
    Pure."""
    refused = set(((s.get("calls") or {}).get(key) or {}).get("refused_models") or [])
    if refused:
        return refused
    out = set()
    for p in (s.get("routes") or {}).get("provider_order") or []:
        deps = sorted(p.get("deployments") or [], key=lambda d: d.get("priority") or 0)
        if deps and deps[0].get("connection_key") == key:
            out.add(p["model_key"])
    return out


def _fallbacks(s: dict, key: str) -> list[str]:
    """The other connections that answered, in the window, the models ``key`` lost (where its traffic went). Pure."""
    models = _models_of(s, key)
    return sorted(k for k, c in (s.get("calls") or {}).items()
                  if k != key and int(c.get("ok") or 0) > 0 and models & set(c.get("models") or []))


def _paid_fallback_spend(s: dict, key: str) -> tuple[float, float, list[str]]:
    """(spend per hour now, its usual hourly median) summed over the fallback connections. Pure."""
    fb = _fallbacks(s, key)
    sp = s.get("spend") or {}
    now = sum(float((sp.get(k) or {}).get("per_hour_usd") or 0.0) for k in fb)
    base = sum(float((sp.get(k) or {}).get("baseline_hour_usd") or 0.0) for k in fb)
    return round(now, 4), round(base, 4), fb


def _observe(s: dict, limits: dict, *, reason: str, evidence: list[str], if_nothing: str, watch: str = "",
             predictions: Optional[list[dict]] = None, measures: Optional[list[dict]] = None) -> dict:
    return make_card(s, limits, "observe", reason=reason, evidence=evidence,
                     predictions=predictions or [pred(watch, "code", "nothing is changed")],
                     if_nothing=if_nothing, cost_usd=0.0, cost_basis="Nothing is called.", measures=measures)


def _item_closes(s: dict) -> list[dict]:
    """The measure "the item is no longer open" (A2 checks it again). Pure."""
    return [m("item_open", "zero", "The item is no longer open", item_id=s.get("item_id"))]


def _open_wave(s: dict) -> Optional[dict]:
    for w in s.get("waves") or []:
        if not w.get("paused"):
            return w
    return None


def _wave_card(s: dict, limits: dict, extra_evidence: list[str], spend_now: float,
               spend_on: Optional[list[str]] = None) -> Optional[dict]:
    w = _open_wave(s)
    if w is None:
        return None
    checks = [m("wave_paused", "nonzero", "The wave is paused", wave_id=w["id"])]
    if spend_on:
        checks.append(m("spend_per_hour", "down", f"Spend per hour on {', '.join(spend_on)} falls",
                        connections=sorted(spend_on)))
    return make_card(
        s, limits, "wave_pause", params={"wave_id": w["id"]}, body={"reason": "ops assistant: paused while the "
                                                                    "incident is open"},
        title=f"Pause the open wave {w['name']}",
        reason="New bulk jobs of the wave would run through the fallback and spend without benefit while the incident "
               "is open.",
        evidence=[f"wave:{w['id']}", *extra_evidence],
        predictions=[pred("No new bulk job of the wave starts; jobs already running finish.", "code",
                          "POST /admin/waves/{id}/pause: bulk jobs wait in the queue"),
                     pred(f"Spend per hour falls towards the median ({spend_now:g} USD/h now).", "not_enough_data",
                          "no measured pause outcome yet")],
        if_nothing=f"The wave keeps queuing jobs at about {spend_now:g} USD/h on the fallback.",
        cost_usd=0.0, cost_basis="A pause calls nothing.", measures=checks,
        precondition="The wave is still open and not paused when the button is pressed (the API answers 409 if "
                     "not).")


# ── playbooks ────────────────────────────────────────────────────────────────────────────────────────────────────

def _provider_order_moving_last(s: dict, key: str) -> dict[str, list[str]]:
    """{model: connections} with ``key`` moved behind the others, for every model tried on ``key`` first. Pure."""
    out = {}
    for p in (s.get("routes") or {}).get("provider_order") or []:
        conns = [d["connection_key"] for d in sorted(p.get("deployments") or [], key=lambda d: d.get("priority") or 0)]
        enabled = [d["connection_key"] for d in sorted(p.get("deployments") or [], key=lambda d: d.get("priority") or 0)
                   if d.get("enabled") and d.get("connection_enabled", True)]
        if enabled and enabled[0] == key and len(conns) > 1:
            out[p["model_key"]] = [c for c in conns if c != key] + [key]
    return out


def llm_access_denied(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    key = ctx["connection_key"]
    label = _conn_label(s, key)
    calls = (s.get("calls") or {}).get(key) or {}
    conn = next((c for c in s.get("connections") or [] if c["connection_key"] == key), {})
    refusing = bool((conn.get("circuit") or {}).get("refusing"))
    st = stages_by_connection(s.get("routes") or {}).get(key) or {"stages": [], "primary": []}
    now_h, base_h, fb = _paid_fallback_spend(s, key)
    probe = s.get("probe") or {}
    last = probe.get("last") or {}
    measured_cost = last.get("estimated_usd") if last.get("dry_run") else None
    window = s["window"]["minutes"]
    cards: list[dict] = []

    probe_preds = [pred(f"Within about a minute the probe shows whether every model on {label} is refused (account "
                        "level) or only some (model level), and the same for every other connection.", "code",
                        "llm_connection_probe: one tiny call per enabled deployment; a refused call costs 0 USD")]
    if measured_cost is not None:
        probe_preds.append(pred(f"A live run costs about {measured_cost:g} USD.", "measured",
                                f"DRY RUN estimate of {last.get('finished_at') or last.get('created_at')}"))
    cards.append(make_card(
        s, limits, "llm_probe", body={"dry_run": False},
        reason=f"{label} refuses calls ({calls.get('access_refused', 0)} account-level refusals on "
               f"{len(calls.get('refused_models') or [])} model(s) in {window} min). Check the account level first: "
               "on 29/10 hours went into model-by-model checks before the refusal was found to be account-wide.",
        evidence=[f"conn:{key}", f"calls:{key}", "probe"], predictions=probe_preds,
        measures=[m("probe_finished", "nonzero", "A connection probe finished after the card was done")],
        if_nothing="The cause stays a guess: account-wide and model-level refusals need different fixes.",
        cost_usd=measured_cost, cost_basis="DRY RUN estimate" if measured_cost is not None else
        "A few cents at most (32 output tokens per enabled deployment, code)",
        precondition=None if probe.get("switch_on") else "A live run is refused (409) while the daily probe switch "
        "is off; the DRY RUN always works and lists what it would call."))

    served = ", ".join(fb) or "no other connection yet"
    cards.append(_observe(
        s, limits,
        reason=f"The breaker already skips {label}; each stage moves to its next connection ({served}).",
        evidence=[f"conn:{key}", f"stages:{key}"] + [f"calls:{k}" for k in fb] + [f"spend:{k}" for k in fb],
        if_nothing=f"Spend continues at about {now_h:g} USD/h on the fallback ({base_h:g} USD/h is the "
                   f"{baseline_label(s)}); refused attempts keep adding latency.",
        predictions=[pred(f"Calls keep being answered by {served}: "
                          + ", ".join(f"{k} {(s.get('calls') or {})[k]['ok']} answered" for k in fb)
                          + f" in {window} min.", "measured", f"shared.llm_call_log, last {window} min")
                     if fb else pred("No other connection answered in the window: the stages may be down.",
                                     "measured", f"shared.llm_call_log, last {window} min")],
        measures=[m("answered_calls", "nonzero", f"Calls are answered by {served}", connections=fb)] if fb else []))

    wave = _wave_card(s, limits, [f"spend:{k}" for k in fb], now_h, spend_on=fb)
    if wave:
        cards.append(wave)

    order = _provider_order_moving_last(s, key)
    version = (s.get("routes") or {}).get("version")
    if order and isinstance(version, int):
        # The bodies the API takes: the preview (PreviewBody) only the change; the save (SaveBody) the change plus the
        # version it was read against (409 when another was saved since); the way back reverts to that version, its
        # expected_version being the one the save makes.
        cards.append(make_card(
            s, limits, "route_save", body={"provider_order": order, "expected_version": version},
            dry_run_body={"provider_order": order}, way_back_body={"expected_version": version + 1},
            params={"version": version},
            title=f"Move {label} behind its fallbacks for {len(order)} model(s) (preview first, $0)",
            reason=f"{len(st['primary'])} stage(s) try {label} first; each call loses one refused attempt before "
                   "the fallback answers.",
            evidence=[f"stages:{key}", "routes", f"conn:{key}"],
            predictions=[pred("The preview lists which stages change their first serving model and whether a "
                              "writer or judge change needs eval evidence (needs_evidence).", "measured",
                              "POST /admin/ai-gateway/route-control/preview, $0, no model call"),
                         pred("Each moved stage saves one refused attempt per call.", "code",
                              "the gateway tries the provider order in turn")],
            if_nothing="Every call keeps trying the refused connection first, then falls back.",
            cost_usd=None, cost_basis="The fallback's own price per call (paid); the preview costs 0 USD.",
            stages=len(st["stages"]),
            measures=[m("refused_calls", "down", f"Refused calls on {label} fall", connection_key=key)],
            precondition=f"The live route version is still {version} when saving (the API refuses a stale "
                         "version). When the preview's needs_evidence names a writer or judge stage, the save also "
                         "needs eval_run_id (an eval run of the new order) or break_glass_reason (why now, at least "
                         f"{BREAK_GLASS_MIN_CHARS} characters; it raises an admin alert). The way back holds while no "
                         f"other version was saved after it (expected version {version + 1})."))

    denied = sum(d["n"] for d in (s.get("jobs") or {}).get("denied") or [])
    if denied:
        cards.append(make_card(
            s, limits, "retry_llm_denied", body={"dry_run": False},
            title=f"After {label} answers again: re-queue the {denied} refused job(s) (DRY RUN first)",
            reason=f"{denied} job(s) failed on the account refusal in {window} min.",
            evidence=["jobs:denied", f"conn:{key}"],
            predictions=[pred("The DRY RUN lists the jobs it would re-queue and holds those whose connection still "
                              "refuses.", "code", "shared/jobs/queue.py retry_by_reason")],
            if_nothing="The failed jobs stay failed; their work is redone only by hand.",
            cost_usd=None, cost_basis="The re-queued jobs' own AI cost; the DRY RUN shows the jobs first.",
            measures=[m("denied_jobs", "down", "Fewer jobs fail on an account refusal")],
            precondition=f"{label} answers again (a probe shows it); now it still refuses." if refusing
            else f"{label} answers again (a probe shows it).", precondition_met=not refusing))

    if conn.get("kind") == "bedrock":
        first = conn.get("last_error_at") or s["as_of"]
        cards.append(make_card(
            s, limits, "aws_support_case",
            reason="An account-level Bedrock refusal is decided by AWS (29/10: only a support case could explain "
                   "it).",
            evidence=[f"conn:{key}", f"calls:{key}"],
            predictions=[pred("AWS answers the case; the time is not in our hands.", "not_enough_data",
                              "one past case (29/10)")],
            if_nothing="The account stays refused until someone asks AWS.", cost_usd=0.0,
            cost_basis="Basic support plan, no charge.",
            copy_text=(f"Bedrock refuses model access on our AI connection {label} since {first}: "
                       f"{calls.get('access_refused', 0)} account-level refusals in {window} min on "
                       f"{len(calls.get('refused_models') or [])} model(s) "
                       f"({', '.join(calls.get('refused_models') or [])}). Please check the account's Bedrock "
                       "model access.")))

    skipped = [
        not_proposed("connection_disable", f"The breaker already skips {label}; switching it off by hand also "
                                           "stops its own recovery check."),
        not_proposed("anomaly_breaker_reset", "Unrelated to a refusal, and a loosening."),
        not_proposed("retry_llm_denied", "Not now for every failed job: they would be refused again and spend "
                                         "more.") if refusing and not denied else None,
    ]
    return cards, [x for x in skipped if x]


def llm_throttled(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    key = ctx["connection_key"]
    label = _conn_label(s, key)
    now_h, _base, fb = _paid_fallback_spend(s, key)
    window = s["window"]["minutes"]
    n = int(((s.get("calls") or {}).get(key) or {}).get("classes", {}).get("throttled") or 0)
    cards = [_observe(
        s, limits, reason=f"{label} throttles ({n} calls in {window} min); throttled calls wait for the window, then "
                          "move to the next deployment.",
        evidence=[f"calls:{key}", f"conn:{key}"],
        watch="Throttled calls are retried or moved on by the limiter; nothing fails for good.",
        if_nothing="Calls are slower while the throttle lasts.",
        measures=[m("throttled_calls", "down", f"Throttled calls on {label} fall", connection_key=key)])]
    wave = _wave_card(s, limits, [f"calls:{key}"], now_h, spend_on=fb)
    if wave:
        cards.append(wave)
    cards.append(make_card(
        s, limits, "app_bulk_pause", params={"app": "cis"}, body={"bulk_paused": True},
        title="Pause the bulk AI work of CIS", reason="Bulk jobs use the same limits as the live pages; pausing them "
                                                      "frees the throttled capacity.",
        evidence=[f"calls:{key}"],
        predictions=[pred("Bulk jobs wait; live requests keep their capacity.", "code",
                          "app profile bulk_paused: the bulk lane does not claim new work")],
        if_nothing="Bulk work and live requests keep competing for the throttled capacity.",
        cost_usd=0.0, cost_basis="A switch, no call.",
        measures=[m("throttled_calls", "down", f"Throttled calls on {label} fall", connection_key=key)]))
    return cards, [not_proposed("connection_disable", f"A throttle is temporary; switching {label} off stops it "
                                                      "for every stage.")]


def llm_cost_spike(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    key = ctx.get("connection_key")
    app = ctx.get("app") or "cis"
    sp = (s.get("spend") or {}).get(key or "") or {}
    now_h, base_h = float(sp.get("per_hour_usd") or 0.0), float(sp.get("baseline_hour_usd") or 0.0)
    ev = [f"spend:{key}"] if key else []
    if s.get("item"):
        ev.append("item")
    cards = [_observe(
        s, limits, reason=f"Spend is {now_h:g} USD/h against a {baseline_label(s)} of {base_h:g} USD/h.",
        evidence=ev, watch="The app's daily cap and the stage caps still stop the spend at their limits.",
        if_nothing=f"About {now_h * 24:g} USD a day at this rate until a cap stops it.",
        predictions=[pred(f"About {now_h:g} USD/h keeps being spent.", "measured",
                          f"shared.llm_call_log, last {s['window']['minutes']} min")],
        measures=[m("spend_per_hour", "nonzero", f"Spend on {key} continues", connections=[key])] if key else [])]
    wave = _wave_card(s, limits, ev, now_h, spend_on=[key] if key else None)
    if wave:
        cards.append(wave)
    cards.append(make_card(
        s, limits, "app_bulk_pause", params={"app": app}, body={"bulk_paused": True},
        title=f"Pause the bulk AI work of {app_label(app)}",
        reason="Bulk work is where a spend jump usually comes from.",
        evidence=ev, predictions=[pred("No new bulk AI job starts; live requests go on.", "code",
                                       "app profile bulk_paused")],
        if_nothing=f"About {now_h:g} USD/h continues.", cost_usd=0.0, cost_basis="A switch, no call.",
        measures=[m("app_spend_per_hour", "down", f"AI spend per hour of {app_label(app)} falls", app=app)]))
    return cards, [not_proposed("anomaly_breaker_reset", "A reset loosens the spend guard during a spike.")]


def queue_stuck(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    qid = ctx["queue"]
    jobs = s.get("jobs") or {}
    q = next((x for x in jobs.get("queues") or [] if x.get("id") == qid), {})
    ev = ["item", f"queue:{qid}"]
    cards: list[dict] = []
    if q.get("workers") == 0:
        cards.append(make_card(
            s, limits, "worker_start", reason="No live worker: queued jobs cannot start.", evidence=ev,
            predictions=[pred("Queued jobs start within a few minutes of a worker task running.", "code",
                              "the worker claims due jobs on each poll")],
            if_nothing="Every queued job keeps waiting.", cost_usd=None,
            cost_basis="The worker task's own ECS cost (the tech lead decides).",
            measures=[m("live_workers", "nonzero", "A worker is live again"),
                      m("oldest_wait_min", "down", "The oldest queued wait falls")]))
    for j in jobs.get("stuck") or []:
        if not j.get("registered"):
            cards.append(make_card(
                s, limits, "job_cancel", params={"job_id": j["id"]},
                title=f"Cancel the queued {j['kind']} job (kind not registered)",
                reason=f"No worker runs kind {j['kind']}: the job can never start and keeps the oldest wait high.",
                evidence=[f"job:{j['id']}", f"queue:{qid}"],
                predictions=[pred("The job leaves the queue; the oldest wait drops to the next job.", "code",
                                  "POST /admin/job-runner/jobs/{id}/cancel on a queued job")],
                if_nothing="The job stays queued for good and the queue keeps its warning.",
                cost_usd=0.0, cost_basis="A cancel calls nothing.",
                measures=[m("job_queued", "zero", "The job left the queue", job_id=j["id"]),
                          m("oldest_wait_min", "down", "The oldest queued wait falls")]))
    cards.append(_observe(
        s, limits, reason="The queue may drain on its own once the running jobs finish.", evidence=ev,
        watch="The oldest wait falls when a running job ends.", if_nothing="The wait keeps growing if nothing runs.",
        measures=[m("oldest_wait_min", "down", "The oldest queued wait falls")]))
    wave = _wave_card(s, limits, ev, 0.0)
    if wave:
        cards.append(wave)
    return cards, []


def server_errors(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    app = ctx["app"]
    deploys = [e for e in s.get("changes") or [] if e["kind"] in ("deploy", "rollout_failed") and e["app"] == app]
    cards = [_observe(
        s, limits, reason="A short spike often ends on its own.", evidence=["item", "changes"],
        watch="The rate is read again on the next refresh.", if_nothing="Users keep getting errors while it lasts.",
        measures=_item_closes(s))]
    if deploys:
        cards.append(make_card(
            s, limits, "ecs_rollback", title=f"Roll {app_label(app)} back to the revision before the last deploy",
            reason=f"A deploy of {app_label(app)} happened in the changes window ({deploys[0]['subject']} at "
                   f"{hhmm(deploys[0]['t'])}).", evidence=["item", "changes"], measures=_item_closes(s),
            predictions=[pred("The previous revision serves again within a few minutes of the rollback.", "code",
                              "ECS rollout of the previous task definition")],
            if_nothing="The errors continue if the deploy caused them.", cost_usd=0.0,
            cost_basis="No AI cost.", copy_text=f"Runbook rollback-ecs: roll back {deploys[0]['subject']}"))
    return cards, []


def generic(s: dict, limits: dict, ctx: dict) -> tuple[list[dict], list[dict]]:
    return [_observe(s, limits, reason="No playbook covers this item yet; its runbook link is on the row.",
                     evidence=["item"], watch="The item is read again on the next refresh.",
                     if_nothing="It stays on the Needs-attention list.")], []


PLAYBOOKS = {"llm_access_denied": llm_access_denied, "llm_throttled": llm_throttled,
             "llm_cost_spike": llm_cost_spike, "queue_stuck": queue_stuck, "server_errors": server_errors,
             "generic": generic}


def cards(s: dict, limits: dict, evidence_ids: set[str]) -> dict:
    """{playbook, cards (ranked), not_proposed}. Pure."""
    book, ctx = choose(s, limits)
    raw, skipped = PLAYBOOKS[book](s, limits, ctx)
    return {"playbook": book, "context": {k: v for k, v in ctx.items() if k != "signal"},
            "signal": ctx.get("signal"), "cards": rank(raw, evidence_ids), "not_proposed": skipped}


__all__ = ["PLAYBOOKS", "cards", "choose", "make_card", "pred", "signals"]
