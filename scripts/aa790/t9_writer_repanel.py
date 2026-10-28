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
                    [--arm r8] [--angles stored|regen] [--t8-mode off|r8]

``--dry-run`` builds every prompt and prints its size, the lead fact and the stored text's census; no model call, $0.

AA-790 round 8 (S255): ``--angles regen`` first re-runs T8 for each distinct request of the selected pieces, with
the live job's own inputs (``services.acp_angle_gate.service.angle_inputs``: seed / Route digest, brand pack,
channel spec, trip, search demand + real questions, piece history), the brief mode ``--t8-mode`` (``off`` = the live
round-6 brief, ``r8`` = the round-8 brief contract) and the live ``t8_angle_gen`` model id, logged under
``adhoc_aa790``. The angles are ranked as live (``measurable_rank``; the Jev answer check is skipped: it only moves
the pick when Jev rejects a claimed PAA answer) and sanitised as live (``sanitize_briefs``); the recommended one
replaces the stored angle in every piece of that request. Nothing is written to the app tables (no
``angle_gate_option`` row). The three angles of each request, the pick, the round-8 brief census
(``r8_brief_problems``) and the T8 cost go to ``<out>.angles.json`` (+ ``<s3>.angles.json``). ``--arm`` is stored on
every row and in the ``shared.llm_call_log`` quality signal of every call (T8 and T9), so cost per arm is
``quality_signal->>'arm'``. With ``--dry-run`` the T8 step builds the angle prompts only and prints their size ($0).
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
# One T8 angle call is ~$0.03 (Sonnet, ~2k tokens out); a contract retry makes two. Reserved per request.
EST_T8_USD = 0.10

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


ANGLE_FIELDS = ("name", "why_it_works", "formula_fit", "best_final_style")


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


def writer_args(row: dict, index: int, angle_override: Optional[dict] = None) -> dict:
    """build_user_prompt's arguments from the stored writer_input (pure). ``angle_override`` (round 8, ``--angles
    regen``): the regenerated T8 angle used in place of the stored one."""
    from services.acp_angle_gate.channel_style import get_channel_style
    from services.acp_angle_gate.goals import get_goal
    from services.acp_content_writing import ai_tells

    wi = row["writer_input"]
    channel = row["channel"] or wi.get("channel")
    angle = {k: (angle_override or wi.get("angle") or {}).get(k) or "" for k in ANGLE_FIELDS}
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


def call_writer(row: dict, args: dict, model_tier: Optional[str], run_id: str, arm: str = "") -> dict:
    """One writer call through the gateway (AA-927 canonical pattern): the row is written once the reply is parsed;
    a reply that came back (billed) but cannot be parsed writes a billed_unusable row in the except branch and
    re-raises. A call that raises inside ``generate()`` is logged by the gateway itself. ``arm`` goes into the
    quality signal, so the cost per arm comes from ``quality_signal->>'arm'``."""
    from services.acp_content_writing import generate
    from services.acp_content_writing.prompts import SYSTEM_PROMPT, build_user_prompt
    from shared.llm_client.call_log import record_call_sync, record_unusable_sync
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    channel = args["channel_style"]["channel"]
    user = build_user_prompt(**args)
    signal = {"aa790_run": run_id, "piece": row["piece_id"], "channel": channel, "mode": "repanel_write",
              "lead_focus": bool(args.get("lead_focus")), "arm": arm}
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


def request_keys(rows: list[dict]) -> list[tuple[str, str]]:
    """(request_id, tenant_id) of the selected pieces, each request once, in first-piece order (pure)."""
    seen: dict[str, str] = {}
    for r in rows:
        seen.setdefault(r["request_id"], r["tenant_id"])
    return list(seen.items())


def chosen_angle(angles: list[dict], idx: int) -> dict:
    """The writer's angle: the four fields of the recommended option (pure)."""
    return {k: angles[idx].get(k) or "" for k in ANGLE_FIELDS}


def brief_census(angles: list[dict]) -> list[dict]:
    """Per angle: brief word count, figures and the round-8 contract problems ($0, pure) — the same check
    ``generate.r8_brief_problems`` the round-8 retry uses, so the control arm's briefs are measured on it too."""
    from services.acp_angle_gate.generate import r8_brief_problems
    out = []
    for a in angles:
        brief = a.get("best_final_style") or ""
        out.append({"words": len(brief.split()), "problems": r8_brief_problems(brief)})
    return out


async def regen_angles(rows: list[dict], *, mode: str, dry_run: bool, model_tier: Optional[str], run_id: str,
                       arm: str, max_usd: float) -> tuple[dict[str, dict], float]:
    """T8 again for each distinct request (see the module docstring). Returns ({request_id: result}, T8 spend).
    A request that fails keeps its stored angle (result has ``error``; the pieces are still written)."""
    import asyncpg

    from services.acp_angle_gate import generate as t8_generate
    from services.acp_angle_gate import service as t8
    from services.acp_angle_gate.goals import get_goal
    from services.acp_angle_gate.prompts import build_user_prompt as t8_user_prompt, system_prompt

    goal_of = {r["request_id"]: (r["writer_input"] or {}).get("goal") for r in rows}
    out: dict[str, dict] = {}
    spent = 0.0
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=2)
    try:
        for request_id, tenant_id in request_keys(rows):
            tid, rid = uuid.UUID(tenant_id), uuid.UUID(request_id)
            try:
                req = await t8._fetch_request_row(tid, rid, pool)
                goal = get_goal(req["goal"] or goal_of.get(request_id))
                if goal is None:
                    raise ValueError(f"unknown goal {req['goal']!r}")
                inputs = await t8.angle_inputs(tid, req, goal, pool)
                kwargs = inputs.generate_kwargs()
                if dry_run:
                    prompt_kwargs = {k: v for k, v in kwargs.items() if k != "moments"}
                    user = t8_user_prompt(**prompt_kwargs)
                    out[request_id] = {"dry": True, "t8_user_chars": len(user),
                                       "t8_system_chars": len(system_prompt(mode)), "route": bool(inputs.moments)}
                    print(f"AA790 t8dry {request_id[:8]} mode={mode} user_chars={len(user)} "
                          f"system_chars={len(system_prompt(mode))} route={bool(inputs.moments)}", flush=True)
                    continue
                if spent + EST_T8_USD > max_usd:
                    out[request_id] = {"error": "skipped_cost_cap"}
                    print(f"AA790 t8skip {request_id[:8]}: cap (${spent:.3f} spent)", flush=True)
                    continue
                angles, idx, _reason, cost = await t8_generate.generate_angles(
                    **kwargs, brief_mode=mode, stage=STAGE, model_tier=model_tier, pool=pool,
                    quality_signal_extra={"aa790_run": run_id, "arm": arm, "mode": "repanel_t8",
                                          "request": request_id})
                spent += float(cost or 0.0)
                if inputs.channel:
                    _evidence, idx = t8.measurable_rank(angles, inputs)
                t8.sanitize_briefs(angles, inputs.brand_audience, request_id=request_id)
                census = brief_census(angles)
                out[request_id] = {"angles": angles, "recommended_index": idx, "chosen": chosen_angle(angles, idx),
                                   "census": census, "cost_usd": round(float(cost or 0.0), 6), "mode": mode}
                print(f"AA790 t8 {request_id[:8]} mode={mode} ${float(cost or 0.0):.4f} t8_total=${spent:.3f} "
                      f"pick={idx} words={[c['words'] for c in census]} "
                      f"r8_problems={[len(c['problems']) for c in census]}", flush=True)
            except Exception as e:  # noqa: BLE001 — that request keeps its stored angle; the run goes on
                out[request_id] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
                print(f"AA790 t8fail {request_id[:8]} {out[request_id]['error']}", flush=True)
    finally:
        await pool.close()
    return out, spent


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
    ap.add_argument("--arm", default="", help="a label stored on every row and in the llm_call_log quality_signal "
                                              "(round 8: 'r8c' control, 'r8')")
    ap.add_argument("--angles", choices=("stored", "regen"), default="stored",
                    help="stored = the piece's stored T8 angle (rounds 1-7); regen = run T8 again per request")
    ap.add_argument("--t8-mode", choices=("off", "r8"), default="off",
                    help="T8 brief mode for --angles regen: off = live round-6 brief, r8 = round-8 brief contract")
    a = ap.parse_args()
    if a.angles == "stored" and a.t8_mode != "off":
        ap.error("--t8-mode needs --angles regen")

    from services.acp_content_writing import ai_tells
    from services.acp_content_writing.service import WRITER_PROMPT_VERSION

    ids = [i.strip() for i in a.ids.split(",") if i.strip()]
    rows = asyncio.run(load_rows(ids))
    missing = sorted(set(ids) - {r["piece_id"] for r in rows})
    print(f"AA790 rows={len(rows)} missing={missing} writer_prompt_version={WRITER_PROMPT_VERSION}", flush=True)
    model_tier = None if a.dry_run else stage_model()
    print(f"AA790 model_tier={model_tier} stage={STAGE} cap=${a.max_usd} arm={a.arm or '-'} angles={a.angles} "
          f"t8_mode={a.t8_mode}", flush=True)
    index = write_indexes(rows)
    run_id = uuid.uuid4().hex[:12]
    spent, out = 0.0, []
    regen: dict[str, dict] = {}
    if a.angles == "regen":
        t8_tier = None if a.dry_run else stage_model("t8_angle_gen")
        print(f"AA790 t8_model_tier={t8_tier} requests={len(request_keys(rows))}", flush=True)
        regen, spent = asyncio.run(regen_angles(rows, mode=a.t8_mode, dry_run=a.dry_run, model_tier=t8_tier,
                                                run_id=run_id, arm=a.arm, max_usd=a.max_usd))
    for r in rows:
        got = regen.get(r["request_id"]) or {}
        try:
            args = writer_args(r, index[r["piece_id"]], got.get("chosen"))
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
               "lead_focus": args.get("lead_focus"), "writer_prompt_version": WRITER_PROMPT_VERSION, "arm": a.arm,
               "angle_source": "regen" if got.get("chosen") else "stored", "t8_mode": a.t8_mode,
               "angle_used": {k: args["angle"].get(k) for k in ANGLE_FIELDS}}
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
            w = call_writer(r, args, model_tier, run_id, a.arm)
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
    if regen:
        with open(a.out + ".angles.json", "w", encoding="utf-8") as f:
            json.dump({"arm": a.arm, "t8_mode": a.t8_mode, "run": run_id, "requests": regen}, f, ensure_ascii=False,
                      indent=1, default=str)
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
        if regen:
            boto3.client("s3").upload_file(a.out + ".angles.json", bucket, key + ".angles.json")
        print(f"AA790 uploaded {a.s3}", flush=True)
    print(f"AA790 wrote {a.out} rows={len(out)} spent=${spent:.3f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
