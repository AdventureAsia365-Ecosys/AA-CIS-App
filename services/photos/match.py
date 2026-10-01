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


def norm(text: str) -> str:
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def place_label(file_name: str) -> str:
    """"Olkhon Island1.jpeg" -> "Olkhon Island"; "Bukchon_Hanok-Village (2).JPG" -> "Bukchon Hanok Village"."""
    base = _EXT.sub("", (file_name or "").strip())
    base = _TRAILING.sub("", base)
    return " ".join(re.sub(r"[_\-]+", " ", base).split())


def match_tour(folder: Optional[str], tours: Iterable[tuple[str, str]]) -> Optional[str]:
    """tour_id whose src_name is the folder name (exact after normalising, else one clear fuzzy best)."""
    f = norm(folder or "")
    if len(f) < 4:
        return None
    scored: list[tuple[float, str]] = []
    for tid, name in tours:
        n = norm(name)
        if n == f:
            return tid
        scored.append((SequenceMatcher(None, f, n).ratio(), tid))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= TOUR_RATIO and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
        return scored[0][1]
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
