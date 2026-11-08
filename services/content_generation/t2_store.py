"""AA-1049 W3 (root ADR 0020, eval item R0b, T2 train) — the T2 version store seam: the production input builder and
the store step a free offline replay can swap for a dry one. Same seam as the T9 PieceStore
(``services/acp_content_writing/piece_store.py``), for ``gold_aa_internal.tenant_tour_versions``.

- ``production_input(row)``: what a T2 rewrite reads, built by the production builders (``t2_rewrite_job
  .source_tour_dict`` for the writer's Master text, T3's grounding baseline, the raw FactSheet of
  ``factsheet.raw_source_tour``, the elephant-policy scan of ``t2_rewrite_job.policy_hold_hits``) with their shas. A
  replay builds its input here, never with a copy of a builder.
- the store step: the linear orchestrator's ``save_version`` (``T2_ORCHESTRATOR=linear``). In production it writes the
  row; inside ``use_store(DryStore())`` it hands the row to the dry store instead, which stamps the final contract with
  the sha of the text it would store (``final_contract.stamp_stored``) and writes nothing to the database: the resume
  checkpoints (``save_checkpoint`` / ``load_checkpoint``) and the review row go to the dry store too. The inputs a run
  reads (SEO, writing memory) still come from the pool the caller passes (a replay passes an in-memory one). The
  default T2 path (main's ``_rewrite_and_save``) is NOT routed through the seam: it stays byte-equal to main.

The active store is a ContextVar: a replay sets it for its own task only; production never sets it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from services.quality import final_contract as _fc

_STORE: ContextVar[Optional["DryStore"]] = ContextVar("t2_version_store", default=None)


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def production_input(row: Any) -> dict:
    """The inputs of one T2 rewrite of ``row`` (a ``t2_rewrite_job._PT_SQL`` row), built by the production builders:
    the writer's source (``tour_dict``) and its sha, T3's grounding baseline sha, the raw FactSheet sha and its
    pointer failures, and the policy-hold hits (non-empty = the D8 elephant path, 0 LLM calls)."""
    from services.jobs import t2_rewrite_job as job
    from services.quality import factsheet

    tour_dict = job.source_tour_dict(row)
    source_texts = [str(v) for v in tour_dict.values() if v]
    try:
        tour, agency = factsheet.raw_source_tour(row)
        sheet = factsheet.build_tour_factsheet(tour, agency=agency)
        fs_sha, pf = sheet.sha(), len(sheet.pointer_failures)
    except Exception:  # noqa: BLE001 — a row without a readable raw source: no FactSheet (recorded as None)
        fs_sha, pf = None, None
    return {"tour_dict": tour_dict, "source_sha": _sha(tour_dict), "grounding_sha": _sha(source_texts),
            "fs_sha": fs_sha, "fs_pointer_failures": pf, "policy_hold_hits": job.policy_hold_hits(row, tour_dict)}


class DryStore:
    """Records what the store step would write (no DB). ``rows[i]`` = the save fields + ``stored_sha`` + the stamped
    ``final_contract`` record (``sha_match``: stored text == gated text); ``reviews`` = the review rows it would add;
    ``checkpoints[version_id]`` = the last resume checkpoint (budget + best candidate) the run would persist."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.reviews: list[dict] = []
        self.checkpoints: dict[str, dict] = {}

    def save(self, row: dict) -> dict:
        stored_sha = _fc.tour_text_sha(row.get("rewritten"))
        record = (_fc.stamp_stored(row.get("final_contract"), stored_sha, actual=row.get("status"))
                  if row.get("final_contract") else None)
        out = {**row, "stored_sha": stored_sha, "final_contract": record}
        self.rows.append(out)
        return out


@contextlib.contextmanager
def use_store(store: DryStore) -> Iterator[DryStore]:
    token = _STORE.set(store)
    try:
        yield store
    finally:
        _STORE.reset(token)


def active() -> Optional[DryStore]:
    """The dry store of the current replay task, None in production (the linear ``save_version`` asks first)."""
    return _STORE.get()
