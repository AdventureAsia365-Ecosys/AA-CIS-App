"""AA-1047 W1 (root ADR 0020 invariant 4) — the last text step of an S1 page, before the final contract.

``normalize_generated`` was ``api/routers/v1_pipeline._normalize_generated``, applied to the graph's output AFTER the
graph ended: the stored page was not the page the checks saw (a post-gate edit). It now runs inside the graph as the
first step of ``graph.final_contract_node`` (A1), so the contract checks and records the text that is stored, and
``_rewrite_tour``'s call is a no-op on it (the function is idempotent). T2 keeps the call in ``_rewrite_tour`` (its
gate, T3, runs after the graph). Moved byte for byte; ``v1_pipeline`` re-exports both names.

Pure: no DB, no LLM.
"""
from __future__ import annotations

import re


def normalize_generated(generated: dict, tour: dict) -> dict:
    """Post-process LLM output: title-case name, strip forbidden words from name."""
    if not generated:
        return generated
    # Title-case name if ALL-CAPS (preserve if already mixed case)
    name = generated.get("name", "")
    if name and name == name.upper():
        generated["name"] = name.title()
    # Strip markdown bold from itineraries — ensure string type (GPT-4.1 may return dict)
    itin = generated.get("itineraries")
    if itin:
        if isinstance(itin, dict):
            parts = []
            for k, v in itin.items():
                parts.append(f"{k} -- {v}" if isinstance(v, str) else str(v))
            itin = "\n\n".join(parts)
        elif isinstance(itin, list):
            parts = []
            for item in itin:
                if isinstance(item, dict):
                    day   = item.get("day", "")
                    title = item.get("title", "")
                    desc  = item.get("description", "")
                    acts  = item.get("activities", [])
                    day_str = f"Day {day}"
                    if title:
                        day_str += f" — {title}"
                    if desc:
                        day_str += f"\n{desc}"
                    if acts:
                        act_list = ", ".join(str(a) for a in acts) if isinstance(acts, list) else str(acts)
                        day_str += f"\n*Activities: {act_list}*"
                    parts.append(day_str)
                else:
                    parts.append(str(item))
            itin = "\n\n---\n\n".join(parts)
        generated["itineraries"] = clean_itinerary(str(itin))
    # Same for highlights — ensure list of strings
    highlights = generated.get("highlights")
    if highlights and isinstance(highlights, list):
        generated["highlights"] = [str(h) if not isinstance(h, str) else h for h in highlights]
    return generated


def clean_itinerary(text: str) -> str:
    """Strip markdown bold markers (**) from itinerary — LLM sometimes adds them."""
    if not text:
        return text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = text.replace("**", "")
    return text.strip()
