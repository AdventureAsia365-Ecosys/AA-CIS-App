"""AA-1049 W3 (root ADR 0020, eval item R0b) — the T9 PieceStore seam: the production input builder and the store step
a free offline replay can swap for a dry one.

- `production_input(context)`: what the gates of a write treat as the source — the same `_grounding_text` /
  `_grounding_sheet` run_write_background builds (one implementation), with its sha. A replay builds its input here,
  never with a copy of the builder.
- the store step: run_write_background's one `service._finalize_piece` call. In production it writes the row
  (unchanged); inside `use_store(DryStore())` `_finalize_piece` hands the row to the dry store instead, which stamps
  the final contract with the sha of the text it would store (`final_contract.stamp_stored`) and touches no database.
  A replay that drives the store step reads the stamp on the dry rows (stored sha vs gated sha); the offline
  declared-diff replay (scripts/quality/replay_aa1049_t9.py) does not drive it yet and checks stored contracts only.

The active store is a ContextVar: a replay sets it for its own task only; production never sets it.
"""
from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from services.quality import final_contract as _fc

_STORE: ContextVar[Optional["DryStore"]] = ContextVar("t9_piece_store", default=None)


def production_input(context: dict) -> dict:
    """The gates' source of one write, built by the production builder (service._grounding_text / _grounding_sheet)."""
    from services.acp_content_writing import service  # lazy: service imports this module
    text = service._grounding_text(context)
    sheet = service._grounding_sheet(context)
    return {"grounding_text": text, "grounding_sha": hashlib.sha256(text.encode()).hexdigest()[:16],
            "fs_sha": sheet.sha(), "fs_lines": len(getattr(sheet, "lines", []) or [])}


class DryStore:
    """Records what the store step would write (no DB). `rows[i]` = the finalize kwargs + `stored_sha` + the stamped
    `final_contract` record (`sha_match`: stored text == gated text)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def finalize(self, pool: Any, **kw: Any) -> dict:
        stored_sha = _fc.text_sha(kw.get("content_text"))
        record = _fc.stamp_stored(kw.get("final_contract"), stored_sha, actual=kw.get("status")) \
            if kw.get("final_contract") else None
        row = {**kw, "stored_sha": stored_sha, "final_contract": record}
        self.rows.append(row)
        return row


@contextlib.contextmanager
def use_store(store: DryStore) -> Iterator[DryStore]:
    token = _STORE.set(store)
    try:
        yield store
    finally:
        _STORE.reset(token)


def active() -> Optional[DryStore]:
    """The dry store of the current replay task, None in production (service._finalize_piece asks first)."""
    return _STORE.get()


async def finalize(pool: Any, **kw: Any) -> dict:
    """The store step as run_write_background calls it: service._finalize_piece, which hands the row to the active
    dry store when a replay set one."""
    from services.acp_content_writing import service
    return await service._finalize_piece(pool, **kw)
