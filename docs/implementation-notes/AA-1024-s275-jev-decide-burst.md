# AA-1024 (S275 lane jev-decide-burst) — Jev under a recompute burst

Gateway hardening side fix. Evidence (Dev, 09/11 18:07Z): one platform recompute sent more than 240 Jev asks a minute
(stage `a3_demand`); 20 asks failed with `ConnectTimeout` after 3 tries at 3 s; the shared provider circuit opened
and refused 4,997 asks. Each refusal was logged `status=refused`, `error_class=other` and counted as an error by the
AI providers Jev health and the root `tools/llm/gateway_status.py`. Later recomputes answered every subject (no data
lost).

## Decisions

- **F2, one keep-alive client per event loop.** `_call_jev` used `async with httpx.AsyncClient(...)` per ask: a new
  TCP + TLS connection for each of ~100k asks a day. A client belongs to the loop whose transports it pools, so the
  registry is keyed by the running loop (`id(loop)` plus an identity check of the loop object). The api / worker loops
  keep one client each; a job body that runs in its own `asyncio.run` (recompute, a3 atomize, `decide_sync`) gets
  its own.
- **Closing without touching every caller.** A small async generator started in the loop holds the client.
  `asyncio.run` finalizes every async generator of its loop (`shutdown_asyncgens`) before it closes the loop, which
  runs the generator's `finally` and closes the client. A loop closed another way (no `shutdown_asyncgens`) is
  dropped from the registry the next time a client is made. `close_jev_client()` closes this loop's client on
  demand (unused by the app today; tests use it).
- **Timeout per request.** The connection row's `timeout_s` is passed on each `post` (`timeout=`), the same
  `httpx.Timeout(timeout_s)` for every phase as before, so an admin edit still applies within `_CONFIG_TTL_S`.
  No connect-timeout change (keep behaviour identical); raising the timeout stays the admin's F1 call.
- **F4, status-based class.** `jev_failure_class(exc)`: `classify()` first (text patterns); when it says `other` and
  the exception carries `response.status_code`, `provider_circuit.failure_class(status)` decides (401 / 403
  access_denied, 402 quota_exhausted, 429 throttled); a content 4xx (`None` there), a 5xx and a 404 stay `other`.
  The access state and the refused-vs-error status follow the class as before.
- **F5, no migration.** `llm_call_log.error_class` has a CHECK (access_denied, quota_exhausted, throttled,
  credential, other, deadline, budget, truncated, disabled, role_not_served): no circuit class. The row keeps the
  class that opened the circuit (already written since B3) and now also carries `quality_signal.refusal =
  circuit_open` (`provider_circuit.refusal_signal()`). The CIS LLM stats already count `status='refused'` apart
  from errors; the Jev health (`DECISIONS_SQL`, decision_log, which has no status column) now uses the Calls tab's
  `JEV_STATUS_SQL`, so gateway refusals land in a new `refused` count and only provider failures in `errors`.
- **F6 not done.** `decide()` does not read `llm_provider_connection.max_concurrency`, and the PATCH does not let an
  admin edit it on a provider row (`PROVIDER_EDITABLE`), so a cap would be code plus an admin change; the lane said
  "only if already supported". The shared client's default pool (100 connections) is not a cap either: a lower
  `max_connections` would turn waiting asks into `PoolTimeout` failures at 3 s.

## Changed

- `shared/llm_client/decide.py`: per-loop client registry (`_new_jev_client`, `_hold_jev_client`, `_jev_client`,
  `close_jev_client`), `_call_jev` on it; `_http_status`, `jev_failure_class`; the circuit-open refusal row carries
  the marker (`failed_ask` takes optional extra quality_signal keys).
- `shared/provider_circuit.py`: `REFUSAL_SIGNAL_KEY`, `REFUSAL_CIRCUIT_OPEN`, `refusal_signal()`.
- `api/services/ai_gateway.py`: the Jev refused-text constants, `jev_verdict_status` and `JEV_STATUS_SQL` moved above
  `DECISIONS_SQL` (unchanged); `DECISIONS_SQL` counts `errors` / `refused` by `JEV_STATUS_SQL`; the Jev row's
  `health` has `refused`.
- Tests: `tests/unit/test_aa1024_s275_jev_burst.py` (new), `test_aa804_ai_gateway_api.py` (health shape),
  `tests/integration/test_aa804_ai_gateway_sql.py` (DECISIONS_SQL on Postgres).
- `CONTEXT.md`: Jev transport + classification, the circuit-open marker.

## Tradeoffs

- The async-generator finalizer is a little indirect, but it closes the client in every `asyncio.run` site
  (recompute, a3 atomize, `decide_sync`, scripts) without each one knowing about Jev. The alternative, closing in
  each job body, would leave any new caller leaking a client per run.
- Old circuit refusals (before this deploy) carry no marker; their `decision_log` error text already ends with
  "nothing was sent to the provider", so the CIS Jev health reads them right at once. The root tool matches the
  same text for old rows.
- `error_pct` of the Jev health drops (refusals no longer count), so `most_calls_failed` (degraded) now means real
  provider failures; an open circuit is still shown through the row's own `circuit_open` status (`with_circuit`).

## Should know

- Keep-alive expiry is httpx's default (5 s): at burst rates connections are reused; an idle connection the server
  closed surfaces as a `TransportError` and is retried by the existing loop.
- The live check after deploy: the next recompute's Jev rows show no `ConnectTimeout` burst; re-run the S275
  diagnostic (`a3demand_diag.py HOURS=6`) and the root gateway status.
