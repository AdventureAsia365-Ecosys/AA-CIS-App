# AA-1042 — Route control preview: "first" is the deployment that would serve (S268 follow-up)

Live Dev (S268): the Route control preview and the $0 route check (`scripts/llm_ops/route_chain_check.py`) named
`haiku-5-5@acc3` first for `a0_column_map` while the probe (CIS 367 `llm_model_deployment.access_state`) and the Stage
coverage card had that deployment refused (`access_denied`). Its breaker circuit was half-open, so the gateway's plan
(`vendor_route.deployment_plan`) still held it, and the resolver reported the first deployment that would be CALLED.

## Decisions

- **"first" = the first model@connection that would SERVE the stage.** `llm_route_chains.resolve_stage(cfg, evidence)`
  takes the refusals on record (`stage_coverage.RefusalEvidence`, read once per preview / check) and skips a planned
  deployment the account refuses. Disabled deployments, switched-off connections, role / scope skips, open circuits and
  budget stops were already left out by the gateway's own plan; that part is unchanged.
- **One refusal rule.** The rule of the Stage coverage card moved out of `stage_coverage.assess` into
  `stage_coverage.refusal(dep, conn, health, now)` (pure, behaviour unchanged): the probe state, the breaker's refusing
  classes (access_denied / quota_exhausted / credential) newer than the newest answer, and the connection's access
  state (unconfigured; quota_exhausted without a recent answer; access_denied with no answer ever). The card and the
  preview call the same function, so they cannot disagree again.
- **Show what was skipped.** Each stage of the preview carries `skipped` ([{model, connection, reason}] for every
  model@connection before `first`: catalog off, a plan skip, refused on record). An attempt keeps `deployments` (the
  plan) and lists its refused ones in `refused`; an attempt whose every deployment is refused has status `refused`.
  `called_first` keeps the old answer (the first deployment the gateway would call). The Route control tab shows
  "Skipped: model@connection (reason)" under "Serves first" (kit only, English + Vietnamese copy).
- **The gateway is not changed.** It still tries a refused deployment when its circuit is half-open (one call per
  breaker period, at most 30 minutes for access_denied: the breaker's own recovery check), gets the refusal and moves
  on. The request is then served by the next deployment, which is what the preview names.

## Changed

- `shared/llm_client/stage_coverage.py`: `refusal`, `RefusalEvidence`, `load_evidence` (three reads: deployments with
  the probe state, LLM connections, breaker rows; the pre-367 fallback without the probe state).
- `api/services/llm_route_chains.py`: `resolve_stage(cfg, evidence=None)`; without evidence the old answer stays.
- `api/services/llm_route_control.py`: `_world` reads the evidence with the rest; now and the proposal resolve with the
  same evidence; the stage output adds `skipped` and the attempts' `refused`.
- `scripts/llm_ops/route_chain_check.py`: reads the evidence (one asyncpg connection), prints refused deployments.
- Frontend: `lib/routeControl.ts` (`skipped`, `skippedWhere`), `RouteControlTab.tsx`, `routeControl.messages.ts`.

## Tradeoffs

- Skipping refused deployments in the gateway too (no wasted refused call) was weighed and left out: it would replace
  the breaker's 30-minute half-open recovery with the daily probe (up to a day before a restored model is used again),
  which is a routing behaviour change for a preview bug. The preview predicts the serving deployment instead.
- The preview is a prediction from evidence: when access comes back and nothing has answered or been probed since, the
  preview still skips the deployment until one call answers (breaker `last_ok_at`) or the next probe writes `ok`.

## Should know

- Tests: `tests/unit/test_aa1042_preview_serving.py` runs the preview resolver AND `LLMClient.generate` on the same
  fixtures (the AA-1026 route snapshot after 366 and the seed registry of `aa1002_s3_route_capture.py`): a refused
  deployment answers AccessDenied, the next answers; the deployment that served equals the preview's `first`, and the
  gateway's first call equals `called_first`. `tests/integration/test_aa1042_route_control_db.py` marks a real
  deployment row refused and checks the preview moves `first` and lists it in `skipped`.
- No migration, no route shape change in OpenAPI (the preview answers a plain dict).
