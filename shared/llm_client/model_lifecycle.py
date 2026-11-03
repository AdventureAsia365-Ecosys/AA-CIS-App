"""AA-1024 D2 (S263 gateway plan, OPS-04) — model lifecycle as data.

Bedrock retires models: ``ListFoundationModels`` gives each foundation model a ``modelLifecycle`` (status ACTIVE or
LEGACY, plus ``legacyTime`` / ``endOfLifeTime`` / ``publicExtendedAccessTime`` in botocore >= 1.40), and a model
past its end of life disappears from the list. This module turns that into the five lifecycle columns of
``shared.llm_model_catalog`` (migration 352), written by the nightly job ``llm_model_lifecycle_sync``
(services/jobs/llm_model_lifecycle_job.py), read by the OPS-12 digest and the admin bell.

Status vocabulary (``lifecycle_status``):
  active / legacy  the worst ``modelLifecycle.status`` over the model's Bedrock deployments whose account answered;
  unlisted         an account answered but its list has no such model id (past end of life, or a wrong wire id);
  unknown          a Bedrock model none of whose accounts answered, with no status before (an answered status is
                   KEPT when its account later refuses: a refusal says nothing about the model);
  untracked        a model with no Bedrock deployment (OpenAI, Anthropic API, Jev, mock): no lifecycle source.

Fail soft: an account that refuses (acc3 has an account-level Bedrock hold since 29/10/2026) or errors is recorded
per connection in ``lifecycle_detail`` (``access`` refused | error, ``error_class``, masked message) and never
fails the run. A control-plane list is free and makes no model call, so nothing is written to shared.llm_call_log.

The Bedrock control-plane client is built here because ``get_satellite_client`` is a gateway internal
(tests/unit/test_aa685_no_raw_model_calls.py).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import structlog

logger = structlog.get_logger()

ACTIVE, LEGACY, UNLISTED, UNKNOWN, UNTRACKED = "active", "legacy", "unlisted", "unknown", "untracked"
STATUSES = (ACTIVE, LEGACY, UNLISTED, UNKNOWN, UNTRACKED)
# Worst first: the status of a model with several answering deployments is the worst of theirs.
_RANK = {UNLISTED: 0, LEGACY: 1, ACTIVE: 2}
# Days before a routed model's end of life from which the digest and the bell warn (plan D2).
EOL_WARN_DAYS = 90

ACCESS_OK, ACCESS_REFUSED, ACCESS_ERROR = "ok", "refused", "error"
ACCESS_DISABLED, ACCESS_UNCONFIGURED = "disabled", "unconfigured"
_REFUSED_CLASSES = ("access_denied", "credential", "quota_exhausted", "throttled")

# Geographic / global inference-profile prefixes: "global.anthropic.claude-x" invokes foundation model
# "anthropic.claude-x".
_PROFILE_PREFIXES = ("us-gov.", "global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.")

# Control-plane client settings: a short read timeout, two attempts (a list is idempotent and free).
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 20
MAX_ATTEMPTS = 2


@dataclass
class Listing:
    """One Bedrock connection's answer: ``models`` {foundation model id: summary} when ``access`` is ok."""
    connection_key: str
    access: str
    models: dict = field(default_factory=dict)
    error_class: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"connection_key": self.connection_key, "access": self.access, "models_listed": len(self.models),
                "error_class": self.error_class, "error": self.error}


@dataclass
class Lifecycle:
    """The lifecycle columns of one catalog row."""
    model_key: str
    status: str
    eol_date: Optional[date] = None
    legacy_date: Optional[date] = None
    detail: dict = field(default_factory=dict)
    kept: bool = False          # an earlier answered status kept because no account answered this time


# ── pure ───────────────────────────────────────────────────────────────────────────────────────────────────────


def foundation_model_id(wire_id: Optional[str]) -> Optional[str]:
    """The foundation model id a deployment's wire id invokes: the inference-profile prefix removed, the id taken from
    a foundation-model or system inference-profile ARN. None for an application inference profile (its model is not
    in the id) or an empty value."""
    w = (wire_id or "").strip()
    if not w:
        return None
    if w.startswith("arn:"):
        if ":foundation-model/" in w or ":inference-profile/" in w:
            w = w.rsplit("/", 1)[-1]
        else:
            return None
    for p in _PROFILE_PREFIXES:
        if w.startswith(p):
            w = w[len(p):]
            break
    return w or None


def _to_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v.astimezone(timezone.utc) if v.tzinfo else v).date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def summarize(model_summary: dict) -> dict:
    """The fields kept from one ``modelSummaries`` entry (pure)."""
    lc = model_summary.get("modelLifecycle") or {}
    status = str(lc.get("status") or "").lower() or None
    return {"model_id": model_summary.get("modelId"), "status": status,
            "eol_date": _to_date(lc.get("endOfLifeTime")), "legacy_date": _to_date(lc.get("legacyTime")),
            "extended_access_date": _to_date(lc.get("publicExtendedAccessTime"))}


def match(model_id: Optional[str], listed: dict) -> Optional[dict]:
    """The listed summary of ``model_id``: the exact id, else the one id with the same base before ':' (a listed
    "x-v1:0" for a wire "x-v1"). None when absent or ambiguous."""
    if not model_id:
        return None
    if model_id in listed:
        return listed[model_id]
    base = model_id.split(":", 1)[0]
    same = sorted(k for k in listed if k.split(":", 1)[0] == base)
    return listed[same[0]] if len(same) == 1 else None


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def model_lifecycle(model_key: str, pairs: list[tuple[str, str]], listings: dict[str, Listing],
                    previous: Optional[dict] = None, *, checked_at: Optional[datetime] = None) -> Lifecycle:
    """The lifecycle of one catalog model (pure). ``pairs``: its (Bedrock connection key, wire id) deployments;
    ``listings``: every Bedrock connection's answer; ``previous``: the row's stored columns (status, eol_date,
    legacy_date) or None."""
    stamp = (checked_at or datetime.now(timezone.utc)).isoformat()
    if not pairs:
        return Lifecycle(model_key, UNTRACKED, detail={"checked_at": stamp, "source": None,
                                                         "reason": "no Bedrock deployment: no lifecycle source"})
    per_conn: dict[str, dict] = {}
    answered: list[dict] = []
    for conn_key, wire in pairs:
        listing = listings.get(conn_key)
        if listing is None:
            per_conn[conn_key] = {"access": ACCESS_UNCONFIGURED, "wire_id": wire}
            continue
        if listing.access != ACCESS_OK:
            per_conn[conn_key] = {"access": listing.access, "error_class": listing.error_class, "wire_id": wire}
            continue
        fid = foundation_model_id(wire)
        found = match(fid, listing.models)
        if found is None:
            entry = {"access": ACCESS_OK, "wire_id": wire, "model_id": fid, "status": UNLISTED}
        else:
            entry = {"access": ACCESS_OK, "wire_id": wire, "model_id": found["model_id"],
                     # a status Bedrock adds later (or none) counts as legacy: it warns rather than hides
                     "status": found["status"] if found["status"] in (ACTIVE, LEGACY) else LEGACY,
                     "eol_date": _iso(found["eol_date"]), "legacy_date": _iso(found["legacy_date"]),
                     "extended_access_date": _iso(found.get("extended_access_date"))}
            if found["status"] not in (ACTIVE, LEGACY):
                entry["listed_status"] = found["status"]
        per_conn[conn_key] = entry
        answered.append(entry)
    detail = {"checked_at": stamp, "source": "bedrock:ListFoundationModels", "connections": per_conn}
    if not answered:
        prev_status = (previous or {}).get("status")
        if prev_status in (ACTIVE, LEGACY, UNLISTED):
            return Lifecycle(model_key, prev_status, _to_date((previous or {}).get("eol_date")),
                             _to_date((previous or {}).get("legacy_date")),
                             {**detail, "kept_from_previous": True}, kept=True)
        return Lifecycle(model_key, UNKNOWN, detail={**detail, "reason": "no Bedrock account answered"})
    status = min((e["status"] for e in answered), key=lambda s: _RANK[s])
    eols = [d for d in (_to_date(e.get("eol_date")) for e in answered) if d]
    legacies = [d for d in (_to_date(e.get("legacy_date")) for e in answered) if d]
    return Lifecycle(model_key, status, min(eols) if eols else None, min(legacies) if legacies else None, detail)


def deployment_pairs(model: dict, deployments: Iterable[dict], bedrock_keys: set[str]) -> list[tuple[str, str]]:
    """(connection key, wire id) of the model's deployments on a Bedrock connection (enabled or not: the lifecycle
    is the model's), sorted. A model with no such deployment row falls back to its catalog ``bedrock_profile_ids``
    ({connection key: profile id}) when its provider is bedrock."""
    mk = model["model_key"]
    pairs = sorted({(d["connection_key"], d["wire_id"]) for d in deployments
                    if d["model_key"] == mk and d["connection_key"] in bedrock_keys and d.get("wire_id")})
    if pairs or model.get("provider") != "bedrock":
        return pairs
    profiles = model.get("bedrock_profile_ids") or {}
    if isinstance(profiles, str):
        try:
            profiles = json.loads(profiles)
        except ValueError:
            profiles = {}
    return sorted((k, str(v)) for k, v in profiles.items() if k in bedrock_keys and v)


def route_keys(routes: Iterable[dict], aliases: Iterable[dict]) -> dict[str, list[str]]:
    """{catalog model key: [stages]} of the active routes: model_id, fallback_model_ids and shadow_model_id, an alias
    replaced by its ordered models (shared.llm_model_alias). Pure."""
    by_alias: dict[str, list[tuple[int, str]]] = {}
    for a in aliases:
        by_alias.setdefault(a["alias_key"], []).append((int(a["position"]), a["model_key"]))
    expand = {k: [m for _, m in sorted(v)] for k, v in by_alias.items()}
    out: dict[str, list[str]] = {}
    for r in routes:
        if not r.get("is_active", True):
            continue
        keys = [r.get("model_id"), *(r.get("fallback_model_ids") or []), r.get("shadow_model_id")]
        for key in keys:
            if not key:
                continue
            for mk in expand.get(key) or [key]:
                stages = out.setdefault(mk, [])
                if r["stage"] not in stages:
                    stages.append(r["stage"])
    return {k: sorted(v) for k, v in sorted(out.items())}


def warning(model_key: str, status: Optional[str], eol: Optional[date], today: date,
            stages: list[str]) -> Optional[dict]:
    """A routed model that needs a look (pure): end of life within ``EOL_WARN_DAYS`` (or past), LEGACY, or unlisted.
    None otherwise (active with a far or no EOL date, unknown, untracked)."""
    eol = _to_date(eol)
    days = (eol - today).days if eol else None
    soon = days is not None and days <= EOL_WARN_DAYS
    if not soon and status not in (LEGACY, UNLISTED):
        return None
    if days is not None and days < 0:
        why = f"past its end of life ({eol.isoformat()})"
    elif soon:
        why = f"end of life {eol.isoformat()} (in {days} days)"
    elif status == UNLISTED:
        why = "no longer listed by Bedrock (past end of life, or a wrong wire id)"
    else:
        why = "legacy on Bedrock" + (f", end of life {eol.isoformat()}" if eol else ", no end-of-life date yet")
    return {"model_key": model_key, "status": status, "eol_date": _iso(eol), "days_left": days,
            "stages": stages, "why": why,
            "message": f"Routed model {model_key} is {why}; stages: {', '.join(stages) or '—'}. "
                       "Move the route to another model before then (Settings › LLM Models)."}


def warnings(rows: Iterable[dict], routed: dict[str, list[str]], today: date) -> list[dict]:
    """``warning`` for every routed model (rows: model_key, lifecycle_status, eol_date), soonest first."""
    by_key = {r["model_key"]: r for r in rows}
    out = []
    for mk, stages in routed.items():
        r = by_key.get(mk) or {}
        w = warning(mk, r.get("lifecycle_status"), r.get("eol_date"), today, stages)
        if w:
            out.append(w)
    return sorted(out, key=lambda w: (w["days_left"] if w["days_left"] is not None else 10**6, w["model_key"]))


def eol_horizon(today: date) -> date:
    return today + timedelta(days=EOL_WARN_DAYS)


# ── Bedrock control plane ──────────────────────────────────────────────────────────────────────────────────────


def _control_client(conn):
    """A ``bedrock`` (control-plane) client for one Bedrock connection: the task role (acc2) directly, an assume-role
    connection (acc3) through the satellite session."""
    from botocore.config import Config

    cfg = Config(connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                 retries={"total_max_attempts": MAX_ATTEMPTS, "mode": "standard"})
    if conn.auth_mode == "assume_role":
        from shared.llm_client import bedrock_satellite

        session = bedrock_satellite._get_satellite_session(conn.connection_key)
        return session.client("bedrock", region_name=bedrock_satellite.satellite_region(conn.connection_key),
                              config=cfg)
    import boto3

    region = conn.region or os.environ.get("BEDROCK_REGION", "us-west-1")
    return boto3.client("bedrock", region_name=region, config=cfg)


def list_connection(conn, *, client_factory=None) -> Listing:
    """One Bedrock connection's ``ListFoundationModels`` (sync; run it in a thread). Never raises: a refusal or an
    error is the Listing's access state, with the masked message."""
    from shared.llm_client.errors import classify, redact_ids

    key = conn.connection_key
    if not conn.enabled:
        return Listing(key, ACCESS_DISABLED)
    problems = conn.policy_problems()
    if problems or conn.auth_mode not in ("task_role", "assume_role"):
        return Listing(key, ACCESS_UNCONFIGURED,
                       error="; ".join(problems) or f"auth_mode {conn.auth_mode!r} has no control-plane client")
    try:
        client = (client_factory or _control_client)(conn)
        resp = client.list_foundation_models()
    except Exception as e:  # noqa: BLE001 — a refusing account is the answer, never a failed run
        cls = classify(e)
        logger.warning("llm_lifecycle_list_failed", connection=key, error_class=cls,
                       error=redact_ids(str(e))[:200])
        return Listing(key, ACCESS_REFUSED if cls in _REFUSED_CLASSES else ACCESS_ERROR, error_class=cls,
                       error=redact_ids(f"{type(e).__name__}: {e}")[:300])
    models = {}
    for s in resp.get("modelSummaries") or []:
        if s.get("modelId"):
            models[s["modelId"]] = summarize(s)
    return Listing(key, ACCESS_OK, models)
