import pandas as pd
import json
import math
from datetime import date, datetime
import structlog
from typing import Any
from shared.llm_client.column_mapper import build_dynamic_column_map
from shared.country_resolver import resolve_country

logger = structlog.get_logger()

COLUMN_MAP = {
    "tour id":            "tour_id_external",
    "sku":                "sku",
    "country":            "country",
    "region":             "region",
    "name":               "src_name",
    "subtitle":           "src_subtitle",
    "duration":           "duration",
    "group size":         "group_size",
    "period":             "period",
    "summary":            "src_summary",
    "description":        "src_description",
    "highlights":         "src_highlights",
    "itineraries":        "src_itineraries",
    "itinerary":          "src_itineraries",
    "itinerary_summary":  "src_itineraries",
    "trip_type":          "trip_type",
    "price_usd":          "price_raw",
    "tour_id":            "tour_id_external",
    "inclusions":         "inclusions",
    "exclusions":         "exclusions",
    "includes":           "inclusions",
    "excludes":           "exclusions",
    "provider":           "provider",
    "price":              "price_raw",
    "links":              "links",
    "activities":         "activities",
    "feature":            "feature",
    "best time to go":    "best_time_to_go",
    "source_name":        "src_name",
    "source_subtitle":    "src_subtitle",
    "source_summary":     "src_summary",
    "source_highlights":  "src_highlights",
    "source_itineraries": "src_itineraries",
    "aa_name":            "src_name",
    "aa_subtitle":        "src_subtitle",
    "aa_summary":         "src_summary",
    "aa_highlights":      "src_highlights",
    "aa_itineraries":     "src_itineraries",
    "seo_title":          "seo_title",
    "seo_meta":           "seo_meta",
}

# AA_* columns are AI-rewritten OUTPUT, never a source of truth for src_* fields — they must never
# win over (or silently stand in for) a real source column, regardless of column order in the file.
_AA_ALIAS_COLUMNS = {"aa_name", "aa_subtitle", "aa_summary", "aa_highlights", "aa_itineraries"}

# Columns that only ever appear in an exported/reviewed file (never a raw supplier upload). Presence
# of any of these means this is not a file we should ingest as new source data. AA_ITINERARIES is
# deliberately NOT in this set — it's handled by _AA_ALIAS_COLUMNS priority resolution below instead,
# so a legitimate raw file carrying both SOURCE_ITINERARIES and AA_ITINERARIES still ingests correctly
# (Nghiep decision, AA-343: reject-on-AA_ITINERARIES would make it impossible to ever ingest that
# combination, whereas the priority fix alone already fully closes the corruption vector).
_EXPORT_FILE_MARKERS = {"audit_status", "publish_ready", "fields_updated"}
_EXPORT_FILE_PREFIX = "dfs_"


def group_size_from_cell(value: Any) -> Any:
    """AA-690 (S206): Excel turns a group-size range typed as "2-10" into a date (2026-02-10), and the
    cell then reaches us as a datetime. Measured: 87 of 177 group_size values in 6 supplier files were
    dates. Both parts of such a range are <= 12 (else Excel keeps it as text), so the range is the
    month and the day, smaller first: 2026-02-10 → "2-10", 2026-12-01 → "1-12". Anything else is
    returned unchanged."""
    if isinstance(value, (datetime, date)):
        low, high = sorted((value.month, value.day))
        return f"{low}-{high}"
    return value


class ExcelParser:
    def __init__(self, file_path: str, source_file: str = None):
        self.file_path = file_path
        self.source_file = source_file

    def parse(self) -> list[dict]:
        # Try header=0 first; if columns look like group labels, use header=1
        df = pd.read_excel(self.file_path, engine="openpyxl", header=0)
        first_cols = [str(c).strip().lower() for c in df.columns]
        group_labels = {"identity", "source", "final content", "seo", "audit", "dfs context"}
        if any(c in group_labels for c in first_cols):
            df = pd.read_excel(self.file_path, engine="openpyxl", header=1)

        # Reject exported/reviewed files outright — they carry AA_* + audit columns that must
        # never be re-ingested as new source data (would re-corrupt src_* with AI output).
        excel_columns = [str(c).strip() for c in df.columns]
        lower_cols = {c.lower() for c in excel_columns}
        export_hits = sorted(
            c for c in lower_cols
            if c in _EXPORT_FILE_MARKERS or c.startswith(_EXPORT_FILE_PREFIX)
        )
        if export_hits:
            raise ValueError(
                f"Refusing to ingest '{self.source_file}': this looks like an exported/reviewed "
                f"file, not a raw supplier file (found export-only column(s): {export_hits}). "
                "Upload the original source file instead."
            )

        # Build column map — static first, LLM fallback if needed
        active_map, llm_used = build_dynamic_column_map(excel_columns, COLUMN_MAP)

        if llm_used:
            logger.info("llm_column_map_applied",
                        file=self.source_file,
                        mapped_cols=len(active_map))
        else:
            logger.info("static_column_map_applied",
                        file=self.source_file,
                        matched_cols=len(active_map))

        # Build col_name → db_field lookup using active_map
        # active_map keys are lowercase excel col names
        col_lookup = {}
        for col in df.columns:
            lower = str(col).strip().lower()
            if lower in active_map:
                col_lookup[str(col)] = active_map[lower]

        # Detect itineraries/name columns for row-grouping. Must prefer real/source columns over
        # AA_* aliases the same way the per-row assignment below does — otherwise an AA_ITINERARIES
        # column that happens to sort before SOURCE_ITINERARIES in the file would drive continuation-
        # row concat off AI output instead of the real source text.
        itin_col = self._first_real_col(col_lookup, "src_itineraries")
        name_col = self._first_real_col(col_lookup, "src_name")

        records = []
        current = None
        for _, row in df.iterrows():
            has_name = bool(self._clean(row.get(name_col)) if name_col else False)

            if has_name:
                if current:
                    records.append(current)
                current = {}
                # Real/source columns always win — write these first, order-independent.
                for excel_col, db_field in col_lookup.items():
                    if str(excel_col).strip().lower() in _AA_ALIAS_COLUMNS:
                        continue
                    value = row.get(excel_col)
                    if db_field == "group_size":
                        value = group_size_from_cell(value)
                    current[db_field] = self._clean(value)
                # AA_* columns never seed src_* fields, even when they're the only column
                # present for that field — flag it instead of silently borrowing AI output.
                for excel_col, db_field in col_lookup.items():
                    if str(excel_col).strip().lower() not in _AA_ALIAS_COLUMNS:
                        continue
                    if current.get(db_field) is not None:
                        continue
                    if self._clean(row.get(excel_col)) is not None:
                        logger.warning("aa_alias_column_ignored",
                                        file=self.source_file, column=excel_col, db_field=db_field)
                    current.setdefault(db_field, None)
                current["source_file"] = self.source_file
                _raw_country = current.get("country")
                _resolved_country = resolve_country(_raw_country, self.source_file)
                if _raw_country and not _resolved_country:
                    # AA-571 Việc 2B (Cách A, Nghiep confirmed): does NOT block ingestion --
                    # the tour still comes in normally, country still ends up NULL (unchanged)
                    # -- but the raw value is preserved in country_raw_unresolved (migration
                    # 151) so these rows are directly queryable later, plus a log line so it's
                    # visible at ingest time too, not just via a later query.
                    logger.warning("country_unresolved_null",
                                    file=self.source_file, raw_country=_raw_country,
                                    tour_name=current.get("src_name"))
                    current["country_raw_unresolved"] = _raw_country
                current["country"] = _resolved_country
                current["raw_data"] = json.dumps(row.to_dict(), default=str)
            else:
                # Continuation row — concat itineraries only
                if current and itin_col:
                    extra = self._clean(row.get(itin_col))
                    if extra:
                        existing = current.get("src_itineraries") or ""
                        current["src_itineraries"] = (existing + "\n" + extra).strip()

        if current and current.get("src_name"):
            records.append(current)

        logger.info("excel_parse_complete",
                    file=self.source_file,
                    records=len(records),
                    llm_used=llm_used)
        return records

    @staticmethod
    def _first_real_col(col_lookup: dict, db_field: str) -> str | None:
        return next(
            (col for col, db in col_lookup.items()
             if db == db_field and str(col).strip().lower() not in _AA_ALIAS_COLUMNS),
            None
        )

    def _clean(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        cleaned = str(value).strip()
        return cleaned if cleaned and cleaned.lower() != "nan" else None

# Country keyword map — dùng cho auto-detect từ filename
_COUNTRY_KEYWORDS = {
    "srilanka": "Sri Lanka", "sri_lanka": "Sri Lanka", "lanka": "Sri Lanka",
    "vietnam": "Vietnam", "viet_nam": "Vietnam",
    "thailand": "Thailand", "thai": "Thailand",
    "indonesia": "Indonesia", "bali": "Indonesia",
    "japan": "Japan",
    "korea": "South Korea", "south_korea": "South Korea",
    "nepal": "Nepal",
    "india": "India",
    "cambodia": "Cambodia",
    "myanmar": "Myanmar",
    "malaysia": "Malaysia",
    "singapore": "Singapore",
    "philippines": "Philippines",
    "laos": "Laos",
}

def detect_country_from_filename(filename: str) -> str | None:
    """Extract country từ filename nếu khớp keyword — fallback khi Excel thiếu country column."""
    if not filename:
        return None
    name = filename.split("/")[-1].lower().replace("-", "_").replace(" ", "_")
    # Remove extension
    name = name.rsplit(".", 1)[0]
    for keyword, country in _COUNTRY_KEYWORDS.items():
        if keyword in name:
            return country
    return None
