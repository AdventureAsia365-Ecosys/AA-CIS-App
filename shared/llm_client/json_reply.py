"""shared.llm_client.json_reply — read the JSON value out of a model reply (AA-1026).

A model asked for "JSON only" still sometimes wraps it: a ```json fence, a line of prose before it, or text AFTER
it (a note, a second JSON value). The S263 acc2 contract smoke failed `t9_faq_filter` on both acc2 models for that
last reason: Nova Lite and Llama 4 Scout answered with a complete verdict object followed by more text, and the
stage parser (first "{" to last "}", then json.loads) refused the whole reply with "Extra data".

`first_json_value` reads the FIRST complete JSON value and ignores what follows it. It never loosens what the value
must look like: the caller still checks its shape (an `items` list, an `atoms` list, one verdict per question ...).

    scan=False  decode at the first opening bracket only (the reply's own value). Use it where a repair step or a
                looser fallback follows the parse.
    scan=True   when that bracket is not valid JSON (a "{" in leading prose, say "for {n} questions"), try the next
                TOP-LEVEL bracket after its matching close. Brackets nested inside a candidate are never tried, and
                a candidate whose bracket is never closed (a truncated reply) ends the scan: a value nested inside a
                cut-off answer is never returned as the answer.

Fences need no stripping: the opening bracket after a fence is found as is, and the closing fence is trailing text.
"""
from __future__ import annotations

import json
from typing import Optional

MAX_STARTS = 50          # opening brackets tried at most (scan=True)
_OPENERS = {dict: "{", list: "["}


class JsonReplyError(ValueError):
    """The reply holds no complete JSON value of the wanted kind."""


def _next_open(s: str, chars: str, start: int) -> int:
    found = [i for i in (s.find(c, start) for c in chars) if i >= 0]
    return min(found) if found else -1


def _close_of(s: str, pos: int) -> int:
    """The index after the bracket that closes the one at `pos` (any bracket kind counts; JSON strings are skipped),
    or -1 when the text ends first."""
    depth, in_str, esc = 0, False, False
    for i in range(pos, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def first_json_value(raw: Optional[str], *, kinds: tuple = (dict,), scan: bool = True):
    """The first complete JSON value of a type in `kinds` (dict and / or list) in `raw`; text before its opening
    bracket and anything after its end are ignored. Raises JsonReplyError: 'no JSON object in the reply' when
    there is no opening bracket, else 'invalid JSON: <first decode error>'."""
    chars = "".join(_OPENERS[k] for k in kinds)
    s = raw or ""
    decoder = json.JSONDecoder()
    first_err: Optional[str] = None
    pos = _next_open(s, chars, 0)
    for _ in range(MAX_STARTS):
        if pos < 0:
            break
        try:
            value, after = decoder.raw_decode(s, pos)
        except json.JSONDecodeError as e:
            first_err = first_err or str(e)
            after = _close_of(s, pos)
            if not scan or after < 0:        # one try only, or the reply is cut off: nothing later is top level
                break
            pos = _next_open(s, chars, after)
            continue
        if isinstance(value, kinds):
            return value
        if not scan:
            break
        pos = _next_open(s, chars, after)
    if first_err is None:
        raise JsonReplyError("no JSON object in the reply")
    raise JsonReplyError(f"invalid JSON: {first_err}")


__all__ = ["JsonReplyError", "first_json_value", "MAX_STARTS"]
