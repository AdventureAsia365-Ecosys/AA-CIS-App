"""AA-1048 W2 (S273) — the import of a claim-label file into shared.eval_claim_label through the staff API, and the
payload the orchestrator sends.

Why an API and not the CLI on ECS: the labels and the export lines hold tour and tenant text. scripts/aa1048 is not in
the CIS image (scripts/ is dev-only, tests/unit/test_aa725_dockerfile_copy.py) and the pilot PII guard refuses a data
bundle uploaded to S3 for an ECS exec; a DB Migrate run-script logs in public and reads its input from the public Infra
repo. A staff-only route that runs the same panel / store code as `cli import` keeps the text on the wire to the API
and in the Dev DB only (POST /admin/writing-memory/claim-labels/import, api/routers/admin_claim_labels.py).

Payload (built locally by `python -m scripts.aa1048.cli payload`, never committed):
  {"set_id", "set_version", "dry_run": true, "export": [<slim export line>], "judgements": [<panel / tech-lead line>]}
  A slim export line keeps only what the consolidation reads (EXPORT_KEYS): no source text, field text, context or
  evidence, so a 150-claim round is about half a megabyte. A line's claim hash must equal
  claims.claim_hash(content sha, field, claim text): a line edited after the export is refused.

Idempotent: a judgement already stored with the same class and label is counted `unchanged`, never written twice; one
stored with another class or label is a conflict (409: a relabel is a new set version); panel rows reach a version once
(the first import), tech-lead rows are added to it as the adjudication goes. Every applied import writes one
acp_shared.audit_log row (counts and ids, never text).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Optional
from uuid import UUID

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import policy
from services.ground_truth import store

PAYLOAD_SCHEMA = "aa1048-claim-import/1"
EXPORT_KEYS = ("claim_hash", "set_id", "writer", "scope", "tenant_id", "refs", "content_sha256", "field", "span",
               "claim_text", "stratum", "split")
REF_KEYS = ("sample_id", "piece_id", "tour_id", "version_id")
MAX_EXPORT_LINES = 5000
MAX_JUDGEMENTS = 15000
AUDIT_ACTION = "ground_truth.claim_labels_import"
AUDIT_RESOURCE = "eval_claim_label_set"
_SAMPLE_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def slim_line(line: dict) -> dict:
    """The keys of an export line the consolidation reads (EXPORT_KEYS; refs limited to REF_KEYS)."""
    out = {k: line.get(k) for k in EXPORT_KEYS if k in line}
    out["refs"] = {k: v for k, v in dict(line.get("refs") or {}).items() if k in REF_KEYS and v is not None}
    return out


def _uuid_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(UUID(str(value)))


def _is_uuid(value: Any) -> bool:
    try:
        _uuid_or_none(value)
        return True
    except (TypeError, ValueError):
        return False


def clean_refs(line: dict) -> tuple[dict, list[str]]:
    """A slim line whose piece_id / version_id is not a uuid (S273: the AA-729 fixture scrub masked part of one T9
    piece id as a phone number) loses that ref when its sample_id still names the object: the split group reads tour
    and sample before piece / version (claims.SPLIT_GROUP_REFS), so the side never moves. Returns the line and the refs
    dropped; anything else stays for check_line to refuse."""
    refs = dict(line.get("refs") or {})
    dropped = [k for k in ("piece_id", "version_id") if refs.get(k) is not None and not _is_uuid(refs[k])]
    if not dropped or not refs.get("sample_id"):
        return line, []
    return {**line, "refs": {k: v for k, v in refs.items() if k not in dropped}}, dropped


def check_line(line: dict) -> Optional[str]:
    """Why an export line cannot be imported (None when it can). The claim hash is recomputed from the content sha,
    field and claim text, so a line changed after the export, or a hash typed by hand, is refused."""
    h = line.get("claim_hash")
    if not cl.valid_hex64(h) or not cl.valid_hex64(line.get("content_sha256")):
        return "claim_hash / content_sha256 is not a 64-hex sha"
    if not cl.valid_field(line.get("field")):
        return "bad field"
    text = line.get("claim_text")
    span = line.get("span")
    if not isinstance(text, str) or not text or len(text) > cl.MAX_CLAIM_CHARS:
        return "bad claim_text"
    if (not isinstance(span, list) or len(span) != 2 or not all(isinstance(x, int) and not isinstance(x, bool)
                                                                 for x in span)
            or not 0 <= span[0] < span[1] or span[1] - span[0] != len(text)):
        return "the span does not fit the claim text"
    if cl.claim_hash(line["content_sha256"], line["field"], text) != h:
        return "claim_hash is not the hash of its content sha, field and claim text"
    writer, scope = line.get("writer"), line.get("scope")
    if writer not in cl.WRITERS:
        return f"unknown writer {writer!r}"
    try:
        tenant = _uuid_or_none(line.get("tenant_id"))
    except (TypeError, ValueError):
        return "tenant_id is not a uuid"
    if (scope == "platform") != (writer == "a1") or (scope == "tenant") != bool(tenant):
        return f"scope {scope!r} / tenant does not fit writer {writer!r}"
    refs = line.get("refs")
    if not isinstance(refs, dict):
        return "refs is not an object"
    for k in ("piece_id", "tour_id", "version_id"):
        try:
            _uuid_or_none(refs.get(k))
        except (TypeError, ValueError):
            return f"refs.{k} is not a uuid"
    sample = refs.get("sample_id")
    if sample is not None and not _SAMPLE_ID_RE.match(str(sample)):
        return "refs.sample_id is not a sample id"
    if not any(refs.get(k) for k in ("sample_id", "piece_id", "version_id")):
        return "an object needs a sample_id, piece_id or version_id"
    if line.get("stratum", "representative") not in cl.STRATA:
        return "unknown stratum"
    if line.get("split") is not None and line["split"] not in cl.SPLITS:
        return "unknown split"
    return None


def build_payload(export_lines: Iterable[dict], judgements: Iterable[dict], *, set_id: str, set_version: str,
                  dry_run: bool = True) -> dict:
    """The import payload: the judgement lines as given and the slim export lines of the claims they judge (an export
    line nobody judged is left out: the consolidation only reads judged claims, and their audit draw is the same)."""
    if not cl.valid_set(set_id, set_version):
        raise pn.PanelError(f"bad set {set_id!r} / version {set_version!r}")
    js = [dict(j) for j in judgements]
    judged = {str(j.get("claim_hash") or "") for j in js}
    export, dropped = [], 0
    for e in export_lines:
        if e.get("claim_hash") in judged:
            line, gone = clean_refs(slim_line(e))
            export.append(line)
            dropped += bool(gone)
    if len(export) > MAX_EXPORT_LINES or len(js) > MAX_JUDGEMENTS:
        raise pn.PanelError(f"too large for one import ({len(export)} claims, {len(js)} judgements; at most "
                            f"{MAX_EXPORT_LINES} / {MAX_JUDGEMENTS}): split the round into set versions")
    return {"schema": PAYLOAD_SCHEMA, "set_id": set_id, "set_version": set_version, "dry_run": dry_run,
            "export": export, "judgements": js, "refs_dropped": dropped}


def consolidate_payload(export_lines: list[dict], judgements: list[dict], *, set_id: str,
                        set_version: str) -> pn.Consolidated:
    """Validate the payload and consolidate it (panel.parse_judgements + panel.consolidate, as `cli import`). Any
    problem refuses the whole payload (PanelError), nothing is half applied."""
    problems: list[str] = []
    by_hash: dict[str, dict] = {}
    for i, e in enumerate(export_lines, 1):
        why = check_line(e) if isinstance(e, dict) else "not an object"
        if why:
            problems.append(f"export #{i}: {why}")
            continue
        if e["claim_hash"] in by_hash:
            problems.append(f"export #{i}: the same claim twice")
            continue
        by_hash[e["claim_hash"]] = slim_line(e)
    if problems:
        raise pn.PanelError("; ".join(problems[:20]) + (f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""))
    js = pn.parse_judgements(judgements, export_by_hash=by_hash)
    return pn.consolidate(list(by_hash.values()), js, set_id=set_id, set_version=set_version,
                          policy_sha=policy.policy_sha())


def judgement_key(row: dict) -> tuple[str, str, Optional[str]]:
    return (row["claim_hash"], row["labeller_kind"], row.get("lens"))


def plan_rows(rows: list[dict], existing: dict[tuple, tuple[str, str]], *, panel_exists: bool) -> dict:
    """Split consolidated rows against what the version already holds: `new` rows to write, `unchanged` (same class
    and label already stored) and `conflicts` (stored with another class or label). New panel rows in a version that
    already has panel rows are refused (append-only: a relabel is a new set version). Pure."""
    new, unchanged, conflicts = [], 0, 0
    for r in rows:
        got = existing.get(judgement_key(r))
        if got is None:
            new.append(r)
        elif got == (r["claim_class"], r["label"]):
            unchanged += 1
        else:
            conflicts += 1
    late_panel = sum(1 for r in new if r["labeller_kind"] == "agent_panel") if panel_exists else 0
    return {"new": new, "unchanged": unchanged, "conflicts": conflicts, "late_panel": late_panel}


async def import_labels(conn, payload: dict, *, actor: str) -> dict:
    """Validate, consolidate and (unless dry_run) write one payload. Raises PanelError (a bad payload: 422) or
    store.StoreRefused (the version already holds other labels for these claims: 409). Counts only in the result."""
    set_id, version = payload["set_id"], payload["set_version"]
    apply = not payload.get("dry_run", True)
    cons = consolidate_payload(list(payload.get("export") or []), list(payload.get("judgements") or []),
                               set_id=set_id, set_version=version)
    existing = await store.existing_judgements(conn, set_id, version)
    panel_exists = any(k[1] == "agent_panel" for k in existing)
    plan = plan_rows(cons.rows, existing, panel_exists=panel_exists)
    if plan["conflicts"]:
        raise store.StoreRefused(f"{plan['conflicts']} judgement(s) differ from the ones {set_id}@{version} holds: "
                                 "import a relabel as a new set version")
    if plan["late_panel"]:
        raise store.StoreRefused(f"{set_id}@{version} already has panel rows and this file adds {plan['late_panel']} "
                                 "new panel judgement(s): import a relabel as a new set version")
    summary: dict[str, Any] = {"set": f"{set_id}@{version}", "dry_run": not apply, "rows": len(cons.rows),
                               "unchanged": plan["unchanged"], "new": len(plan["new"]), "inserted": 0, "skipped": 0}
    if apply and plan["new"]:
        async with conn.transaction():
            wrote = await store.insert_rows(conn, plan["new"], apply=True)
            summary.update(inserted=wrote["inserted"], skipped=wrote["skipped"])
            from services.acp_shared.audit_log import write_audit_log

            await write_audit_log(
                conn, tenant_id=None, actor=actor[:128], action=AUDIT_ACTION, resource_type=AUDIT_RESOURCE,
                resource_id=f"{set_id}@{version}"[:64],
                details={"rows": len(cons.rows), "inserted": wrote["inserted"], "skipped": wrote["skipped"],
                         "unchanged": plan["unchanged"], "policy_sha": policy.policy_sha()})
    summary.update(counts(cons.rows), states=dict(Counter(cons.states.values())), problems=cons.problems,
                   policy_source=policy.POLICY_SOURCE)
    return summary


def counts(rows: list[dict]) -> dict:
    """Aggregate counts of label rows (the import result and the verification read): per label, labeller kind,
    labeller, lens, panel state, split and writer. Never a text, a reason or a claim hash."""
    def by(key: str) -> dict:
        return dict(sorted(Counter(str(r.get(key) or "-") for r in rows).items()))

    return {"by_label": by("label"), "by_labeller_kind": by("labeller_kind"), "by_labeller": by("labeller"),
            "by_lens": by("lens"), "by_adjudication": by("adjudication"), "by_split": by("split"),
            "by_writer": by("writer"), "claims": len({r["claim_hash"] for r in rows})}


def finals_rows(finals: dict[str, pn.FinalLabel]) -> list[dict]:
    """Final labels as the lines of a `cli finals` file (what `cli detectors` and `r1_calibrate` read)."""
    return [{"claim_hash": f.claim_hash, "claim_class": f.claim_class, "label": f.label, "source": f.source,
             "writer": f.writer, "sample_id": f.sample_id, "stratum": f.stratum, "split": f.split,
             "content_sha256": f.content_sha256, "claim_text": f.claim_text} for f in finals.values()]


__all__ = ["AUDIT_ACTION", "EXPORT_KEYS", "MAX_EXPORT_LINES", "MAX_JUDGEMENTS", "PAYLOAD_SCHEMA", "build_payload",
           "check_line", "clean_refs", "consolidate_payload", "counts", "finals_rows", "import_labels",
           "judgement_key", "plan_rows", "slim_line"]
