# Live writing progress: Redis snapshot + short poll, not SSE (AA-637)

## Context

Tenant writing runs as background jobs, because a single request cannot survive API Gateway
REST's ~29s integration timeout:
- T2 tour rewrite: `trigger_rewrite` → `_rewrite_tour` LangGraph → T3 QA gate.
- T9 social post: `run_write_background`, the AA-466 202 + poll pattern.

Until now the UI showed only a pulsing "Writing…" until the whole result landed, typically
15–90s. Nghiệp wants a Claude-like experience instead: visible steps plus text appearing as the
model writes it.

The LLM calls were already half-way there:
- `LLMClient._call_bedrock` (acc2 native) already used `invoke_model_with_response_stream`
  (AA-224), but only accumulated the text.
- `bedrock_satellite.invoke_claude` (acc3/acc1) — the path essentially all real calls take,
  because acc2 native is blocked for Anthropic models — used non-streaming `invoke_model`.

## Decision

1. **The progress channel is a side channel, bound per job with a `ContextVar` sink.**
   - `shared/llm_client/stream_sink.py` holds the current sink.
   - `LLMClient.generate()` reports call start and end.
   - Every provider attempt reports a restart, which drops text from a provider that failed
     mid-stream.
   - Text deltas are forwarded only for stages the sink asks for (`sink.wants(stage)`).
   - `invoke_claude` switches to `invoke_model_with_response_stream` **only when** it is given an
     `on_delta` callback, so every other call site keeps the exact old request type.
   - `asyncio.to_thread` and LangGraph's `run_in_executor` both copy the context, so the sink
     reaches the worker threads that make the calls.
2. **Only writer stages stream text:** `t9_write`, `t2_generate` and `s1_generate`. Judge, brand
   audit and fix calls surface as steps, never as text.
3. **State is stored as a Redis snapshot, and the browser polls it.**
   - The tracker keeps state in memory under a lock, because deltas arrive on worker threads.
   - An asyncio flusher writes a JSON snapshot to `wp:{tenant_id}:{kind}:{job_id}` about every
     0.4s, with a 1h TTL.
   - `GET /v1/progress/{kind}/{job_id}` builds the key from the **JWT tenant id**, so one tenant
     cannot read another tenant's job.
   - The browser polls about every 0.8s and types out the new text locally.
4. **The server shapes what is displayed; the client never sees raw model output.**
   - The blog envelope and the tour JSON are parsed from partial JSON into labelled sections.
   - Citation tags `[R:…]`/`[F:…]` are stripped, and any trailing half-written tag is held back.
   - For the other channels, everything after the `===SUMMARY===` marker is hidden.
5. **Persistence is unchanged.** The progress view is best-effort. A Redis failure is logged and
   swallowed, and never affects the job or the rows it writes.

## Alternatives considered

- **True SSE / streaming response through API Gateway.** It needs API Gateway REST response
  streaming, Terraform changes and reconnect handling. It also ties the view to one HTTP
  connection, so a reload or tab switch loses it. Deferred. Revisit only if about 1s latency
  proves not good enough.
- **In-process memory store.** Rejected, because the ECS service can scale above one task and
  every deploy replaces the task.
- **Polling the existing DB rows.** Rejected: nothing is written to the database until the job
  ends, and writing partial text to the database would change persistence semantics.

## Consequences

- No LLM cost change: the same calls are made, streamed instead of buffered.
- Satellite calls from a tenant writing job now use `InvokeModelWithResponseStream`. The acc3 and
  acc1 roles already allow it (see `get_satellite_client` docstring).
- Adding a new streamed stage means listing it in the tracker's `stream_stages`.
