"""AA-763 (G4) — stage keys for the shared S1 content graph.

T2 (a tenant's tour rewrite) runs the same graph as S1 (the A1 platform rewrite). Each model call
picks its route in shared.llm_role_config by stage key and logs under it, so a shared `s1_*` key
mixed T2 cost, routing and shadow config into A1's. `stage_for()` gives a T2 run its own `t2_*`
twin (same route, seeded by migration 219) — the precedent is generate_node's `generate_stage`
(`t2_generate`, AA-620). A1 keeps its `s1_*` keys unchanged.
"""
from __future__ import annotations

# A1 key -> T2 twin. Only route keys (LLMRequest.stage) — Jev stages are separate.
T2_TWINS = {
    "s1_judge": "t2_judge",
    "s1_brand_audit": "t2_brand_audit",
    "s1_flag_fix": "t2_flag_fix",
    "s1_itinerary_nudge": "t2_itinerary_nudge",
}

# T7 Debate's brand-fit call (services/acp_shared/debate.py) — same route as s1_judge.
T7_DEBATE_JUDGE = "t7_debate_judge"


def stage_for(state: dict | None, a1_stage: str) -> str:
    """The stage key a graph node uses: the `t2_*` twin on a tenant rewrite, else `a1_stage`."""
    if state and state.get("is_tenant_rewrite"):
        return T2_TWINS.get(a1_stage, a1_stage)
    return a1_stage
