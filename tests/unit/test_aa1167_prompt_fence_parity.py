"""AA-1167 — parity evidence of the writer prompt change (root ADR 0020 change control, component prompt_registry):
with the untrusted-data fence off (the seed default of cis.llm.prompt_data_fence) the S1 v2, T2 / A1 v1 and T9 writer
prompts are byte-identical to the release before the fence existed.

The hashes below are sha256 of the renders of the guard's invented fixtures (tests/unit/test_aa1167_prompt_fence_guard.py)
produced by the builders of main BEFORE this change (60867997c, the builders had no fence parameter), and the same
renders on this tree with fence=False are compared to them. The prompt versions s1_generate v4 / t9_write v9 changed only
the hashed source, not an off render.

When a later change edits one of these prompts on purpose, it bumps the prompt version with its own evidence; update the
hashes here in that PR (they pin the AA-1167 off render, not the prompts forever). Invented text only."""
import hashlib

from services.acp_content_writing import prompts as t9
from services.content_generation.batch_prompt import build_s1_system_prompt, build_s1_user_prompt

from tests.unit import test_aa1167_prompt_fence_guard as g

# sha256 of the main renders (60867997c), fixtures of the guard module
MAIN_S1 = [  # (system, user) for writer, editor, meta
    ("3053411d4ec74a603d60416ef00dd5801db140e35665308725fd69c8b2fe0551",
     "3bb924c4504e2c61495bec8eb0fa7975d1a49717cd43d9a344e181d3be72a508"),
    ("a02a175a773f35f525c548556963a77ad20229897f43ec60ebd26f00662feb5b",
     "015e753bd0399f4ee0f4bbccc4d0b0db13501cff2e81f617a78f417df5d29a25"),
    ("5a58d9a7be318f11f134c5a1b60bf507f1608226adbc5f4b59fbe7b33ab316ca",
     "95e319ad6253a1ae7bf913b5ab0451cbd5d56d57aa8579fa789340156b94f5d2"),
]
MAIN_V1 = ["7837f9a7a6edbf34dc86220dde59e9d985073ff06b6ba10c1a9f38124ed49b7d",   # neutral voice
           "c854dce10691b1b074443af8a27f58b87948150fe0829b647c127ac1655abee2"]   # tenant voice
MAIN_T9 = ["93a28a3755ade9cd120df11729f18fe38eb5eb5a8361d1333bb831cd9d2696ea",   # blog, flat seed
           "a54ac779ebe505e39108da12ba3c0f8ff40baa4eb5177c793d4574b2a45ea53b",   # blog, route moments
           "ebccdd55ae08ad4374978acedfe3ce16c56e36c05b70ad9b9d397e11de45216d"]   # facebook, lead focus
MAIN_T2 = ("7a37fc0e44a65e3637b45361bf84248059066754ac8ca465e36510d5d6cee08b",
           "dfb12f051e7ccc3ef6f6a69cbbe3eb4eb67c80f49921a5e6a968c843f190cd7d")
MAIN_T9_SYSTEM = "cc9b4979f39d55e4c97cb3261cd0aa0f24199cb5c643bb3de0ba634091a7ec03"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_s1_v2_off_render_is_main():
    assert [(_h(s), _h(u)) for s, u in g._s1_prompts(False)] == MAIN_S1


def test_v1_rewrite_off_render_is_main():
    assert [_h(p) for p in g._v1_prompts(False)] == MAIN_V1


def test_t9_off_render_is_main():
    assert [_h(p) for p in g._t9_prompts(False)] == MAIN_T9
    assert _h(t9.SYSTEM_PROMPT) == MAIN_T9_SYSTEM


def test_t2_state_without_the_key_is_main():
    state = {"tour": g._tour(), "seo": {}, "few_shots": [], "is_tenant_rewrite": True, "brand_name": "Northstep"}
    assert (_h(build_s1_system_prompt(state)), _h(build_s1_user_prompt(state))) == MAIN_T2
