"""AA-790 (S251) — re-write the S251 T9 pilot inputs with the new writer brief + the writer-side AI-tell pass, for
the re-panel against AI-tells 4.08 / brand voice 6.53. PAID (one T9 writer call per piece; ~$0.03 a LinkedIn post,
~$0.08 a blog; 20 pieces ≈ $1.0–1.4). Hard cap --max-usd (default 2.0).

Runs in the CIS api container AFTER the AA-790 writer PR is deployed (it imports the deployed ``services``: the new
SYSTEM_PROMPT, ``ai_tells``), uploaded alone (the image has no ``scripts/`` folder) and started detached with the
root runbook's ``run_bg.sh`` (20 sequential calls take ~10–15 min; a foreground ECS exec drops). Reads with the
container ``DATABASE_URL`` (role cis_app): SELECT on ``acp_shared.content_piece`` (+ its request and tenant) only.
Writes nothing to the app tables: one ``shared.llm_call_log`` row per call (stage ``adhoc_aa790``, role writer),
and the JSON result to ``--out`` (+ ``--s3``).

Per piece it rebuilds the writer prompt from the piece's stored ``writer_input`` (AA-769 / AA-916 snapshot: seed,
route moments, Facts, scope, brand pack, angle, goal, channel, keyword, CTA, trip), calls the writer on the SAME model
route as stage ``t9_write`` (its model id, passed as ``model_tier``; logged under ``adhoc_aa790``), then runs the
deterministic steps the T9 loop runs before the gates: ``ai_tells.rewrite``, ``hygiene.autofix``,
``hygiene.ensure_cta``, ``text_integrity.final_pass``; citation tags stripped as the tenant sees the text. No gate,
no FAQ regeneration, no sentence repair, no judge: the re-panel scores the writer + the writer-side pass. The
non-blog pieces of one request get the lead-fact rotation by their order in that request (index 0, 1, 2, ...), as the
live T9 job now does.

Output rows have the pilot row shape (``id``, ``piece_id``, ``tenant``, ``channel``, ``status``, ``text``, ``cta``,
``brand_pack``, ``custom_validators``, ``label``, ``source``) plus ``before_text`` (the stored pilot text),
``ai_tells_before`` / ``ai_tells_writer`` / ``ai_tells_after`` (``ai_tells.census``), ``pass_log``, ``lead_focus``,
``cost_usd``, ``model``, ``stop_reason``, ``writer_prompt_version``. The rows quote tenant piece text: keep them in the
private root ``.tmp-session/``, never in this repo.

  python3 /tmp/s.py --ids <id,id,...> [--dry-run] [--max-usd 2.0] [--out /tmp/aa790-repanel.json] [--s3 s3://b/k]

``--dry-run`` builds every prompt and prints its size, the lead fact and the stored text's census; no model call, $0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from typing import Optional

STAGE = "adhoc_aa790"
# Spend reserved before a call, so the cap holds even if the last call is a long blog.
EST_USD = {"blog": 0.12}
EST_USD_DEFAULT = 0.04

PIECES_SQL = """
    SELECT cp.piece_id::text AS piece_id, cp.tenant_id::text AS tenant_id,
           cp.angle_gate_request_id::text AS request_id, cp.channel, cp.status, cp.created_at,
           cp.content_text, cp.writer_input, r.atom_id, t.name AS tenant
    FROM acp_shared.content_piece cp
    JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
    JOIN shared.tenants t ON t.tenant_id = cp.tenant_id
    WHERE cp.piece_id = ANY($1::uuid[])
    ORDER BY cp.created_at, cp.piece_id
"""


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


async def load_rows(ids: list[str]) -> list[dict]:
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        rows = await conn.fetch(PIECES_SQL, [uuid.UUID(i) for i in ids])
    finally:
        await conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["writer_input"] = _json(d["writer_input"]) or {}
        d["created_at"] = str(d["created_at"])
        out.append(d)
    return out


def write_indexes(rows: list[dict]) -> dict[str, int]:
    """piece_id → its index among the selected pieces of its request (created_at order), the live rotation's input."""
    seen: dict[str, int] = {}
    out = {}
    for r in rows:
        k = r["request_id"]
        out[r["piece_id"]] = seen.get(k, 0)
        seen[k] = seen.get(k, 0) + 1
    return out


def writer_args(row: dict, index: int) -> dict:
    """build_user_prompt's arguments from the stored writer_input (pure)."""
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal
    from services.acp_content_writing import ai_tells

    wi = row["writer_input"]
    channel = row["channel"] or wi.get("channel")
    angle = {k: (wi.get("angle") or {}).get(k) or "" for k in ("name", "why_it_works", "formula_fit",
                                                                "best_final_style")}
    route = [(m.get("atom_id"), m.get("seed")) for m in wi.get("route_moments") or [] if isinstance(m, dict)]
    search = [q.get("question") for q in wi.get("search_questions") or []
              if isinstance(q, dict) and q.get("question")] if channel == "blog" else []
    lead = ai_tells.lead_focus(wi.get("seed"), index) if channel != "blog" else None
    args = {
        "content_seed": wi.get("seed") or "", "goal": get_goal(wi.get("goal")),
        "channel_style": get_channel_style(channel), "brand_audience": wi.get("brand_pack") or {}, "angle": angle,
        "cta": wi.get("cta") or "", "destination": wi.get("destination"),
        "trip_name": wi.get("writer_trip") or wi.get("trip"), "atom_id": row.get("atom_id"),
        "route_segments": route or None, "keyword": wi.get("keyword"), "facts_text": wi.get("facts") or None,
        "scope": wi.get("scope"),
    }
    if search:
        args["search_questions"] = search
    if lead:
        args["lead_focus"] = lead
    return args


def _contexts(row: dict, args: dict):
    from services.acp_content_writing import ai_tells, hygiene
    from services.acp_shared.channel_spec import channel_spec
    from services.acp_shared.signature_vocabulary import signature_words

    wi = row["writer_input"]
    pack = args["brand_audience"]
    channel = args["channel_style"]["channel"]
    spec = channel_spec(channel, pack)
    hyg = hygiene.HygieneContext(seed=args["content_seed"], scope=args["scope"], trip_name=wi.get("trip"),
                                 brand_name=pack.get("brand_name"), cta=args["cta"],
                                 hashtags_max=spec.hashtags_max if spec else None,
                                 first_line_max=spec.first_line_max if spec else None,
                                 audience=pack.get("customer_segment"), facts=args["facts_text"] or "",
                                 trip_places=tuple(wi.get("trip_places") or ()))
    ignore = hygiene.brand_words(pack.get("brand_name"))
    tell = ai_tells.TellContext(cta=args["cta"], angle_name=args["angle"].get("name"), source=hyg.source_text,
                                ignore=ignore, keep_words=frozenset(hygiene.fold(w) for w in signature_words(pack)),
                                angle_text=ai_tells.angle_text(args["angle"]))
    return hyg, tell


def finish(text: str, row: dict, args: dict) -> tuple[str, list[str], dict]:
    """The deterministic steps the T9 loop runs before the gates.

    Returns (tenant-view text, pass log, writer census)."""
    from services.acp_content_writing import ai_tells, hygiene, text_integrity
    from services.acp_content_writing.quality_gates import strip_citation_tags

    hyg, tell = _contexts(row, args)
    writer_census = ai_tells.census(strip_citation_tags(text), tell)
    text, log = ai_tells.rewrite(text, tell)
    text, _ = hygiene.autofix(text, hyg)
    text, _ = hygiene.ensure_cta(text, args["cta"])
    text, _, _ = text_integrity.final_pass(text, hyg)
    return strip_citation_tags(text), log, writer_census


def call_writer(row: dict, args: dict, model_tier: Optional[str], run_id: str) -> dict:
    """One writer call through the gateway (AA-927 canonical pattern): the row is written once the reply is parsed;
    a reply that came back (billed) but cannot be parsed writes a billed_unusable row in the except branch and
    re-raises. A call that raises inside ``generate()`` is logged by the gateway itself."""
    from services.acp_content_writing import generate
    from services.acp_content_writing.prompts import SYSTEM_PROMPT, build_user_prompt
    from shared.llm_client.call_log import record_call_sync, record_unusable_sync
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    channel = args["channel_style"]["channel"]
    user = build_user_prompt(**args)
    signal = {"aa790_run": run_id, "piece": row["piece_id"], "channel": channel, "mode": "repanel_write",
              "lead_focus": bool(args.get("lead_focus"))}
    resp = LLMClient().generate(LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=user, stage=STAGE,
                                           model_tier=model_tier, max_tokens=generate.max_tokens_for(channel)))
    try:
        if channel == "blog":
            body, _seo, _summary = generate._parse_blog_envelope(resp.content)
        else:
            body, _summary = generate._extract_summary(generate._strip_fences(resp.content))
            if channel in generate.PLAIN_TEXT_CHANNELS:
                body = generate.strip_markdown(body)
        if not (body or "").strip():
            raise ValueError("empty writer reply")
    except Exception as exc:
        record_unusable_sync(resp, stage=STAGE, role="writer", error=exc, quality_signal_extra=signal)
        raise
    record_call_sync(
        stage=STAGE, role="writer", model=resp.model_used, tokens_in=getattr(resp, "input_tokens", None),
        tokens_out=getattr(resp, "output_tokens", None), cost_usd=resp.cost_usd, quality_signal=signal,
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
        provider=resp.provider if isinstance(getattr(resp, "provider", None), str) else None)
    return {"text": body, "cost_usd": float(resp.cost_usd or 0.0), "model": resp.model_used,
            "stop_reason": getattr(resp, "stop_reason", None)}


def stage_model(stage: str = "t9_write") -> Optional[str]:
    from shared.llm_client.role_config import get_stage_config_sync
    cfg = get_stage_config_sync(stage)
    return getattr(cfg, "model_id", None) if cfg else None


def main() -> int:
    ap = argparse.ArgumentParser(description="AA-790 T9 writer re-panel rewrite (PAID unless --dry-run)")
    ap.add_argument("--ids", required=True, help="comma-separated content_piece ids (the S251 pilot pieces)")
    ap.add_argument("--dry-run", action="store_true", help="build the prompts only; no model call ($0)")
    ap.add_argument("--max-usd", type=float, default=2.0)
    ap.add_argument("--out", default="/tmp/aa790-repanel.json")
    ap.add_argument("--s3", help="also upload the JSON to this s3://bucket/key")
    a = ap.parse_args()

    from services.acp_content_writing import ai_tells
    from services.acp_content_writing.service import WRITER_PROMPT_VERSION

    ids = [i.strip() for i in a.ids.split(",") if i.strip()]
    rows = asyncio.run(load_rows(ids))
    missing = sorted(set(ids) - {r["piece_id"] for r in rows})
    print(f"AA790 rows={len(rows)} missing={missing} writer_prompt_version={WRITER_PROMPT_VERSION}", flush=True)
    model_tier = None if a.dry_run else stage_model()
    print(f"AA790 model_tier={model_tier} stage={STAGE} cap=${a.max_usd}", flush=True)
    index = write_indexes(rows)
    run_id = uuid.uuid4().hex[:12]
    spent, out = 0.0, []
    for r in rows:
        try:
            args = writer_args(r, index[r["piece_id"]])
            if args["goal"] is None or args["channel_style"] is None:
                raise ValueError("writer_input has an unknown goal or channel")
        except Exception as e:  # noqa: BLE001 — a piece without a usable writer_input is reported, not fatal
            print(f"AA790 bad_input {r['piece_id'][:8]} {type(e).__name__}: {str(e)[:200]}", flush=True)
            out.append({"id": r["piece_id"][:8], "piece_id": r["piece_id"], "error": f"bad_input: {e}"})
            continue
        channel = args["channel_style"]["channel"]
        _, tell = _contexts(r, args)
        rec = {"id": r["piece_id"][:8], "piece_id": r["piece_id"], "tenant": r["tenant"], "channel": channel,
               "status": "rewrite", "cta": args["cta"], "brand_pack": args["brand_audience"], "custom_validators": {},
               "label": None, "source": STAGE, "before_text": r["content_text"],
               "ai_tells_before": ai_tells.census(r["content_text"] or "", tell),
               "lead_focus": args.get("lead_focus"), "writer_prompt_version": WRITER_PROMPT_VERSION}
        if a.dry_run:
            from services.acp_content_writing.prompts import build_user_prompt
            rec["prompt_chars"] = len(build_user_prompt(**args))
            print(f"AA790 dry {rec['id']} {channel} prompt_chars={rec['prompt_chars']} "
                  f"lead={bool(rec['lead_focus'])} before={rec['ai_tells_before']['fixable_total']}"
                  f"+{rec['ai_tells_before']['residue']}", flush=True)
            out.append(rec)
            continue
        est = EST_USD.get(channel, EST_USD_DEFAULT)
        if spent + est > a.max_usd:
            print(f"AA790 skip {rec['id']}: cap (${spent:.3f} spent + ${est} > ${a.max_usd})", flush=True)
            rec["error"] = "skipped_cost_cap"
            out.append(rec)
            continue
        t0 = time.monotonic()
        try:
            w = call_writer(r, args, model_tier, run_id)
        except Exception as e:  # noqa: BLE001 — recorded per piece, the run goes on
            spent += est  # a billed failure is logged by the gateway; reserve its estimate against the cap
            rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            print(f"AA790 fail {rec['id']} {rec['error']}", flush=True)
            out.append(rec)
            continue
        spent += w["cost_usd"]
        text, log, writer_census = finish(w["text"], r, args)
        rec.update(text=text, pass_log=log, ai_tells_writer=writer_census, ai_tells_after=ai_tells.census(text, tell),
                   cost_usd=round(w["cost_usd"], 6), model=w["model"], stop_reason=w["stop_reason"],
                   secs=round(time.monotonic() - t0, 1))
        print(f"AA790 ok {rec['id']} {channel} ${w['cost_usd']:.4f} total=${spent:.3f} "
              f"tells before={rec['ai_tells_before']['fixable_total']}+{rec['ai_tells_before']['residue']} "
              f"writer={writer_census['fixable_total']}+{writer_census['residue']} "
              f"after={rec['ai_tells_after']['fixable_total']}+{rec['ai_tells_after']['residue']} "
              f"pass_edits={len(log)}", flush=True)
        out.append(rec)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    done = [x for x in out if x.get("text")]
    if done:
        mean = lambda k, f: round(sum(x[k][f] for x in done) / len(done), 2)  # noqa: E731
        print(f"AA790 summary n={len(done)} cost=${spent:.3f} "
              f"fixable before={mean('ai_tells_before', 'fixable_total')} "
              f"writer={mean('ai_tells_writer', 'fixable_total')} after={mean('ai_tells_after', 'fixable_total')} "
              f"residue before={mean('ai_tells_before', 'residue')} after={mean('ai_tells_after', 'residue')} "
              f"mean_words before={mean('ai_tells_before', 'mean_words')} after={mean('ai_tells_after', 'mean_words')}",
              flush=True)
    if a.s3:
        import boto3
        bucket, _, key = a.s3[len("s3://"):].partition("/")
        boto3.client("s3").upload_file(a.out, bucket, key)
        print(f"AA790 uploaded {a.s3}", flush=True)
    print(f"AA790 wrote {a.out} rows={len(out)} spent=${spent:.3f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
