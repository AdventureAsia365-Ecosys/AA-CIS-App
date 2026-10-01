"""AA-708 — match a Drive photo to a tour (its folder name) and a destination (its file name). Pure.

Folder layout on the CON board: country folder → one folder per tour → images named by place
("Olkhon Island1.jpeg", "Gyeongbokgung Palace (2).jpg"). Ambiguous matches stay unmatched; the
admin Photos page assigns them by hand.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

_EXT = re.compile(r"\.[a-z0-9]{2,5}$", re.IGNORECASE)
_TRAILING = re.compile(r"(?:[\s_\-]*(?:\(\d+\)|copy|\d+))+$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")
TOUR_RATIO = 0.85
TOKEN_SHARE = 0.9
# Real CON folder names (01/10/2026): "Best of Bhutan - 9 days", "Dabajianshan Trek 3 Day / 2 Night
# (Guided)", "GB-01 MANILA and SUBURBS - 4 hours", "Beijing, Xi'an and Shanghai — Nine Days".
_CODE = re.compile(r"^\s*[A-Z]{2,4}\s*[-–]\s*\d+\s*")
_PAREN = re.compile(r"\((?:guided|private|small group|self[- ]guided)[^)]*\)", re.IGNORECASE)
_DURATION = re.compile(
    r"\s*[-–—:|,]?\s*\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen)\s*-?\s*(?:days?|nights?|hours?|hrs?)\b.*$", re.IGNORECASE)


def norm(text: str) -> str:
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def place_label(file_name: str) -> str:
    """"Olkhon Island1.jpeg" -> "Olkhon Island"; "Bukchon_Hanok-Village (2).JPG" -> "Bukchon Hanok Village"."""
    base = _EXT.sub("", (file_name or "").strip())
    base = _TRAILING.sub("", base)
    return " ".join(re.sub(r"[_\-]+", " ", base).split())


def tour_title(name: str) -> str:
    """A folder or tour name without its code, "(Guided)" and duration tail."""
    n = _PAREN.sub("", _CODE.sub("", (name or "").strip()))
    short = _DURATION.sub("", n)
    return (short if len(norm(short)) >= 4 else n).strip(" -–—:,")


def _token_share(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def match_tour(folder: Optional[str], tours: Iterable[tuple[str, ...]]) -> Optional[str]:
    """tour_id named by the folder. `tours` rows are (tour_id, name, *other names) — the raw name and,
    once written, the rewritten aa_name. Exact title wins; else one clear best by similarity, or all
    words of the shorter title shared (≥ 3 words)."""
    f = norm(tour_title(folder or ""))
    if len(f) < 4:
        return None
    best: dict[str, float] = {}
    for tid, *names in tours:
        for name in names:
            n = norm(tour_title(name or ""))
            if not n:
                continue
            if n == f:
                return tid
            score = SequenceMatcher(None, f, n).ratio()
            if min(len(f.split()), len(n.split())) >= 3 and _token_share(f, n) >= TOKEN_SHARE:
                score = max(score, TOUR_RATIO)
            best[tid] = max(best.get(tid, 0.0), score)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if ranked and ranked[0][1] >= TOUR_RATIO and (len(ranked) == 1 or ranked[1][1] < ranked[0][1] - 0.05):
        return ranked[0][0]
    return None


def match_destination(label: str, destinations: Iterable[tuple[str, str]]) -> Optional[str]:
    """destination id named by the label: exact name, else the single longest destination name that
    appears as whole words inside the label ("Olkhon Island sunset" -> "Olkhon Island")."""
    lab = norm(label)
    if len(lab) < 3:
        return None
    padded = f" {lab} "
    best: list[tuple[int, str]] = []
    for did, name in destinations:
        n = norm(name)
        if not n:
            continue
        if n == lab:
            return did
        if len(n) >= 4 and f" {n} " in padded:
            best.append((len(n), did))
    if not best:
        return None
    best.sort(reverse=True)
    if len(best) > 1 and best[1][0] == best[0][0]:
        return None
    return best[0][1]
