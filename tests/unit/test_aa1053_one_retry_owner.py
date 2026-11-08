"""AA-1053 (W1 contradiction #13) — the gateway is the ONLY owner of the retry of a reply cut at its token ceiling.

``LLMClient._call_key_logged`` (shared/llm_client/client.py, rules in token_floor.py) asks a cut reply once more on the
same route model with double the ceiling. Before AA-1053 a3_atomize opted out of that retry (``NO_RETRY_STAGES``) and
re-asked a cut read itself at its own ceiling: two owners, two ceilings, and a cut that could be billed twice. These
tests fail on that double-owner path:

1. the static guard: no code outside the gateway re-asks the model after reading a cut signal (a loop that calls the
   gateway and reads a cut signal is registered in CUT_AWARE_LOOPS with the reason it stops; a loop that rebinds
   max_tokens, or an ``if <cut>:`` that calls the gateway, has no allowlist);
2. the registry: the only stage the gateway does not retry is the connection probe;
3. the behaviour: a3_atomize sends one gateway request per cut read (the gateway retried it) and never a bigger
   ceiling of its own.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client import token_floor

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("api", "services", "shared", "worker", "scripts")
# The gateway itself (its own retry lives here) and the pure token core.
GATEWAY_PREFIXES = ("shared/llm_client/", "shared/aa_llm/")

# A reference to a gateway entry point: LLMClient.generate, the token_budget wrappers, the judge helpers, and the
# local ``_generate*`` / ``*_generate`` wrappers call sites build around them.
_GATEWAY_CALL = re.compile(r"^(?:_?a?generate(?:_\w+)?|\w+_a?generate|invoke_judge|call_judge)$")
# A read of a cut: the cut predicates of the gateway and the caller-side helper.
_CUT_PREDICATES = frozenset({"is_truncated", "is_cut", "is_cut_reply", "is_cut_failure", "route_ended_on_cut",
                             "still_cut"})
_CUT_STOPS = frozenset({"max_tokens", "length", "model_context_window_exceeded"})

# "<file>:<function>" -> why a loop there that calls the gateway and reads a cut is NOT a cut retry. Each entry has a
# behaviour test. Grow only with a reason; a cut retry belongs in the gateway (token_floor.py), never here.
CUT_AWARE_LOOPS = {
    "services/acp_contract/a3_atomize.py:_ask_atoms":
        "a cut read ends the loop (the gateway already retried it once): a cut reply, or a route error any rung of "
        "which ended on a cut (token_floor.route_ended_on_cut); only a non-cut parse failure or a non-cut error is "
        "asked again, at the same ceiling (test_a3_atomize_never_re_asks_a_cut_read, "
        "test_a3_atomize_does_not_re_ask_a_route_that_ended_on_billed_cuts)",
}


def _sources():
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "tests" in p.parts or "node_modules" in p.parts:
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(GATEWAY_PREFIXES):
                continue
            yield rel, ast.parse(p.read_text(encoding="utf-8"))


def _name_of(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _calls_gateway(nodes) -> bool:
    return any(isinstance(n, (ast.Attribute, ast.Name)) and _GATEWAY_CALL.match(_name_of(n))
               for root in nodes for n in ast.walk(root))


def _mentions_stop_reason(node) -> bool:
    for n in ast.walk(node):
        if "stop_reason" in _name_of(n) or (isinstance(n, ast.Constant) and n.value == "stop_reason"):
            return True
    return False


def _is_cut_read(node) -> bool:
    if isinstance(node, (ast.Attribute, ast.Name)):
        name = _name_of(node)
        return name in _CUT_PREDICATES or "TRUNCATED_STOP" in name
    if isinstance(node, ast.Compare) and _mentions_stop_reason(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and n.value in _CUT_STOPS:
                return True
            if isinstance(n, (ast.Name, ast.Attribute)) and "TRUNCAT" in _name_of(n).upper():
                return True
    return False


def _reads_cut(nodes) -> bool:
    return any(_is_cut_read(n) for root in nodes for n in ast.walk(root))


def _rebinds_max_tokens(nodes) -> bool:
    for root in nodes:
        for n in ast.walk(root):
            targets = (n.targets if isinstance(n, ast.Assign)
                       else [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else [])
            if any(isinstance(t, ast.Name) and "max_tokens" in t.id.lower() for t in targets):
                return True
    return False


def _functions(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def scan_tree(rel: str, tree) -> tuple[set, set]:
    """(cut-aware gateway loops, forbidden cut retries) of one module, as '<file>:<function>' labels."""
    aware: set = set()
    forbidden: set = set()
    for fn in _functions(tree):
        label = f"{rel}:{fn.name}"
        for n in ast.walk(fn):
            if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
                body = n.body + n.orelse
                if not _calls_gateway(body):
                    continue
                if _rebinds_max_tokens(body) and _reads_cut(body):
                    forbidden.add(f"{label} (a loop raises max_tokens after a cut)")
                elif _reads_cut(body):
                    aware.add(label)
            elif isinstance(n, ast.If) and _reads_cut([n.test]) and _calls_gateway(n.body + n.orelse):
                forbidden.add(f"{label} (calls the gateway again on a cut)")
    return aware, forbidden


def _scan_repo() -> tuple[set, set]:
    aware: set = set()
    forbidden: set = set()
    for rel, tree in _sources():
        a, f = scan_tree(rel, tree)
        aware |= a
        forbidden |= f
    return aware, forbidden


# ── 1. the static guard ────────────────────────────────────────────────────────────────────────────────────────────

def test_no_call_site_retries_a_cut_reply():
    aware, forbidden = _scan_repo()
    assert not forbidden, ("a call site retries a reply cut at its token ceiling; the gateway owns that retry "
                           f"(shared/llm_client/token_floor.py): {sorted(forbidden)}")
    new = sorted(aware - set(CUT_AWARE_LOOPS))
    assert not new, ("a loop calls the gateway and reads a cut signal: a cut must end it (the gateway already retried "
                     f"once), then register it in CUT_AWARE_LOOPS with the reason and a behaviour test: {new}")
    stale = sorted(set(CUT_AWARE_LOOPS) - aware)
    assert not stale, f"no longer a cut-aware gateway loop: remove from CUT_AWARE_LOOPS: {stale}"


_DOUBLE_OWNER = '''
async def ask(prompt):
    max_tokens = 4096
    for attempt in range(2):
        resp = await asyncio.to_thread(LLMClient().generate, LLMRequest(user_prompt=prompt, max_tokens=max_tokens))
        if resp.stop_reason in _TRUNCATED_STOP_REASONS:
            max_tokens = 8192
            continue
        return resp
'''

_IF_RETRY = '''
def write(client, request):
    resp = client.generate(request)
    if resp.stop_reason == "max_tokens":
        resp = client.generate(request.model_copy(update={"max_tokens": 16000}))
    return resp
'''

_STOPS_ON_CUT = '''
def ask(client, request):
    for attempt in range(2):
        resp = client.generate(request)
        if is_truncated(resp.stop_reason):
            return None
        if parse(resp):
            return resp
'''


def test_the_guard_catches_the_double_owner_shapes():
    """The shape a3_atomize had before AA-1053 (a loop that raises its ceiling on a cut) and an ``if <cut>:`` re-ask
    are forbidden; a loop that stops on a cut is only 'cut-aware' (registered with a reason)."""
    _, forbidden = scan_tree("x.py", ast.parse(_DOUBLE_OWNER))
    assert forbidden == {"x.py:ask (a loop raises max_tokens after a cut)"}
    _, forbidden = scan_tree("x.py", ast.parse(_IF_RETRY))
    assert forbidden == {"x.py:write (calls the gateway again on a cut)"}
    aware, forbidden = scan_tree("x.py", ast.parse(_STOPS_ON_CUT))
    assert aware == {"x.py:ask"} and not forbidden


# ── 2. the registry ────────────────────────────────────────────────────────────────────────────────────────────────

def test_only_the_connection_probe_is_left_out_of_the_gateway_retry():
    assert token_floor.NO_RETRY_STAGES == frozenset({"adhoc_conn_test"})
    req = MagicMock(stage="a3_atomize", budget_retry=True)
    assert token_floor.retry_allowed(req, {})


def test_a3_atomize_keeps_its_first_ceiling():
    """a3_atomize moved its retry to the gateway without a new first ceiling: it stays out of the floor, so the
    gateway's retry ceiling (double 4096) is the 8192 its own re-ask used."""
    from shared.aa_llm import tokens

    assert "a3_atomize" in token_floor.NO_FLOOR_STAGES
    luna = MagicMock(model_key="gpt-6-luna", capabilities={"min_output_tokens": 8192}, max_output_tokens=128000)
    req = MagicMock(stage="a3_atomize", max_tokens=4096)
    assert token_floor.floored_max_tokens(req, luna) == 4096
    assert token_floor.retry_ceiling(4096, luna) == 8192 == tokens.retry_budget(4096)


# ── 3. the behaviour of a3_atomize ─────────────────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, content, stop_reason="end_turn", retried=None):
        self.content = content
        self.model_used, self.input_tokens, self.output_tokens, self.cost_usd = "m", 10, 5, 0.001
        self.stop_reason, self.satellite_account, self.fallback_used, self.provider = stop_reason, None, False, "x"
        self.truncation_retry_max_tokens = retried


def _atoms(place, action):
    return json.dumps({"atoms": [{"place": place, "action": action, "activity_type": "culture"}]})


async def _ask(*replies):
    from services.acp_contract import a3_atomize

    client = MagicMock()
    client.generate = MagicMock(side_effect=list(replies))
    unusable = AsyncMock()
    with patch.object(a3_atomize, "LLMClient", return_value=client), \
            patch.object(a3_atomize, "record_unusable", unusable):
        out = await a3_atomize._ask_atoms("prompt", "writer", event="a3_atomize_day", log_fields={})
    return out, client.generate, unusable


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["max_tokens", "length"])
async def test_a3_atomize_never_re_asks_a_cut_read(stop_reason):
    """The gateway retried the cut read (its reply says so); still cut = the read failed: no third call, no ceiling
    of a3's own. Red on base: a3 re-asked at _TRUNCATION_RETRY_MAX_TOKENS."""
    cut = '{"atoms": [{"place": "Brindle Cove", "action": "walk"}, {pla'
    (atoms, result, attempt, error), gen, unusable = await _ask(
        _Resp(cut, stop_reason, retried=8192), _Resp(_atoms("Brindle Cove", "walk")))
    assert atoms is None and result is None and gen.call_count == 1
    assert [c.args[0].max_tokens for c in gen.call_args_list] == [4096]
    assert "cut at its token ceiling" in error
    unusable.assert_awaited_once()                 # the billed cut reply keeps its billed_unusable row


def _route_unusable_cut(stop_reason="max_tokens"):
    """What ``LLMClient.generate`` raises when every rung of the route answered with an unusable reply cut at its
    ceiling (each after its gateway retry): a RouteUnusableReply with the billing dicts in ``logged_usage``, no cause
    and no ``billed_usage`` (client._call_route)."""
    from shared.llm_client.client import RouteUnusableReply

    exc = RouteUnusableReply("All models in the route failed for stage 'a3_atomize': "
                             "orchid-x: returned empty content; orchid-y: returned empty content")
    exc.logged_usage = (
        {"model": "orchid-x", "stop_reason": stop_reason, "cost_usd": 0.01},   # the cut
        {"model": "orchid-x", "stop_reason": stop_reason, "cost_usd": 0.02},   # its gateway retry, cut again
        {"model": "orchid-y", "stop_reason": stop_reason, "cost_usd": 0.01},
        {"model": "orchid-y", "stop_reason": stop_reason, "cost_usd": 0.02},
    )
    return exc


def _route_failed_with_refused_cut():
    """What ``generate`` raises when one rung's provider refused to return a cut reply (nothing billed, after the
    gateway retry) and another rung was down: the ``_all_failed`` aggregate (llm_error_class decided, only the last
    failure as cause)."""
    from shared.llm_client.client import _all_failed

    refused = RuntimeError("Error code: 400 - Could not finish the message because max_tokens or model output limit "
                           "was reached")
    down = RuntimeError("Error code: 503 - upstream unavailable")
    return _all_failed("All models in the route failed for stage 'a3_atomize'", [refused, down])


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["max_tokens", "length"])
async def test_a3_atomize_does_not_re_ask_a_route_that_ended_on_billed_cuts(stop_reason):
    """Red before the fix: is_cut_failure read neither logged_usage nor the route's aggregate, so a3 asked the whole
    route again and every rung billed its cut + gateway retry a second time."""
    exc = _route_unusable_cut(stop_reason)
    assert not token_floor.is_cut_failure(exc)          # the per-attempt predicate the gateway uses stays per attempt
    (atoms, _, attempt, error), gen, _ = await _ask(exc, _Resp(_atoms("Brindle Cove", "walk")))
    assert atoms is None and gen.call_count == 1 and attempt == 1 and "RouteUnusableReply" in error


@pytest.mark.asyncio
async def test_a3_atomize_does_not_re_ask_a_route_with_a_refused_cut():
    exc = _route_failed_with_refused_cut()
    assert not token_floor.is_cut_failure(exc)
    (atoms, _, attempt, _), gen, _ = await _ask(exc, _Resp(_atoms("Brindle Cove", "walk")))
    assert atoms is None and gen.call_count == 1 and attempt == 1


@pytest.mark.asyncio
async def test_a3_atomize_does_not_re_ask_a_raised_cut():
    from shared.llm_client.client import BilledUnusableReply

    exc = BilledUnusableReply("haiku-5-5 returned empty content (stop_reason=max_tokens)")
    exc.billed_usage = [{"stop_reason": "max_tokens", "cost_usd": 0.01}]
    (atoms, _, _, error), gen, _ = await _ask(exc, _Resp(_atoms("Brindle Cove", "walk")))
    assert atoms is None and gen.call_count == 1 and "BilledUnusableReply" in error


@pytest.mark.asyncio
async def test_a3_atomize_still_re_asks_a_route_that_failed_without_a_cut():
    """An outage or a non-cut unusable reply on every rung is still asked once more (AA-645)."""
    from shared.llm_client.client import RouteUnusableReply, _all_failed

    bad_json = RouteUnusableReply("All models in the route failed for stage 'a3_atomize'")
    bad_json.logged_usage = ({"model": "orchid-x", "stop_reason": "end_turn", "cost_usd": 0.01},)
    outage = _all_failed("All models in the route failed", [RuntimeError("Error code: 503 - upstream unavailable")])
    for exc in (bad_json, outage):
        (atoms, _, attempt, _), gen, _ = await _ask(exc, _Resp(_atoms("Brindle Cove", "walk")))
        assert atoms and attempt == 2 and gen.call_count == 2


def test_route_ended_on_cut_reads_every_route_error_shape():
    from shared.llm_client.client import BilledUnusableReply, _all_failed

    billed_cut = BilledUnusableReply("empty content")
    billed_cut.billed_usage = [{"stop_reason": "length", "cost_usd": 0.01}]
    assert token_floor.route_ended_on_cut(billed_cut)                                  # a model's own cut
    assert token_floor.route_ended_on_cut(_route_unusable_cut())                       # every rung billed cut
    assert token_floor.route_ended_on_cut(_route_failed_with_refused_cut())            # refused cut + outage
    # a billed cut on an earlier rung, the outage last (the aggregate's cause is the outage)
    mixed = _all_failed("route failed", [billed_cut, RuntimeError("Error code: 503 - upstream unavailable")])
    assert token_floor.route_ended_on_cut(mixed)
    # a legacy alias aggregate over a deployment aggregate keeps the inner cut visible
    nested = _all_failed("legacy failed", [_route_failed_with_refused_cut()])
    assert token_floor.route_ended_on_cut(nested)
    assert not token_floor.route_ended_on_cut(_all_failed("route failed", [RuntimeError("Error code: 503")]))
    assert not token_floor.route_ended_on_cut(RuntimeError("boom"))


@pytest.mark.asyncio
async def test_a3_atomize_still_re_asks_a_malformed_read_that_was_not_cut():
    (atoms, _, attempt, _), gen, _ = await _ask(_Resp("{not json"), _Resp(_atoms("Brindle Cove", "walk")))
    assert atoms and attempt == 2
    assert [c.args[0].max_tokens for c in gen.call_args_list] == [4096, 4096]


def test_the_route_error_of_billed_cuts_is_read_as_a_cut():
    """The real wrapper: ``LLMClient._call_route`` turns each rung's billed cut (after its gateway retry) into the
    RouteUnusableReply ``generate`` raises; route_ended_on_cut reads it, is_cut_failure does not."""
    from shared.llm_client import client as gw
    from shared.llm_client.client import BilledUnusableReply, LLMClient, LLMRequest, RouteUnusableReply

    def cut(*_a, **_k):
        exc = BilledUnusableReply("returned empty content (stop_reason=max_tokens)")
        exc.billed_usage = [{"model": "m", "stop_reason": "max_tokens", "cost_usd": 0.01}]
        raise exc

    llm = LLMClient.__new__(LLMClient)
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="a3_atomize", max_tokens=4096)
    with patch.object(LLMClient, "_route_attempts", lambda self, r, chain: [(k, None) for k in chain]), \
            patch.object(LLMClient, "_call_key_logged", side_effect=cut), \
            patch.object(LLMClient, "_deadline_gate", lambda *a, **k: None), \
            patch.object(gw, "stage_role", lambda r: None), patch.object(gw, "stage_pii_class", lambda r: None):
        with pytest.raises(RouteUnusableReply) as raised:
            llm._call_route(req, ["haiku", "sonnet"], "acct")
    assert not token_floor.is_cut_failure(raised.value)
    assert token_floor.route_ended_on_cut(raised.value)
