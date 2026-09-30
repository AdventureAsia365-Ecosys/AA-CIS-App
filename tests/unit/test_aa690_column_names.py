"""AA-690 A0-5 (S206) — header spelling no longer drops a column (6 of 12 supplier files lost two)."""
from services.ingestion.excel_parser import COLUMN_MAP
from shared.llm_client.column_mapper import build_dynamic_column_map, normalize_header


def test_normalize_header():
    assert normalize_header("best_time_to_go") == "best time to go"
    assert normalize_header("Group-Size") == "group size"
    assert normalize_header("PRICE (USD)") == "price"
    assert normalize_header("  Itineraries ") == "itineraries"


def test_real_supplier_headers_all_map():
    # header row of the NEPAL / JAPAN / THAILAND / LAOS / INDIA / TAIWAN uploads (S206 measurement)
    cols = ["Tour ID", "SKU", "Country", "Name", "Subtitle", "Duration", "group_size", "Period", "Summary",
            "Description", "Highlights", "Itineraries", "Inclusions", "Exclusions", "Provider", "Price",
            "Links", "Activities", "best_time_to_go"]
    final, llm_used = build_dynamic_column_map(cols, COLUMN_MAP)
    assert not llm_used
    assert final["group_size"] == "group_size" and final["best_time_to_go"] == "best_time_to_go"
    assert len(final) == len(cols)


def test_price_with_unit_and_keys_stay_lowercased_originals():
    final, _ = build_dynamic_column_map(["Name", "Itinerary", "PRICE (USD)", "Mystery col"], COLUMN_MAP)
    assert final == {"name": "src_name", "itinerary": "src_itineraries", "price (usd)": "price_raw"}
