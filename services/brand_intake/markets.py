"""Tenant markets as the tenant writes them ("Europe", "UAE", "North America") → the market codes
the pipeline reads from `shared.tenant_seo_config.target_market.countries` (AA-741, S229).

One source of truth: the Brand Identity form. Before, the form's `target_markets` was stored in
`tenant_brand_rules` and never read, while T2/T7 read `tenant_seo_config` — which no tenant could
write — so every tenant silently wrote for the US default.

Region mapping agreed with Nghiệp (S229): Europe → UK, DE, FR, NL; North America → US + CA.
Unsupported markets are kept as ISO-style codes in `countries`, so the existing AA-629 logic
(`seed_builder.unmatched_countries`) still shows the banner when NONE is supported, and they are
also listed in `unsupported` for the UI.
"""
from __future__ import annotations

import re

SUPPORTED = ("US", "UK", "AU", "DE", "FR", "NL")   # seed_builder.DFS_LOCATION_MAP keys

_ALIASES: dict[str, tuple[str, ...]] = {
    # supported
    "us": ("US",), "usa": ("US",), "united states": ("US",), "united states of america": ("US",),
    "america": ("US",),
    "uk": ("UK",), "gb": ("UK",), "united kingdom": ("UK",), "great britain": ("UK",),
    "britain": ("UK",), "england": ("UK",),
    "au": ("AU",), "australia": ("AU",),
    "de": ("DE",), "germany": ("DE",),
    "fr": ("FR",), "france": ("FR",),
    "nl": ("NL",), "netherlands": ("NL",), "the netherlands": ("NL",), "holland": ("NL",),
    # regions
    "europe": ("UK", "DE", "FR", "NL"), "western europe": ("UK", "DE", "FR", "NL"),
    "north america": ("US", "CA"),
    # unsupported today (kept so the tenant sees them and AA can see the demand)
    "uae": ("AE",), "ae": ("AE",), "united arab emirates": ("AE",), "dubai": ("AE",),
    "saudi arabia": ("SA",), "saudi": ("SA",), "ksa": ("SA",), "sa": ("SA",),
    "singapore": ("SG",), "sg": ("SG",),
    "hong kong": ("HK",), "hk": ("HK",),
    "canada": ("CA",), "ca": ("CA",),
    "new zealand": ("NZ",), "nz": ("NZ",),
}


def normalize_markets(declared: list[str] | None) -> dict:
    """{"declared", "countries", "supported", "unsupported", "unknown"} — order kept, deduped.
    `unknown` = entries we could not map at all (shown to the tenant as-is)."""
    countries: list[str] = []
    unknown: list[str] = []
    clean = [d.strip() for d in (declared or []) if d and d.strip()]
    for item in clean:
        key = re.sub(r"\s+", " ", item.lower().strip(" .;:"))
        codes = _ALIASES.get(key)
        if codes is None and len(key) == 2 and key.isalpha():
            codes = (key.upper(),)                 # a bare ISO code the tenant typed
        if codes is None:
            unknown.append(item)
            continue
        for c in codes:
            if c not in countries:
                countries.append(c)
    return {
        "declared": clean,
        "countries": countries,
        "supported": [c for c in countries if c in SUPPORTED],
        "unsupported": [c for c in countries if c not in SUPPORTED],
        "unknown": unknown,
    }


def target_market_json(declared: list[str] | None, language: str = "en") -> dict:
    """The `tenant_seo_config.target_market` value for these declared markets."""
    m = normalize_markets(declared)
    return {"countries": m["countries"], "language": language, "declared": m["declared"],
            "unsupported": m["unsupported"], "unknown": m["unknown"]}
