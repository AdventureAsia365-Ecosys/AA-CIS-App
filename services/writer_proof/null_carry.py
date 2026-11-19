"""AA-1051 (S285) — a W5 null run's non_inferior verdict carried to a LATER version of the same golden set.

Why. The null run (root ADR 0020 W5) proves the eval stable: the pinned judge, on the null run's writer GATE items,
re-judged an unchanged writer and found no regression. `runner.same_instrument` opens a writer proof only through a
null run on the SAME judge and golden version. A new golden version that only adds samples of another writer (aa729
1.6.0 = 1.5.0 plus 22 S1 samples, S285) then closes every proof that needs the new version, although nothing the null
run measured has changed, and the only way back is a paid re-verify plus a new null run.

The rule (tech lead S285: a reviewed code change, not a new paid run). A registered null run pinned to golden
`name@old` opens a proof pinned to `name@new` when
  (a) both pin the same judge (model_key, connection, claim instrument);
  (b) the golden set name is the same and `new` is a later semantic version than `old` (a null run carries forward,
      never back);
  (c) every GATE item of the null run's writer is the same in both versions: the same sample ids on GATE (none
      removed, moved to another side or added), and each item byte-identical in every stored column but its
      per-version ids (`ITEM_COLUMNS`: input, stored control output, labels, tiers, trap, the input hash, ...) and in
      its stored DEV / CAL / GATE row (side, rule, bucket); the items the null run evaluated (its sample, resolved on
      `old` as its report resolves it) are among them. For a writer whose judge source reads the set's shared inputs
      (brand packs: A1 / T2), those must be equal too; the T9 judge source does not read them.
jsonb columns are compared as the text Postgres prints for them (jsonb is stored normalised, so equal values print
the same bytes). Item ids differ in every version by construction (the importer writes new rows) and are not
compared; the sample id is the identity across versions, as R2's control pairing uses it.

Claim labels (shared.eval_claim_label) are not compared: the null run's verdict never reads them (its judge is the
pinned instrument, whose calibration is its own AA-1048 record, named by the instrument id in (a)).

What cannot be proved from stored data refuses: a version that is missing, an item of the writer without a stored
side, no GATE item at all. `null_covers` lists every difference; the runner's null gate names them in its blocking
line. `same_instrument` stays strict for every other caller.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from services.writer_proof import prereg as pr

SAME_RULE = "same_golden_version"
CARRY_RULE = "golden_carry"
# The writers whose judge source does not read the set's shared inputs (eval_judge.judge_source_text: a T9 item's
# SOURCE is its own stored input). A unit test keeps it equal to eval_writers.T9_WRITERS.
NO_SHARED_INPUT_WRITERS = ("t9_blog", "t9_social")
# Every stored column of a golden item but its per-version ids (item_id, set_id) and created_at
ITEM_COLUMNS = ("sample_id", "writer", "channel", "scope", "tenant_id", "split", "trap", "kind", "tiers", "country",
                "days", "input", "control_output", "post_edit", "pipeline_verdict", "label", "label_verdict",
                "label_source", "injection_spec", "refs", "input_sha256", "pick_reason")
# The item's frozen DEV / CAL / GATE row (shared.eval_golden_split, CIS 400)
SPLIT_COLUMNS = ("eval_split", "split_rule", "split_bucket")
MAX_LISTED = 8  # technical-constant: differences named in a blocking line (display only; the dict keeps all)

SET_SQL = """
    SELECT set_id::text AS set_id, version, shared_inputs::text AS shared_inputs
      FROM shared.eval_golden_set WHERE name = $1 AND version = $2
"""
ITEMS_SQL = """
    SELECT g.sample_id, g.writer, g.channel, g.scope, g.tenant_id::text AS tenant_id, g.split, g.trap, g.kind, g.tiers,
           g.country, g.days, g.input::text AS input, g.control_output::text AS control_output, g.post_edit,
           g.pipeline_verdict::text AS pipeline_verdict, g.label::text AS label, g.label_verdict, g.label_source,
           g.injection_spec::text AS injection_spec, g.refs::text AS refs, g.input_sha256, g.pick_reason,
           s.eval_split, s.rule AS split_rule, s.bucket::text AS split_bucket
      FROM shared.eval_golden_item g
      LEFT JOIN shared.eval_golden_split s ON s.set_id = g.set_id AND s.sample_id = g.sample_id
     WHERE g.set_id = $1::uuid AND g.writer = $2
     ORDER BY g.sample_id
"""

_SEMVER = re.compile(r"^([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})$")


def semver(version: Any) -> Optional[tuple[int, int, int]]:
    """(major, minor, patch) of a golden version, None when it is not one. Pure."""
    m = _SEMVER.match(str(version or ""))
    return None if m is None else (int(m[1]), int(m[2]), int(m[3]))


def judge_pin(pre: pr.Prereg) -> tuple:
    """The judge a pre-registration pins: (model_key, connection, claim instrument). Pure."""
    j = pre.doc.get("judge") or {}
    return tuple(j.get(k) for k in ("model_key", "connection", "instrument"))


def golden_of(pre: pr.Prereg) -> tuple[Optional[str], Optional[str]]:
    gs = pre.doc.get("golden_set") or {}
    return gs.get("name"), gs.get("version")


def carry_problem(null: pr.Prereg, pre: pr.Prereg) -> Optional[str]:
    """Why the null run's verdict cannot be carried to the proof's golden version by rules (a) and (b), None when it
    may be (rule (c), the items, is `null_covers`). Pure."""
    if judge_pin(null) != judge_pin(pre):
        return "another judge"
    (n_name, n_ver), (p_name, p_ver) = golden_of(null), golden_of(pre)
    if not n_name or n_name != p_name:
        return f"another golden set ({n_name} vs {p_name})"
    a, b = semver(n_ver), semver(p_ver)
    if a is None or b is None:
        return f"no comparable golden versions ({n_ver} vs {p_ver})"
    if a == b:
        return f"the same golden version {n_ver} (the strict rule decides)"
    if a > b:
        return f"its golden version {n_ver} is later than the proof's {p_ver}: a null run carries forward only"
    return None


def _side_problems(items: list[dict], writer: str, label: str) -> list[str]:
    missing = sorted(it["sample_id"] for it in items if it.get("writer") == writer and it.get("eval_split") is None)
    if missing:
        return [f"{len(missing)} {writer} items of {label} have no stored eval split (e.g. {missing[0]}): their side "
                "cannot be proved"]
    return []


def _gate(items: list[dict], writer: str) -> dict[str, dict]:
    return {it["sample_id"]: it for it in items if it.get("writer") == writer and it.get("eval_split") == "gate"}


def _differing_columns(a: dict, b: dict) -> list[str]:
    return [c for c in ITEM_COLUMNS + SPLIT_COLUMNS if a.get(c) != b.get(c)]


def _gate_differences(g_old: dict, g_new: dict, new_items: list[dict], writer: str, v_new: str) -> list[str]:
    """The GATE items of `writer` removed, moved, added or changed between two versions. Pure."""
    out: list[str] = []
    side_new = {it["sample_id"]: it.get("eval_split") for it in new_items if it.get("writer") == writer}
    for sid in sorted(set(g_old) - set(g_new)):
        out.append(f"{writer} GATE item {sid} moved to {side_new[sid]} in {v_new}" if side_new.get(sid)
                   else f"{writer} GATE item {sid} is missing from {v_new}")
    out.extend(f"{writer} GATE item {sid} was added in {v_new}" for sid in sorted(set(g_new) - set(g_old)))
    for sid in sorted(set(g_old) & set(g_new)):
        cols = _differing_columns(g_old[sid], g_new[sid])
        if cols:
            out.append(f"{writer} GATE item {sid} differs in {', '.join(cols)}")
    return out


def _sample_problems(null: pr.Prereg, old_items: list[dict], g_old: dict, v_old: str) -> tuple[list[str], int]:
    """The items the null run evaluated (its sample, resolved on the old version as its report resolves it) must be
    GATE items of its writer there. (problems, distinct sample items). Pure."""
    res = pr.resolve(null, old_items)
    sample = list(res.doc.get("sample_ids") or []) + list(res.doc.get("noise_sample_ids") or [])
    out: list[str] = []
    off = sorted(set(sample) - set(g_old))
    if off:
        out.append(f"{len(off)} items of the null run's sample are not {null.writer} GATE items of {v_old} "
                   f"(e.g. {off[0]})")
    if not sample:
        out.append("the null run's sample is empty")
    return out, len(set(sample))


def null_covers(null: pr.Prereg, pre: pr.Prereg, old: Optional[dict], new: Optional[dict]) -> dict:
    """Rule (c): does every GATE item of the null run's writer read the same in the proof's golden version?
    `old` / `new`: `read_items` of the null run's and the proof's version ({"version", "shared_inputs", "items"}),
    None when the version is not stored. {"ok", "golden_carried_from", "golden_carried_to", "items_compared",
    "sample_items", "differs"} (`differs` empty when ok). Pure."""
    writer = null.writer
    name, v_old = golden_of(null)
    v_new = golden_of(pre)[1]
    out: dict[str, Any] = {"ok": False, "golden_carried_from": v_old, "golden_carried_to": v_new,
                           "items_compared": 0, "sample_items": 0, "differs": []}
    differs: list[str] = out["differs"]
    differs.extend(f"golden {name}@{ver} is not stored" for ver, got in ((v_old, old), (v_new, new)) if got is None)
    if differs:
        return out
    differs.extend(_side_problems(old["items"], writer, f"{name}@{v_old}")
                   + _side_problems(new["items"], writer, f"{name}@{v_new}"))
    g_old, g_new = _gate(old["items"], writer), _gate(new["items"], writer)
    if not g_old:
        differs.append(f"{name}@{v_old} has no {writer} GATE item")
    differs.extend(_gate_differences(g_old, g_new, new["items"], writer, v_new))
    sample_problems, n_sample = _sample_problems(null, old["items"], g_old, v_old)
    differs.extend(sample_problems)
    if writer not in NO_SHARED_INPUT_WRITERS and old.get("shared_inputs") != new.get("shared_inputs"):
        differs.append(f"the shared inputs the {writer} judge source reads differ between {v_old} and {v_new}")
    out["items_compared"] = len(g_old)
    out["sample_items"] = n_sample
    out["ok"] = not differs
    return out


def blocking_line(null: pr.Prereg, pre: pr.Prereg, cover: dict) -> str:
    """One readable line: why a non_inferior null run of an earlier version was not carried. Pure."""
    name = golden_of(null)[0]
    d = cover["differs"]
    more = f" (+{len(d) - MAX_LISTED} more)" if len(d) > MAX_LISTED else ""
    return (f"registered null run {null.id} ({name}@{cover['golden_carried_from']}) is not carried to "
            f"{name}@{cover['golden_carried_to']}: {'; '.join(d[:MAX_LISTED])}{more}")


async def read_items(conn, name: str, version: str, writer: str) -> Optional[dict]:
    """The stored items of one writer in golden `name@version`, every compared column, None when the version is not
    stored. READ ONLY."""
    s = await conn.fetchrow(SET_SQL, name, version)
    if s is None:
        return None
    rows = await conn.fetch(ITEMS_SQL, s["set_id"], writer)
    return {"version": s["version"], "shared_inputs": s["shared_inputs"],
            "items": [{**dict(r), "tiers": list(r["tiers"] or [])} for r in rows]}


async def load_pair(pool, null: pr.Prereg, pre: pr.Prereg) -> tuple[Optional[dict], Optional[dict]]:
    """`read_items` of the null run's and the proof's version, in one READ ONLY transaction."""
    name, v_old = golden_of(null)
    v_new = golden_of(pre)[1]
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            return (await read_items(conn, name, v_old, null.writer),
                    await read_items(conn, name, v_new, null.writer))


__all__ = ["CARRY_RULE", "ITEM_COLUMNS", "NO_SHARED_INPUT_WRITERS", "SAME_RULE", "SPLIT_COLUMNS", "blocking_line",
           "carry_problem", "golden_of", "judge_pin", "load_pair", "null_covers", "read_items", "semver"]
