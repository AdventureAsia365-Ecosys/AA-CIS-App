"""AA-1113 (root ADR 0009 decision 10) — $0 live probe of the gateway's free-text PII scrub. Calls NO model.

Runs inside the CIS api / worker container (the S3-mediated ECS exec runner puts /app on sys.path), as the
container's DATABASE_URL (cis_app). It sends one request through ``LLMClient.generate`` on a free-text stage
(``t9_write`` by default) with an invented e-mail address and phone number planted in the prompt, and a model key the
catalog does not hold (``zz-aa1113-probe-no-model``): the gateway runs its gates and the scrub, then refuses the key
before any provider attempt, so nothing is sent and nothing is billed. It then reads the gateway's 0 USD ledger row of
that request (``shared.llm_call_log``, matched by ``quality_signal.route_trace``) and checks:

  * ``quality_signal.pii_scrub.status`` is ``scrubbed`` with one ``[email]`` and one ``[phone]`` redacted;
  * ``quality_signal.pii_scrub.scrubbed_hash`` equals the sha256 of the scrubbed request computed here
    (``free_text_scrub.scrubbed_hash``): the text the model would have got holds neither planted value;
  * the planted values appear nowhere in the row; ``cost_usd`` is 0.

    python3 aa1113_scrub_probe.py [--stage t9_write]

Output markers: ``--- AA1113 SCRUB PROBE ---`` / ``--- END ---`` around one JSON object; exit 0 when every check holds,
1 otherwise. Writes exactly one ledger row (the refusal), like any refused call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

PLANTED_EMAIL = "probe.guest@example.test"
PLANTED_PHONE = "+84 900 000 111"
PROBE_MODEL = "zz-aa1113-probe-no-model"
PROBE_MAX_TOKENS = 16  # never sent: the unknown key is refused before any provider attempt


def _request(stage: str):
    from shared.llm_client.models import LLMRequest
    return LLMRequest(system_prompt="AA-1113 probe: reply with one word.",
                      user_prompt=f"Guest note: write to {PLANTED_EMAIL} or call {PLANTED_PHONE}.",
                      stage=stage, model_tier=PROBE_MODEL, max_tokens=PROBE_MAX_TOKENS)


async def _row(trace: str) -> dict | None:
    import asyncpg

    from shared.secrets import get_database_url
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        r = await conn.fetchrow(
            "SELECT stage, model, status, error_class, cost_usd, quality_signal::text AS qs "
            "FROM shared.llm_call_log WHERE created_at > now() - interval '15 minutes' "
            "AND quality_signal ->> 'route_trace' = $1 ORDER BY created_at DESC LIMIT 1", trace)
        return dict(r) if r else None
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", default="t9_write")
    args = ap.parse_args(argv)

    from shared.llm_client import call_log, free_text_scrub
    from shared.llm_client.client import LLMClient

    req = _request(args.stage)
    expected = free_text_scrub.scrubbed_hash(free_text_scrub.scrub_request(req)[0])
    captured: dict = {}
    original = call_log.record_calls

    async def spy(rows):
        for r in rows:
            trace = (r.get("quality_signal") or {}).get("route_trace")
            if r.get("model") == PROBE_MODEL and trace:
                captured["trace"] = trace
        return await original(rows)
    call_log.record_calls = spy
    error = None
    try:
        LLMClient().generate(req)
    except Exception as e:  # noqa: BLE001 — the refusal is the expected outcome
        error = type(e).__name__
    finally:
        call_log.wait_for_pending_writes()
        call_log.record_calls = original
    row = None
    for _ in range(5):
        if captured.get("trace"):
            row = asyncio.run(_row(captured["trace"]))
        if row:
            break
        time.sleep(1)
    qs = json.loads(row["qs"]) if row else {}
    receipt = qs.get("pii_scrub") or {}
    flat = json.dumps(row, default=str) if row else ""
    checks = {
        "refused_before_any_attempt": error is not None,
        "ledger_row_found": row is not None,
        "scrubbed": (receipt.get("status") == "scrubbed"
                     and receipt.get("redacted", {}).get("[email]") == 1
                     and receipt.get("redacted", {}).get("[phone]") == 1),
        "scrubbed_hash_matches": receipt.get("scrubbed_hash") == expected,
        "planted_values_absent": bool(row) and PLANTED_EMAIL not in flat and "900 000 111" not in flat,
        "zero_cost": bool(row) and float(row.get("cost_usd") or 0) == 0.0,
    }
    out = {"stage": args.stage, "model": PROBE_MODEL, "error": error, "expected_hash": expected,
           "row": {k: row[k] for k in ("stage", "model", "status", "error_class", "cost_usd")} if row else None,
           "pii_scrub": receipt, "checks": checks, "ok": all(checks.values())}
    print("--- AA1113 SCRUB PROBE ---")
    print(json.dumps(out, default=str, indent=2))
    print("--- END ---")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
