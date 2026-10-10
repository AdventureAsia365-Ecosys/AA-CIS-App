"""S218 — a merge target that only pseudo-atoms joined this run must exist in atom_segment
before members are re-pointed to it or an alias row names it (both are FKs).

Live 08/10: after the PK fix (#592), 7 backfill tours failed with ForeignKeyViolationError on
atom_segment_member_segment_id_fkey / atom_segment_alias_segment_id_canonical_fkey."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import segment_matching as sm
from services.acp_contract.segment_matching import Segment, _PSEUDO_PREFIX

TOUR = "11111111-1111-1111-1111-111111111111"


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        if "v_active_tour_atoms" in sql:
            return [{"atom_id": "atomX", "tour_id": TOUR, "itinerary_day": 1, "place": "Kyoto", "action": "walk",
                     "activity_type": "culture", "country": "Japan"}]
        if "FROM acp_contract.atom_segment asg" in sql:
            return [{"segment_id": s, "canonical_place": "Gion", "canonical_action": "stroll", "country": "Japan",
                     "member_types": ["culture"]}
                    for s in ("old1", "old2")]
        return []

    async def executemany(self, sql, rows):
        self.calls.append((" ".join(sql.split()), list(rows)))

    async def execute(self, sql, *args):
        self.calls.append((" ".join(sql.split()), []))

    def transaction(self):
        t = MagicMock()
        t.__aenter__ = AsyncMock(return_value=None)
        t.__aexit__ = AsyncMock(return_value=False)
        return t


@pytest.mark.asyncio
async def test_pseudo_only_merge_target_is_created_before_repoint_and_alias():
    conn = _Conn()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    merged = Segment("new_target", "Gion", "stroll", (f"{_PSEUDO_PREFIX}old1", f"{_PSEUDO_PREFIX}old2"))
    own = Segment("segX", "Kyoto", "walk", ("atomX",))
    with patch.object(sm, "_apart_pairs", AsyncMock(return_value=(frozenset(), 0))), \
         patch.object(sm, "derive_segments", return_value=[merged, own]), \
         patch.object(sm, "reconcile_ids", return_value=([merged, own], {"old1": "new_target", "old2": "new_target"})):
        out = await sm.run_segment_matching(TOUR, pool)

    kinds = [c[0] for c in conn.calls]
    seg_i = next(i for i, k in enumerate(kinds) if k.startswith("INSERT INTO acp_contract.atom_segment ("))
    repoint_i = next(i for i, k in enumerate(kinds) if "SELECT $2, atom_id" in k)
    alias_i = next(i for i, k in enumerate(kinds) if k.startswith("INSERT INTO acp_contract.atom_segment_alias"))
    assert seg_i < repoint_i < alias_i
    written_ids = {r[0] for r in conn.calls[seg_i][1]}
    assert written_ids == {"new_target", "segX"}   # the pseudo-only target is created too
    assert out["segments_written"] == 1 and out["aliases"] == 2
