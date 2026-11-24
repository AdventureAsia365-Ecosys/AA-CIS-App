"""AA-1154 (A0, CIS 481) — the limits of the CIS admin assistant (ops assist), on Platform › Settings (area "ops").

The assistant (``api/services/ops_assist/``) turns the evidence of a Needs-attention item or an incident into ranked
action cards by rule. Every window, trigger, floor and cap it uses is a setting (tech lead S279: a threshold is a
setting, maker-checker and audited, with the S285 advisory value as the seeded default), never a code literal:

- ``evidence_window_min``: how far back the LLM ledger and the job failures are read (advisory: 30 minutes);
- ``changes_window_min``: how far back "what changed" reads the ops events and route versions (advisory: 2 hours);
- ``refused_trigger_calls``: refused account-level calls on one connection inside the window that open an
  "LLM access refused" signal although its circuit has not opened yet;
- ``spend_spike_factor``: spend per hour on a connection against its 7-day hourly median that opens a cost signal;
- ``wide_scope_stages``: an action on a connection or a route that serves at least this many stages is wide (R3);
- ``small_money_usd``: the most an action may cost and still count as "a few cents" (R1); above it is paid (R3);
- ``history_min_n``: a "right k of n" confidence is shown only from this many measured outcomes (advisory: 5);
- ``card_ttl_min``: a card older than this is locked and the item must be analysed again (advisory: 30 minutes);
- ``reason_min_chars``: the shortest reason an R3 card asks for (tech lead AI7: 20 characters).

``shared.settings.get`` serves the last snapshot and the compiled default before the first refresh, so a read never
blocks and never fails. The keys equal the CIS 481 seed (tests/integration/test_aa1154_migration_481.py and the
settings-lint reader check, tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

from shared import settings

EVIDENCE_WINDOW_MIN = "cis.ops.assist_evidence_window_min"
CHANGES_WINDOW_MIN = "cis.ops.assist_changes_window_min"
REFUSED_TRIGGER_CALLS = "cis.ops.assist_refused_trigger_calls"
SPEND_SPIKE_FACTOR = "cis.ops.assist_spend_spike_factor"
WIDE_SCOPE_STAGES = "cis.ops.assist_wide_scope_stages"
SMALL_MONEY_USD = "cis.ops.assist_small_money_usd"
HISTORY_MIN_N = "cis.ops.assist_history_min_n"
CARD_TTL_MIN = "cis.ops.assist_card_ttl_min"
REASON_MIN_CHARS = "cis.ops.assist_reason_min_chars"

ALL = (EVIDENCE_WINDOW_MIN, CHANGES_WINDOW_MIN, REFUSED_TRIGGER_CALLS, SPEND_SPIKE_FACTOR, WIDE_SCOPE_STAGES,
       SMALL_MONEY_USD, HISTORY_MIN_N, CARD_TTL_MIN, REASON_MIN_CHARS)

# The names the rules use; ``snapshot()`` maps them to their current values.
NAMES = {
    "evidence_window_min": EVIDENCE_WINDOW_MIN,
    "changes_window_min": CHANGES_WINDOW_MIN,
    "refused_trigger_calls": REFUSED_TRIGGER_CALLS,
    "spend_spike_factor": SPEND_SPIKE_FACTOR,
    "wide_scope_stages": WIDE_SCOPE_STAGES,
    "small_money_usd": SMALL_MONEY_USD,
    "history_min_n": HISTORY_MIN_N,
    "card_ttl_min": CARD_TTL_MIN,
    "reason_min_chars": REASON_MIN_CHARS,
}


def snapshot() -> dict:
    """{name: value} of every assistant limit, read once per analysis so one set of cards uses one set of limits.
    Numbers as float, except the counts and minutes (int)."""
    out: dict = {}
    for name, key in NAMES.items():
        v = float(settings.get(key))
        out[name] = v if name in ("spend_spike_factor", "small_money_usd") else int(v)
    return out


def keys() -> dict:
    """{name: settings key}, for the links the page draws next to a value."""
    return dict(NAMES)
