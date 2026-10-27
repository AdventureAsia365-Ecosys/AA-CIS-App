"""S252 (AA-990 sweep) — the Browse Pool duration filter on real PostgreSQL: the old filter concatenated every digit
of raw_tours.duration and overflowed INTEGER on strings like "3 hours\\n9:30am-12:30pm" (HTTP 500 on any
duration_min / duration_max request). The new expression reads the number before "day(s)" (as the catalog views do)
and counts hour-based / half- or full-day products as 1 day."""
import pytest

from api.routers.v1_tours import _DURATION_DAYS_SQL

CASES = [
    ("7 days 6 nights", 7),
    ("14 Days", 14),
    ("10-day", 10),
    ("3 hours\n9:30am-12:30pm\n1:30pm-4:30pm", 1),
    ("3 hours\n17km/10.6miles\n9:30am-12:30pm\n1:30pm-4:30pm", 1),
    ("HALF DAY", 1),
    ("DAY TRIP", 1),
    ("same day", 1),
    ("", None),
    (None, None),
]


@pytest.mark.parametrize("duration,days", CASES)
def test_duration_days_expression(db_conn, duration, days):
    cur = db_conn.cursor()
    cur.execute(f"SELECT {_DURATION_DAYS_SQL} FROM (SELECT %s::text AS duration) rt", (duration,))
    assert cur.fetchone()[0] == days


def test_no_input_overflows_integer(db_conn):
    cur = db_conn.cursor()
    cur.execute(f"SELECT {_DURATION_DAYS_SQL} <= 3 FROM (SELECT %s::text AS duration) rt",
                ("1234567890123456789 hours 9:30am",))
    assert cur.fetchone()[0] is True
