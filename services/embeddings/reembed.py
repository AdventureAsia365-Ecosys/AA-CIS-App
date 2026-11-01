"""AA-1026 D5 — re-embed one registered vector column into another embedding space (the logic of job kind
``embedding_reembed``; operator steps: root runbook ``docs/ops/runbooks/embedding-re-embed.md``).

Three modes, all for ONE column of ``shared.embedding_column``:

- ``dry_run`` ($0, no provider call): rows still to embed (a vector in the serving column, none in the shadow), blank
  texts, characters, estimated tokens (characters / 4, plus 10 %) and estimated USD at the target model's catalog
  price, requests, and whether the shadow is prepared. Works before ``embedding_prepare_shadow`` (then every row with a
  vector counts) so the cost is known before any DDL.
- ``fill``: needs the shadow prepared for the target space (registry row ``shadow_space_key`` = target, the column
  exists with the space's dimension). Pages through the rows in key order (keyset, ``sources.select_sql``), embeds each
  page through the gateway in the TARGET space only (``embed(..., space=target)``: its own models, never a fallback
  into another space), writes the shadow vectors, one ledger row per call (the column's writer stage, role embed,
  quality_signal op=reembed + ``embedding_space``). Checkpoint = the data: only rows whose shadow is still NULL are
  read, so a stopped, failed or released run resumes by queueing the same payload (a writer that rewrites a serving
  vector also empties its shadow: CIS 329 trigger ``<column>__shadow_reset``, so updated rows are re-read like new
  ones). Stops before a page whose estimate would cross ``cost_cap_usd`` (result ``stopped: cost_cap``), or after
  ``max_rows``. The cap covers the whole JOB: ``already_spent`` (``shared.job.cost_usd``, kept across retries and
  releases) counts against it. ``complete`` only when no row with a vector lacks a shadow vector, blank-text rows
  included (the swap refuses them too): their keys come back in ``blank_keys`` (runbook: purge or fix them).
- ``probe``: one embedding of a few words in the target space (its deployments must be enabled), so the fallback model
  is proven reachable before a fill. Ledger stage ``adhoc_conn_test``.

The swap, rollback and drops are SQL functions of the table owner (CIS 329), never this job.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any, Awaitable, Callable, Optional

import structlog

from services.embeddings import sources
from shared.llm_client import connections, embedding_space
from shared.llm_client.call_log import billed_fields, record_call_with_pool

logger = structlog.get_logger()

MODES = ("dry_run", "fill", "probe")
MAX_COST_CAP_USD = 25.0
PROBE_TEXT = "connectivity test"
PROBE_STAGE = "adhoc_conn_test"
TOKEN_MARGIN = 1.1
BLANK_KEYS_SHOWN = 50
# Pacing per provider request: Cohere Embed v4 on acc2 is 20 requests a minute for the whole account (content_embedding
# paces its own writers at 3.5 s); Titan takes one text per request, so its pages are smaller and paced at 1 s.
DEFAULTS = {"cohere": {"batch_size": 96, "pace_seconds": 3.5}}
OTHER_DEFAULTS = {"batch_size": 16, "pace_seconds": 1.0}
_CAST_RE = re.compile(r"^(vector|[a-z][a-z0-9_ ]*\[\])$")


class ReembedRefused(ValueError):
    """The request cannot run as asked (not retryable): unknown column, no source, not prepared, bad cap."""


class ReembedWrongSpace(ReembedRefused):
    """The gateway answered outside the target space (a routing or registry fault, raised after the paid call):
    nothing was written for the page, and a retry would only pay for the same page again (not retryable)."""


def defaults_for(space: embedding_space.EmbeddingSpace) -> dict:
    return dict(DEFAULTS.get(space.model_family, OTHER_DEFAULTS))


def estimate_tokens(chars: int) -> int:
    return int(math.ceil(max(0, chars) / 4 * TOKEN_MARGIN))


def price_in(space: embedding_space.EmbeddingSpace) -> Optional[float]:
    """USD per 1M input tokens of the space's primary model (catalog), None when unpriced or unknown."""
    from shared.llm_client.catalog import get_model_sync
    for key in space.model_keys:
        model = get_model_sync(key)
        if model is not None and model.price_in_per_mtok is not None:
            return float(model.price_in_per_mtok)
    return None


def estimate_usd(chars: int, price: Optional[float]) -> Optional[float]:
    return None if price is None else round(estimate_tokens(chars) * price / 1_000_000, 6)


def resolve(column_key: str, space_key: Optional[str]) -> tuple[embedding_space.EmbeddingColumn,
                                                                   embedding_space.EmbeddingSpace]:
    """(column row, target space). The target is ``space_key`` or the column's prepared shadow space."""
    try:
        col = embedding_space.column(column_key)
    except KeyError as e:
        raise ReembedRefused(str(e)) from None
    target_key = space_key or col.shadow_space_key
    if not target_key:
        raise ReembedRefused(f"{column_key}: name the target space (space_key); no shadow is prepared")
    try:
        target = embedding_space.space(target_key)
    except KeyError as e:
        raise ReembedRefused(str(e)) from None
    if target.status == "retired":
        raise ReembedRefused(f"space {target.space_key} is retired")
    if target.space_key == col.space_key:
        raise ReembedRefused(f"{column_key} already serves {target.space_key}")
    try:
        sources.source_for(col)
    except ValueError as e:
        raise ReembedRefused(str(e)) from None
    return col, target


def next_steps(col: embedding_space.EmbeddingColumn, target: embedding_space.EmbeddingSpace) -> list[str]:
    """The owner SQL around a fill (runbook), for the dry run's result."""
    prepare = (f"SELECT shared.embedding_prepare_shadow('{col.table_schema}', '{col.table_name}', "
               f"'{col.column_name}', '{target.space_key}');")
    return [prepare, "fill: job embedding_reembed mode=fill (resumable)",
            f"SELECT shared.embedding_swap('{col.swap_group}');  -- every column of the swap group filled",
            f"SELECT shared.embedding_rollback('{col.swap_group}');  -- while the __prev column is kept",
            f"SELECT shared.embedding_drop_shadow('{col.table_schema}', '{col.table_name}', '{col.column_name}');"
            "  -- abandon a fill instead"]


async def _shadow_type(conn, col: embedding_space.EmbeddingColumn, shadow: str) -> Optional[str]:
    return await conn.fetchval(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = to_regclass($1) AND a.attname = $2 AND NOT a.attisdropped",
        f"{col.table_schema}.{col.table_name}", shadow)


def cast_for(type_name: Optional[str], dimension: int) -> str:
    """The SQL cast of the shadow column's type: ``vector`` (its dimension must be the space's) or an array type
    (tests without pgvector). Refuses anything else."""
    if not type_name:
        raise ReembedRefused("the shadow column does not exist (run shared.embedding_prepare_shadow first)")
    if type_name.startswith("vector"):
        if type_name != f"vector({dimension})":
            raise ReembedRefused(f"the shadow column is {type_name}, the space has dimension {dimension}")
        return "vector"
    if _CAST_RE.match(type_name):
        return type_name
    raise ReembedRefused(f"the shadow column type {type_name} is not a vector")


def vector_literal(vec: list[float], cast: str) -> str:
    body = ",".join(repr(float(x)) for x in vec)
    return f"[{body}]" if cast == "vector" else "{" + body + "}"


def _provider(account: str) -> str:
    conn = connections.connection_or_builtin(account)
    return "bedrock-native" if conn is not None and conn.auth_mode == "task_role" else "bedrock-satellite"


async def dry_run(pool, column_key: str, space_key: Optional[str] = None) -> dict:
    col, target = resolve(column_key, space_key)
    prepared = bool(col.shadow_column and col.shadow_space_key == target.space_key)
    async with pool.acquire() as conn:
        if prepared and await _shadow_type(conn, col, col.shadow_column) is None:
            prepared = False
        row = await conn.fetchrow(sources.estimate_sql(col, col.shadow_column if prepared else None))
    rows, blank, chars = int(row["n_rows"]), int(row["n_blank"]), int(row["n_chars"])
    d = defaults_for(target)
    texts = max(0, rows - blank)
    requests = texts if target.model_family == "amazon" else math.ceil(texts / d["batch_size"])
    price = price_in(target)
    return {"mode": "dry_run", "column": col.qualified, "swap_group": col.swap_group, "from_space": col.space_key,
            "target_space": target.space_key, "model_key": target.model_keys[0], "dimension": target.dimension,
            "prepared": prepared, "rows": rows, "blank_rows": blank, "chars": chars,
            "estimated_tokens": estimate_tokens(chars), "price_in_per_mtok": price,
            "estimated_usd": estimate_usd(chars, price), "requests": requests, "batch_size": d["batch_size"],
            "estimated_seconds": round(math.ceil(texts / d["batch_size"]) * d["pace_seconds"], 1),
            "next_steps": next_steps(col, target)}


async def fill(pool, column_key: str, *, cost_cap_usd: float, batch_size: Optional[int] = None,
               max_rows: Optional[int] = None, pace_seconds: Optional[float] = None,
               embed_fn: Optional[Callable] = None,
               progress: Optional[Callable[..., Awaitable[None]]] = None,
               add_cost: Optional[Callable[[float], None]] = None,
               already_spent: float = 0.0,
               sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> dict:
    if not isinstance(cost_cap_usd, (int, float)) or isinstance(cost_cap_usd, bool) \
            or not 0 < float(cost_cap_usd) <= MAX_COST_CAP_USD:
        raise ReembedRefused(f"cost_cap_usd must be > 0 and <= {MAX_COST_CAP_USD}")
    col, target = resolve(column_key, None)
    if not col.writer_stages:
        raise ReembedRefused(f"{col.qualified} has no writer stage to route the calls")
    shadow = col.shadow_column
    d = defaults_for(target)
    page = int(batch_size or d["batch_size"])
    if not 1 <= page <= 96:
        raise ReembedRefused("batch_size must be 1..96")
    pace = d["pace_seconds"] if pace_seconds is None else max(0.0, float(pace_seconds))
    stage = col.writer_stages[0]
    src = sources.source_for(col)
    price = price_in(target)
    if price is None:
        raise ReembedRefused(f"{target.model_keys[0]} has no catalog price: the cost cap cannot be enforced")
    if embed_fn is None:
        from shared.llm_client.embed import embed as embed_fn
    async with pool.acquire() as conn:
        cast = cast_for(await _shadow_type(conn, col, shadow), target.dimension)
    upd = sources.update_sql(col, shadow, cast)

    # The cap is the job's: what earlier attempts of this job spent (shared.job.cost_usd) counts against it.
    prior = max(0.0, float(already_spent or 0.0))
    spent, written, blank, calls = 0.0, 0, 0, 0
    blank_keys: list[list[str]] = []
    last: Optional[tuple] = None
    stopped: Optional[str] = None
    last_call = 0.0
    while True:
        if max_rows and written + blank >= max_rows:
            stopped = "max_rows"
            break
        limit = page if not max_rows else min(page, max_rows - written - blank)
        async with pool.acquire() as conn:
            rows = await conn.fetch(sources.select_sql(col, shadow, after=last is not None), limit, *(last or ()))
        if not rows:
            break
        nkeys = len(col.key_columns)
        items = [(tuple(r[f"k{i}"] for i in range(nkeys)), src.text(r["text"])) for r in rows]
        last = items[-1][0]
        todo = [(k, t) for k, t in items if t]
        blank += len(items) - len(todo)
        for k, t in items:
            if not t and len(blank_keys) < BLANK_KEYS_SHOWN:
                blank_keys.append([str(x) for x in k])
        if not todo:
            continue
        est = estimate_usd(sum(len(t) for _, t in todo), price) or 0.0
        if prior + spent + est > float(cost_cap_usd):
            stopped = "cost_cap"
            break
        wait = pace - (time.monotonic() - last_call)
        if calls and wait > 0:
            await sleep(wait)
        last_call = time.monotonic()
        resp = await asyncio.to_thread(embed_fn, stage, [t for _, t in todo],
                                       input_type=target.input_type("document"), dimensions=target.dimension,
                                       space=target)
        calls += 1
        cost = float(resp.cost_usd or 0.0)
        spent += cost
        if add_cost is not None:
            add_cost(cost)
        await record_call_with_pool(
            pool, stage=stage, role="embed", model=resp.model_used, tokens_in=resp.input_tokens, tokens_out=0,
            cost_usd=resp.cost_usd,
            quality_signal={"app": "cis", "issue": "AA-1026", "op": "reembed", "column": col.qualified,
                            "texts": len(todo), "dimensions": len(resp.vectors[0]) if resp.vectors else None,
                            "tokens_estimated": resp.tokens_estimated, **embedding_space.ledger_signal(resp)},
            account=resp.account, fallback_used=resp.fallback_used, provider=_provider(resp.account),
            **billed_fields(resp))
        if getattr(resp, "space_key", None) != target.space_key or len(resp.vectors) != len(todo) \
                or any(len(v) != target.dimension for v in resp.vectors):
            raise ReembedWrongSpace(f"embedding answer is not in {target.space_key} (space {resp.space_key!r}, "
                                    f"{len(resp.vectors)} vectors): nothing written for this page")
        async with pool.acquire() as conn:
            await conn.executemany(upd, [(vector_literal(v, cast), *k) for (k, _), v in zip(todo, resp.vectors)])
        written += len(todo)
        if progress is not None:
            await progress(step="fill", column=col.qualified, written=written, blank=blank, calls=calls,
                           spent_usd=round(spent, 6), job_spent_usd=round(prior + spent, 6),
                           last_key=[str(x) for x in last])
    result = {"mode": "fill", "column": col.qualified, "target_space": target.space_key, "shadow_column": shadow,
              "written": written, "blank_rows": blank, "blank_keys": blank_keys, "calls": calls,
              "spent_usd": round(spent, 6), "job_spent_usd": round(prior + spent, 6),
              "cost_cap_usd": float(cost_cap_usd), "stopped": stopped}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sources.estimate_sql(col, shadow))
    result["remaining_rows"] = int(row["n_rows"])
    result["remaining_blank_rows"] = int(row["n_blank"])
    # The swap refuses while any row has a vector but no shadow vector, blank-text rows included: so does this.
    result["complete"] = result["remaining_rows"] == 0
    logger.info("embedding_reembed_fill", **{k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return result


async def probe(pool, space_key: str, *, embed_fn: Optional[Callable] = None) -> dict:
    try:
        target = embedding_space.space(space_key)
    except KeyError as e:
        raise ReembedRefused(str(e)) from None
    if embed_fn is None:
        from shared.llm_client.embed import embed as embed_fn
    out = {"mode": "probe", "target_space": target.space_key}
    try:
        resp = await asyncio.to_thread(embed_fn, PROBE_STAGE, [PROBE_TEXT], input_type=target.input_type("query"),
                                       dimensions=target.dimension, space=target)
    except Exception as e:  # noqa: BLE001 — the answer of a probe is the outcome; the gateway logged the refusal
        out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:300]}")
        return out
    await record_call_with_pool(
        pool, stage=PROBE_STAGE, role="embed", model=resp.model_used, tokens_in=resp.input_tokens, tokens_out=0,
        cost_usd=resp.cost_usd,
        quality_signal={"app": "cis", "issue": "AA-1026", "op": "reembed_probe",
                        "dimensions": len(resp.vectors[0]) if resp.vectors else None,
                        **embedding_space.ledger_signal(resp)},
        account=resp.account, fallback_used=resp.fallback_used, provider=_provider(resp.account),
        **billed_fields(resp))
    ok = bool(resp.vectors) and len(resp.vectors[0]) == target.dimension and resp.space_key == target.space_key
    out.update(ok=ok, model_used=resp.model_used, account=resp.account, dimension=len(resp.vectors[0]),
               cost_usd=resp.cost_usd)
    return out


__all__ = ["MODES", "MAX_COST_CAP_USD", "ReembedRefused", "ReembedWrongSpace", "dry_run", "fill", "probe", "resolve",
           "estimate_tokens", "estimate_usd", "cast_for", "vector_literal", "next_steps"]
