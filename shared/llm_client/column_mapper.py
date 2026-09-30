"""
P2-S7: LLM Column Auto-detect
When Excel columns don't match COLUMN_MAP (< 50% hit rate),
use LLM to map arbitrary column names to raw_tours schema fields.

AA-685: the call goes through LLMClient with stage "a0_column_map" (model from the stage route,
seeded to Haiku — the model this file hardcoded before) and writes a shared.llm_call_log row.
"""
import json
import re
import structlog

from .call_log import record_call_sync
from .client import LLMClient
from .models import LLMRequest

logger = structlog.get_logger()

STAGE = "a0_column_map"

# Target schema fields with descriptions
TARGET_FIELDS = {
    "src_name":         "Tour name or title (REQUIRED)",
    "subtitle":         "Short subtitle or tagline",
    "src_summary":      "Summary or overview paragraph",
    "src_description":  "Full description",
    "src_highlights":   "Highlights or key features (list)",
    "src_itineraries":  "Day-by-day itinerary",
    "country":          "Country name",
    "duration":         "Trip duration (e.g. 7 days)",
    "group_size":       "Group size or pax",
    "period":           "Best travel period or season",
    "price_raw":        "Price in USD or raw price string",
    "inclusions":       "What is included",
    "exclusions":       "What is not included",
    "provider":         "Tour operator or provider name",
    "tour_id_external": "External tour ID or reference code",
    "sku":              "SKU or product code",
    "activities":       "Activities or activity types",
    "best_time_to_go":  "Best time to visit",
    "links":            "URLs or reference links",
}


def detect_column_mapping(excel_columns: list[str]) -> dict[str, str]:
    """
    Use LLM to map Excel column names → raw_tours schema fields.
    Returns dict: {excel_column: schema_field}
    Only includes high-confidence mappings.
    """
    target_desc = "\n".join([
        f"  - {field}: {desc}"
        for field, desc in TARGET_FIELDS.items()
    ])

    prompt = f"""You are a data engineer mapping Excel columns to a database schema.

Excel columns from supplier file:
{json.dumps(excel_columns, indent=2)}

Target database fields:
{target_desc}

Task: Map each Excel column to the most appropriate database field.
Rules:
- Only map if you are confident (>80% sure)
- One Excel column maps to at most one database field
- Multiple Excel columns can map to the same field (take the best one)
- Skip columns that don't match any field
- src_name is REQUIRED — always try to find it

Respond ONLY with a JSON object like:
{{"excel_column_name": "db_field_name", ...}}

No explanation, no markdown, just the JSON object."""

    resp = None
    try:
        resp = LLMClient().generate(LLMRequest(
            system_prompt="You map spreadsheet columns to database fields. Reply with JSON only.",
            user_prompt=prompt, max_tokens=500, stage=STAGE,
        ))
        raw = resp.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        mapping = json.loads(raw)

        # Validate — only keep mappings to known target fields
        valid = {
            col: field
            for col, field in mapping.items()
            if field in TARGET_FIELDS and col in excel_columns
        }

        logger.info("llm_column_mapping",
                    input_cols=len(excel_columns),
                    mapped=len(valid),
                    mapping=valid)
        _log_call(resp, json_parsed=True, mapped=len(valid))
        return valid

    except Exception as e:
        logger.error("llm_column_mapping_failed", error=str(e))
        if resp is not None:
            _log_call(resp, json_parsed=False, mapped=0)
        return {}


def _log_call(resp, *, json_parsed: bool, mapped: int) -> None:
    record_call_sync(
        stage=STAGE, role="writer", model=resp.model_used,
        tokens_in=resp.input_tokens, tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
        quality_signal={"json_parsed": json_parsed, "mapped_columns": mapped},
        stop_reason=resp.stop_reason, account=resp.satellite_account,
        fallback_used=resp.fallback_used,
    )


def normalize_header(name: str) -> str:
    """'PRICE (USD)' → 'price', 'best_time_to_go' → 'best time to go', 'Group-Size' → 'group size'."""
    n = re.sub(r"\([^)]*\)", " ", str(name).lower())
    n = re.sub(r"[_\-/.:]+", " ", n)
    return " ".join(n.split())


def build_dynamic_column_map(
    excel_columns: list[str],
    static_map: dict[str, str],
) -> tuple[dict[str, str], bool]:
    """
    Try static COLUMN_MAP first. If hit rate < 50%, use LLM.
    Returns (final_map, llm_used).
    final_map: {excel_col_lower: db_field}
    """
    lower_cols = [c.strip().lower() for c in excel_columns]
    # AA-690 A0-5 (S206): match on a normalised name, so "group_size" / "Group-Size" / "PRICE (USD)" hit the
    # "group size" / "price" entries. Measured on the uploaded files: group_size and best_time_to_go were
    # silently dropped in 6 of 12 supplier files, and the LLM fallback below had never been triggered.
    norm_static = {normalize_header(k): v for k, v in static_map.items()}
    matched = {c: norm_static[normalize_header(c)] for c in lower_cols if normalize_header(c) in norm_static}

    # Count static hits
    hits = len(matched)
    hit_rate = hits / len(lower_cols) if lower_cols else 0

    logger.info("column_map_check",
                total=len(lower_cols),
                hits=hits,
                hit_rate=round(hit_rate, 2))

    if hit_rate >= 0.3:
        # Static map sufficient — build lower→field map
        unmatched = [c for c in excel_columns if c.strip().lower() not in matched
                     and not c.strip().lower().startswith("unnamed")]
        if unmatched:
            logger.info("column_map_unmatched", columns=unmatched)
        return matched, False

    # LLM fallback
    logger.info("llm_fallback_triggered", hit_rate=hit_rate)
    llm_mapping = detect_column_mapping(excel_columns)

    # Convert to lower-keyed map
    final = {col.strip().lower(): field for col, field in llm_mapping.items()}
    return final, True
