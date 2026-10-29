#!/usr/bin/env python3
"""AA-790 (S258) — pick the FIXED sample of T9 blog pieces for the writer A/B rounds and the panel calibration.
Read-only, $0, no model call, deterministic.

Why: rounds 8-10 ran on 8 pieces (5 blogs). The paired noise of one piece (control written twice, same panel) has an SD
of about 1.1 on three of the four lenses (S257 round-10 panel), so the mean of 8 pairs moves by about 0.4 on noise
alone, against a 0.15 bar. A larger sample that stays the same across rounds, plus a calibrated panel, comes before
any new prompt round (S257 proposal).

What it does:
  1. Reads every blog piece the repanel harness can rewrite (`acp_shared.content_piece`, channel blog, status
     approved / held, a stored `writer_input` with a seed and a brand pack, joined to its angle request and tenant).
  2. Keeps ONE piece per distinct input (sha256 of the stored seed; the newest piece): attempts and reruns of the same
     request write from the same seed, and two copies of one input would count one input twice.
  3. Marks the pieces that already carry a label (the active golden set's t9_blog label, else the majority of the
     `acp_shared.content_piece_label` rows): they are the panel's calibration anchors.
  4. Picks --size pieces: forced ids first (--force-ids, e.g. the round 1-10 harness pieces, for the link to old
     rounds), then an equal share per tenant (a tenant with fewer inputs gives all it has, the rest goes to the
     others); inside a tenant up to half the share are labelled anchors, then the rest, each part spread round-robin
     over destination, held pieces first. Order: sha256("<seed>:<piece_id>").
Output: one JSON document on stdout (the manifest: ids, tenant, destination, status, route size, writer_input and text
hashes, label verdict and source; never text) — store it as the sample's version (root audit folder), and the pool
counts that say whether --size distinct inputs exist at all.

Runs in aa-cis-dev-api / -worker as cis_app (container DATABASE_URL), read-only session; the file is exec'd alone
(tools/ecs/ecsx.sh), so it imports nothing from scripts/. Arguments through the environment or argv:
  python3 fixed_sample.py [--size 24] [--seed 790] [--force-ids <id,id>] [--version v1]
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_SIZE = 24          # >= 20 (S257 proposal); 8 per tenant with three tenants
ANCHOR_SHARE = 0.5         # at most half of a tenant's share are labelled anchors
PIECE_LABEL_VERDICT = {"as_is": "pass", "light": "pass", "heavy": "fail", "reject": "fail"}

# ── pure parts (unit-tested) ───────────────────────────────────────────────────────────────────────────────────────


def sha(text: Any) -> str:
    if not isinstance(text, str):
        text = json.dumps(text, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def order_key(seed: int, ident: str) -> str:
    return hashlib.sha256(f"{seed}:{ident}".encode()).hexdigest()


def distinct_inputs(rows: list[dict]) -> tuple[list[dict], dict]:
    """One row per distinct seed (the newest piece wins; ties by piece_id). Returns (kept, counts)."""
    best: dict[str, dict] = {}
    for r in rows:
        k = r["seed_sha"]
        cur = best.get(k)
        if cur is None or (r["created_at"], r["piece_id"]) > (cur["created_at"], cur["piece_id"]):
            best[k] = r
    kept = sorted(best.values(), key=lambda r: r["piece_id"])
    return kept, {"pieces": len(rows), "distinct_inputs": len(kept),
                  "distinct_requests": len({r["request_id"] for r in rows})}


def shares(size: int, avail: dict[str, int]) -> dict[str, int]:
    """An equal share per group, capped by what the group has; what a small group cannot fill goes to the others
    (largest first, ties by name). Sums to min(size, total)."""
    out = {g: 0 for g in avail}
    left = min(size, sum(avail.values()))
    while left > 0:
        open_groups = sorted((g for g in avail if out[g] < avail[g]), key=lambda g: (out[g], -avail[g], g))
        if not open_groups:
            break
        each = max(1, left // len(open_groups))
        for g in open_groups:
            take = min(each, avail[g] - out[g], left)
            out[g] += take
            left -= take
            if left == 0:
                break
    return out


def _round_robin(rows: list[dict], n: int, key) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    out: list[dict] = []
    while len(out) < n and any(buckets[k] for k in order):
        for k in order:
            if buckets[k] and len(out) < n:
                out.append(buckets[k].pop(0))
    return out


def _held_first(rows: list[dict], seed: int) -> list[dict]:
    """Held pieces first (the cases the gates caught, rarer than approved ones), then the seeded order; the round-robin
    over destinations runs on this order."""
    return sorted(rows, key=lambda r: (r["status"] != "held", order_key(seed, r["piece_id"])))


def select(rows: list[dict], *, size: int, seed: int, force_ids: tuple[str, ...] = ()) -> list[dict]:
    """The fixed sample (see the module docstring, step 4). Each pick carries `why`: forced, anchor or spread."""
    ordered = sorted(rows, key=lambda r: order_key(seed, r["piece_id"]))
    by_id = {r["piece_id"]: r for r in ordered}
    picks = [{**by_id[i], "why": "forced"} for i in force_ids if i in by_id][:size]
    taken = {p["piece_id"] for p in picks}
    rest = [r for r in ordered if r["piece_id"] not in taken]
    avail = Counter(r["tenant"] for r in rest)
    alloc = shares(size - len(picks), dict(avail))
    for tenant in sorted(alloc):
        q = alloc[tenant]
        pool = [r for r in rest if r["tenant"] == tenant]
        anchors = _round_robin(_held_first([r for r in pool if r["label"]], seed), int(q * ANCHOR_SHARE + 0.5),
                               key=lambda r: r["destination"])
        got = [{**r, "why": "anchor"} for r in anchors]
        ids = {r["piece_id"] for r in got}
        others = _round_robin(_held_first([r for r in pool if r["piece_id"] not in ids and not r["label"]], seed),
                              q - len(got), key=lambda r: r["destination"])
        got += [{**r, "why": "spread"} for r in others]
        ids |= {r["piece_id"] for r in others}
        if len(got) < q:   # not enough unlabelled pieces: top up with more anchors
            got += [{**r, "why": "anchor"} for r in pool if r["piece_id"] not in ids][:q - len(got)]
        picks += got
    return picks


def strata(picks: list[dict]) -> dict:
    return {"tenant": dict(Counter(p["tenant"] for p in picks)),
            "status": dict(Counter(p["status"] for p in picks)),
            "route": dict(Counter("route" if (p["route_segment_count"] or 0) > 1 else "single" for p in picks)),
            "label": dict(Counter(str(p["label"]) for p in picks)),
            "destination": dict(Counter(p["destination"] for p in picks)),
            "why": dict(Counter(p["why"] for p in picks))}


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id, t.name AS tenant,
           cp.angle_gate_request_id::text AS request_id, r.atom_id, cp.status, cp.created_at,
           cp.route_segment_count, cp.content_text, cp.writer_input
      FROM acp_shared.content_piece cp
      JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
      JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
     WHERE cp.channel = 'blog' AND cp.status IN ('approved', 'held') AND cp.writer_input IS NOT NULL
       AND length(cp.content_text) > 0
"""
_PIECE_LABEL_SQL = "SELECT piece_id::text AS piece_id, label FROM acp_shared.content_piece_label"
_GOLDEN_SQL = """
    SELECT i.refs->>'piece_id' AS piece_id, i.label_verdict, i.label_source, i.sample_id, i.post_edit
      FROM shared.eval_golden_item i JOIN shared.eval_golden_set s ON s.set_id = i.set_id
     WHERE s.name = 'aa729' AND s.status = 'active' AND i.writer = 't9_blog' AND NOT i.trap
"""


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def to_row(r: dict, golden: dict, human: dict) -> Optional[dict]:
    """A selectable row, or None when the harness cannot rewrite the piece (no seed / no brand pack)."""
    wi = _json(r["writer_input"]) or {}
    if not isinstance(wi, dict) or not wi.get("seed") or not wi.get("brand_pack"):
        return None
    g = golden.get(r["piece_id"])
    if g and not g["post_edit"]:
        label = {"verdict": g["label_verdict"], "source": f"golden:{g['label_source']}", "sample_id": g["sample_id"]}
    elif human.get(r["piece_id"]):
        votes = Counter(human[r["piece_id"]])
        label = {"verdict": "pass" if votes["pass"] > votes["fail"] else "fail", "source": "content_piece_label"}
    else:
        label = None
    return {"piece_id": r["piece_id"], "tenant_id": r["tenant_id"], "tenant": r["tenant"],
            "request_id": r["request_id"], "atom_id": r["atom_id"], "status": r["status"],
            "created_at": str(r["created_at"]), "route_segment_count": r["route_segment_count"],
            "destination": str(wi.get("destination") or "unknown"), "seed_sha": sha(wi["seed"]),
            "writer_input_sha": sha(wi), "content_sha": sha(r["content_text"] or ""), "label": label}


def parse(argv):
    ap = argparse.ArgumentParser(description="AA-790 fixed blog sample (read-only, $0)")
    ap.add_argument("--size", type=int, default=int(os.environ.get("SIZE", DEFAULT_SIZE)))
    ap.add_argument("--seed", type=int, default=int(os.environ.get("SEED", 790)))
    ap.add_argument("--force-ids", default=os.environ.get("FORCE_IDS", ""))
    ap.add_argument("--version", default=os.environ.get("VERSION", "v1"))
    return ap.parse_args(argv)


async def main(argv=None) -> int:
    import asyncpg

    a = parse(sys.argv[1:] if argv is None else argv)
    force = tuple(x.strip().lower() for x in a.force_ids.split(",") if x.strip())
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await c.execute("SET default_transaction_read_only = on")
        assert (await c.fetchval("SHOW default_transaction_read_only")) == "on"
        golden = {x["piece_id"]: dict(x) for x in await c.fetch(_GOLDEN_SQL) if x["piece_id"]}
        human: dict[str, list[str]] = {}
        for x in await c.fetch(_PIECE_LABEL_SQL):
            v = PIECE_LABEL_VERDICT.get(x["label"])
            if v:
                human.setdefault(x["piece_id"], []).append(v)
        raw = [dict(r) for r in await c.fetch(_PIECES_SQL)]
    finally:
        await c.close()
    rows = [x for x in (to_row(r, golden, human) for r in raw) if x]
    kept, counts = distinct_inputs(rows)
    picks = select(kept, size=a.size, seed=a.seed, force_ids=force)
    missing_forced = [i for i in force if i not in {p["piece_id"] for p in picks}]
    manifest = {
        "name": "aa790-fixed-blog-sample", "version": a.version, "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": a.seed, "size_requested": a.size, "size": len(picks),
        "short_by": max(0, a.size - len(picks)),
        "pool": {"blog_pieces": len(raw), "rewritable": len(rows), **counts,
                 "labelled_inputs": sum(1 for r in kept if r["label"]),
                 "by_tenant": dict(Counter(r["tenant"] for r in kept))},
        "missing_forced": missing_forced, "strata": strata(picks),
        "pieces": [{k: p[k] for k in ("piece_id", "tenant", "tenant_id", "request_id", "atom_id", "destination",
                                      "status", "route_segment_count", "why", "label", "writer_input_sha",
                                      "content_sha", "seed_sha")} for p in picks],
    }
    print(json.dumps(manifest, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
