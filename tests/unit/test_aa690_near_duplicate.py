"""AA-690 A0-2 — near-duplicate rule, on pairs measured in S206 (Dev raw_tours)."""
import asyncio

from services.ingestion import near_duplicate as nd

ITIN_A = ("Day 1 Pakse arrival transfer hotel. Day 2 Wat Phou temple Champasak river boat. "
          "Day 3 Bolaven plateau coffee waterfalls Tad Fane. Day 4 Four Thousand Islands Don Khone. "
          "Day 5 Khone Phapheng falls departure.")
ITIN_B = ("Day 1 Pakse arrival transfer hotel. Day 2 Wat Phou temple Champasak river boat. "
          "Day 3 Bolaven plateau coffee waterfalls Tad Fane. Day 4 Four Thousand Islands Don Khone. "
          "Day 5 Khone Phapheng falls departure airport.")
ITIN_OTHER = ("Day 1 Vientiane arrival. Day 2 Vang Vieng caves kayaking. Day 3 Luang Prabang alms giving "
              "monks. Day 4 Kuang Si falls. Day 5 Mekong cruise Pak Ou caves. Day 6 Plain of Jars.")


def _row(name, itin, duration="5 DAYS", provider="TIGER TRAILS TRAVEL", country="Laos", tour_id=None):
    return {"src_name": name, "src_itineraries": itin, "duration": duration, "provider": provider,
            "country": country, "tour_id": tour_id}


def test_same_product_reuploaded_is_a_duplicate():
    got = nd.classify(_row("SOUTHERN LAOS", ITIN_B, "5 days"), [_row("Southern Laos", ITIN_A, tour_id="t1")])
    assert got.duplicate is not None and got.duplicate.tour_id == "t1" and got.variant is None
    assert "skipped on Commit" in got.message()


def test_same_name_different_itinerary_is_a_variant_not_a_duplicate():   # Classic Laos 6 vs 10 days
    got = nd.classify(_row("SOUTHERN LAOS", ITIN_OTHER, "8 days"), [_row("SOUTHERN LAOS", ITIN_A)])
    assert got.duplicate is None and got.variant is not None and "variant" in got.note()


def test_same_itinerary_different_duration_is_not_a_duplicate():      # Golden Triangle 4 vs 5 days
    got = nd.classify(_row("SOUTHERN LAOS", ITIN_B, "6 days"), [_row("SOUTHERN LAOS", ITIN_A, "5 days")])
    assert got.duplicate is None and got.variant is not None


def test_other_country_or_no_provider_is_never_compared():
    assert nd.classify(_row("Southern Laos", ITIN_A, country="Thailand"), [_row("Southern Laos", ITIN_A)]) \
        .duplicate is None
    assert nd.classify(_row("Southern Laos", ITIN_A, provider=""), [_row("Southern Laos", ITIN_A, provider="")]) \
        .duplicate is None


def test_provider_normalised():
    assert nd.norm_provider("Horizon Voyages") == nd.norm_provider("horizon  voyages") == "horizonvoyages"
    assert nd.duration_days("8 days`") == 8 and nd.duration_days("DAY TRIP") is None


def test_checker_loads_provider_once_and_catches_in_file_duplicates():
    calls = []

    class Conn:
        async def fetch(self, sql, tenant, prov):
            calls.append(prov)
            return []

    async def run():
        ck = nd.Checker(Conn(), "tenant")
        first = await ck.check(_row("Southern Laos", ITIN_A))
        ck.accept(_row("Southern Laos", ITIN_A))
        second = await ck.check(_row("SOUTHERN LAOS", ITIN_B, "5 days"))
        return first, second

    first, second = asyncio.run(run())
    assert first.duplicate is None and second.duplicate is not None and calls == ["tigertrailstravel"]
