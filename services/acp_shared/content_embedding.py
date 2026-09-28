"""
services.acp_shared.content_embedding — AA-499 (AA-494 Decision 5), the shared embedding
mechanism migration 124's own column comment describes: "embedding of this piece's full text,
for within-tenant/cross-tenant similarity checks (shared mechanism, two call sites)."

Model: Cohere Embed v4 (`us.cohere.embed-v4:0`, cross-region inference profile — the model
itself, `cohere.embed-v4:0`, requires INFERENCE_PROFILE inference type, confirmed via
`aws bedrock get-foundation-model`), output dimension 1536 requested explicitly
(`output_dimension` — Cohere v4 supports 256/512/1024/1536, chosen to match `content_piece.
content_embedding vector(1536)`, migration 124, with zero schema change needed).

**Real, live finding that overrides migration 041/124's own stated assumption**: those migrations'
comments say "vector(1536) = Bedrock Titan Embed Text v2 output" — confirmed via
`aws bedrock list-foundation-models` (both acc2 AND acc3) that Titan Embed is **not offered at
all** in this account/region; `invoke_model()` against `amazon.titan-embed-text-v2:0` fails with
`ValidationException: The provided model identifier is invalid` (caught live during this build's
own verify, not assumed from documentation). The only embedding-capable model either account
actually lists is `cohere.embed-v4:0` — confirmed live via a real `invoke-model` call, response
shape verified directly (`{"embeddings": {"float": [[1536 floats]]}}`), same class of
"the plan assumed a model that turns out not to be available on this account" finding this
codebase has hit before for Anthropic/GPT models (AA-329/AA-351). Genuinely lucky that Cohere v4's
requested `output_dimension` still lands on exactly 1536 — no migration change needed either way.

AA-685: the call goes through the gateway embed path (`shared.llm_client.embed.embed()`, stage
`f10_embed`), which takes the model and price from shared.llm_model_catalog, and every call now
writes a shared.llm_call_log row. Before AA-685 this module called boto3 directly and logged
nothing, so embedding spend never showed on External Spend.

Synchronous, like every other blocking Bedrock call in this codebase (`generate.py`'s own
docstring: "wrap at the async/sync boundary, not inside every helper", AA-416's documented
lesson) — the caller (`services/acp_content_writing/service.py`) wraps this in
`asyncio.to_thread()`, same as the write/rewrite/judge calls it already wraps.
"""
from __future__ import annotations

import threading
import time

import structlog

from shared.llm_client.call_log import record_call_sync
from shared.llm_client.embed import embed

logger = structlog.get_logger()

EMBEDDING_STAGE = "f10_embed"
EMBEDDING_DIMENSIONS = 1536  # matches content_piece.content_embedding vector(1536)

# Cohere Embed v4 accepts up to 128k tokens per text; a T9 piece is one short single-channel
# piece (generate.py's own docstring: "not the multi-H2 long-form draft"), realistically well
# under this — capped defensively so a pathological input can't fail the call outright, same
# "generous margin, not tuned to a real observed ceiling" rationale generate.py's own
# _MAX_TOKENS comment uses.
_MAX_INPUT_CHARS = 25000

# AA-610 (Sub 2) — this account's REAL Bedrock quota for Cohere Embed v4 is 20 requests/minute
# (confirmed live: `aws service-quotas list-service-quotas --service-code bedrock`, quota codes
# L-EB8C1F30/L-7089DC7D, both = 20.0), not a theoretical ceiling this build assumed. AA-499's
# original single-piece-per-write call site never hit this (one embed per approved T9 piece,
# nowhere near 20/minute) — Sub 2's per-atom/per-question landing does, by design (a Segment
# with several PAA candidates calls this once per candidate). A live re-atomize test hit
# ThrottlingException on effectively every call once volume rose past a handful of atoms.
#
# Paced at the account's own ceiling (60s / 20 = 3s apart), not tuned to "whatever felt safe" —
# 3.5s (not the bare 3.0s minimum) leaves a small margin for other callers (T9's own writes)
# sharing the same account-wide quota concurrently. A module-level `threading.Lock` serialises
# every caller through one pacing gate — `compute_embedding()` already runs on a
# `asyncio.to_thread()` worker (this module's own docstring), so blocking with `time.sleep()`
# here never blocks an event loop, only the one worker thread making the call.
_MIN_SECONDS_BETWEEN_CALLS = 3.5
_last_call_lock = threading.Lock()
_last_call_at = 0.0


def _pace_calls() -> None:
    """Blocks the calling thread just long enough to keep this process's own Bedrock InvokeModel
    calls at or under the account's real per-minute quota — see `_MIN_SECONDS_BETWEEN_CALLS`'s
    own comment for the number this is paced to and why. Does not (and cannot) account for
    OTHER processes/tasks sharing the same account-wide quota; it only stops a single process
    from being the one dogpiling requests to have this quota well under it, which is what a
    tight loop over many atoms/questions actually does."""
    global _last_call_at
    with _last_call_lock:
        wait = _MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def compute_embedding(text: str) -> list[float] | None:
    """Returns a 1536-float embedding, or `None` on any failure — soft-fail, same contract
    `generate.py`'s own summary extraction follows: a piece must never fail to persist because
    an embedding call errored. Callers should treat `None` as "not computed this time", not as a
    signal to retry inline (a real Bedrock outage shouldn't block T9's own write/check loop).

    `input_type="search_document"` (Cohere's own API — the text being STORED for later
    retrieval/comparison, as opposed to `"search_query"`, a query text searching against stored
    documents) — every call site in this build stores a finished piece, never searches with a
    fragment, so `search_document` is correct for all of them, not just the common case."""
    if not text or not text.strip():
        return None
    _pace_calls()
    try:
        resp = embed(EMBEDDING_STAGE, [text[:_MAX_INPUT_CHARS]], input_type="search_document",
                     dimensions=EMBEDDING_DIMENSIONS)
    except Exception as exc:
        logger.warning("content_embedding_call_failed", error_type=type(exc).__name__, error=str(exc))
        return None
    record_call_sync(
        stage=EMBEDDING_STAGE, role="embed", model=resp.model_used,
        tokens_in=resp.input_tokens, tokens_out=0, cost_usd=resp.cost_usd,
        quality_signal={"texts": 1, "dimensions": len(resp.vectors[0]),
                        "tokens_estimated": resp.tokens_estimated},
        account=resp.account, fallback_used=resp.fallback_used,
        provider="bedrock-native" if resp.account == "acc2" else "bedrock-satellite",
    )
    return resp.vectors[0]


def embedding_to_pgvector_literal(embedding: list[float]) -> str:
    """asyncpg has no built-in `vector` codec — this codebase's existing pgvector columns
    (migration 041) were never written to by any code either, so there's no existing convention
    to follow. The standard workaround (pgvector's own docs): pass the text literal
    '[0.1,0.2,...]' and let the SQL do an explicit `::vector` cast, rather than registering a
    custom asyncpg type codec for one column."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


__all__ = ["EMBEDDING_STAGE", "EMBEDDING_DIMENSIONS", "compute_embedding", "embedding_to_pgvector_literal"]
