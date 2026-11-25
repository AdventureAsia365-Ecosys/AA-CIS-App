"""AA-1154 (A0, CIS 481) — the limits of the CIS admin assistant (ops assist), on Platform › Settings (area "ops").

The assistant (``api/services/ops_assist/``) turns the evidence of a Needs-attention item or an incident into ranked
action cards by rule. Every window, trigger, floor and cap it uses is a setting (tech lead S279: a threshold is a
setting, maker-checker and audited, with the S285 advisory value as the seeded default), never a code literal:

- ``evidence_window_min``: how far back the LLM ledger and the job failures are read (advisory: 30 minutes);
- ``changes_window_min``: how far back "what changed" reads the ops events and route versions (advisory: 2 hours);
- ``refused_trigger_calls``: refused account-level calls on one connection inside the window that open an
  "LLM access refused" signal although its circuit has not opened yet;
- ``throttled_trigger_calls``: throttled calls on one connection inside the window that open a throttle signal (its
  own value: quieting refusal signals never quiets throttle signals);
- ``spend_spike_factor``: spend per hour on a connection against its usual hourly median that opens a cost signal;
- ``spend_baseline_days``: the days of that usual hourly median (advisory: 7), also the "if we do nothing" baseline;
- ``spend_spike_floor_usd_h``: spend per hour at or below which no spike signal opens (its own value, not the R1
  money cap);
- ``wide_scope_stages``: an action on a connection or a route that serves at least this many stages is wide (R3);
- ``small_money_usd``: the most an action may cost and still count as "a few cents" (R1); above it is paid (R3);
- ``history_min_n``: a "right k of n" confidence is shown only from this many measured outcomes (advisory: 5);
- ``card_ttl_min``: a card older than this is locked and the item must be analysed again (advisory: 30 minutes);
- ``reason_min_chars``: the shortest reason an R3 card asks for (tech lead AI7: 20 characters).

A1 (CIS 486, the model ranks and explains the cards):

- ``auto_severities``: the severities that get one automatic quick analysis per incident (AI6: sev1, sev2; empty =
  none), a list of strings;
- ``auto_incident_gap_min``: minutes an item must stay unseen by the automatic analysis before it counts as a new
  incident (never shorter than ``card_ttl_min``);
- ``deep_model``: the catalog model key of "Deep analysis" (AI2: sonnet-5-5), a string;
- ``lookup_windows_min``: the windows (minutes) the "what changed" look-up offers, ascending, each once.

``shared.settings.get`` serves the last snapshot and the compiled default before the first refresh, so a read never
blocks and never fails. The keys equal the CIS 481 and 486 seeds (tests/integration/test_aa1154_migration_481.py,
test_aa1154_migration_486.py and the settings-lint reader check, tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

from shared import settings

EVIDENCE_WINDOW_MIN = "cis.ops.assist_evidence_window_min"
CHANGES_WINDOW_MIN = "cis.ops.assist_changes_window_min"
REFUSED_TRIGGER_CALLS = "cis.ops.assist_refused_trigger_calls"
THROTTLED_TRIGGER_CALLS = "cis.ops.assist_throttled_trigger_calls"
SPEND_SPIKE_FACTOR = "cis.ops.assist_spend_spike_factor"
SPEND_BASELINE_DAYS = "cis.ops.assist_spend_baseline_days"
SPEND_SPIKE_FLOOR_USD_H = "cis.ops.assist_spend_spike_floor_usd_h"
WIDE_SCOPE_STAGES = "cis.ops.assist_wide_scope_stages"
SMALL_MONEY_USD = "cis.ops.assist_small_money_usd"
HISTORY_MIN_N = "cis.ops.assist_history_min_n"
CARD_TTL_MIN = "cis.ops.assist_card_ttl_min"
REASON_MIN_CHARS = "cis.ops.assist_reason_min_chars"
# A1 (CIS 486)
AUTO_SEVERITIES = "cis.ops.assist_auto_severities"
AUTO_INCIDENT_GAP_MIN = "cis.ops.assist_auto_incident_gap_min"
DEEP_MODEL = "cis.ops.assist_deep_model"
LOOKUP_WINDOWS_MIN = "cis.ops.assist_lookup_windows_min"

A0 = (EVIDENCE_WINDOW_MIN, CHANGES_WINDOW_MIN, REFUSED_TRIGGER_CALLS, THROTTLED_TRIGGER_CALLS, SPEND_SPIKE_FACTOR,
      SPEND_BASELINE_DAYS, SPEND_SPIKE_FLOOR_USD_H, WIDE_SCOPE_STAGES, SMALL_MONEY_USD, HISTORY_MIN_N, CARD_TTL_MIN,
      REASON_MIN_CHARS)
A1 = (AUTO_SEVERITIES, AUTO_INCIDENT_GAP_MIN, DEEP_MODEL, LOOKUP_WINDOWS_MIN)
ALL = A0 + A1

# The values kept as float; every other one is a count, minutes or days (int).
FLOATS = ("spend_spike_factor", "spend_spike_floor_usd_h", "small_money_usd")
# The values that are not numbers: a list of strings, a list of minutes, a string.
STR_LISTS = ("auto_severities",)
INT_LISTS = ("lookup_windows_min",)
STRINGS = ("deep_model",)

# The names the rules use; ``snapshot()`` maps them to their current values.
NAMES = {
    "evidence_window_min": EVIDENCE_WINDOW_MIN,
    "changes_window_min": CHANGES_WINDOW_MIN,
    "refused_trigger_calls": REFUSED_TRIGGER_CALLS,
    "throttled_trigger_calls": THROTTLED_TRIGGER_CALLS,
    "spend_spike_factor": SPEND_SPIKE_FACTOR,
    "spend_baseline_days": SPEND_BASELINE_DAYS,
    "spend_spike_floor_usd_h": SPEND_SPIKE_FLOOR_USD_H,
    "wide_scope_stages": WIDE_SCOPE_STAGES,
    "small_money_usd": SMALL_MONEY_USD,
    "history_min_n": HISTORY_MIN_N,
    "card_ttl_min": CARD_TTL_MIN,
    "reason_min_chars": REASON_MIN_CHARS,
    "auto_severities": AUTO_SEVERITIES,
    "auto_incident_gap_min": AUTO_INCIDENT_GAP_MIN,
    "deep_model": DEEP_MODEL,
    "lookup_windows_min": LOOKUP_WINDOWS_MIN,
}


def snapshot() -> dict:
    """{name: value} of every assistant limit, read once per analysis so one set of cards uses one set of limits.
    Numbers as float, except the counts and minutes (int); the lists ascending, each value once; the model key a
    string."""
    out: dict = {}
    for name, key in NAMES.items():
        v = settings.get(key)
        if name in STR_LISTS:
            out[name] = sorted({str(x) for x in v or []})
        elif name in INT_LISTS:
            out[name] = sorted({int(x) for x in v or []})
        elif name in STRINGS:
            out[name] = str(v or "")
        else:
            out[name] = float(v) if name in FLOATS else int(float(v))
    return out


def keys() -> dict:
    """{name: settings key}, for the links the page draws next to a value."""
    return dict(NAMES)
