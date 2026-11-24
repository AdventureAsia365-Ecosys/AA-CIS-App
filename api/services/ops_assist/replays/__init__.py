"""AA-1154 (A0) — recorded incidents the assistant's rules replay to the same cards (Done-when A0, and later the A2
replay set of the S285 advisory section 6).

Each case is one JSON file here: ``title``, ``what_happened`` (with the postmortem it comes from), ``expected`` (what
the cards must show: the first card's action, the actions that must be there and the ones that must not) and
``snapshot``, an evidence snapshot in the shape of :func:`api.services.ops_assist.evidence.collect`. A snapshot holds
only facts from the repo (connection keys, model and stage keys, counts and times); no database row text.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _files() -> dict[str, Path]:
    return {p.stem: p for p in HERE.glob("*.json")}


def cases() -> list[str]:
    return sorted(_files())


def load(case: str) -> dict:
    # The path comes from the folder listing, never from the caller's text: an unknown case is a KeyError.
    path = _files().get(case)
    if path is None:
        raise KeyError(case)
    return json.loads(path.read_text(encoding="utf-8"))


def index() -> list[dict]:
    out = []
    for c in cases():
        d = load(c)
        out.append({"case": c, "title": d.get("title"), "what_happened": d.get("what_happened"),
                    "source": d.get("source")})
    return out
