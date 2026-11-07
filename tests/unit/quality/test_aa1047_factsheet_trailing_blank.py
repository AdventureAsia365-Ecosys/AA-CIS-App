"""CodeQL py/redos: the trailing blank-line strip of the FactSheet budget render is linear and keeps the old result."""
import itertools
import re
import time

from services.quality.factsheet import _strip_trailing_blank_lines

OLD = re.compile(r"(\n\s*\n)+$")


def test_same_result_as_the_old_regex_on_every_short_whitespace_tail():
    for n in range(0, 6):
        for tail in itertools.product("\n \t", repeat=n):
            for head in ("", "a", "a\n- b", "x  "):
                text = head + "".join(tail)
                assert _strip_trailing_blank_lines(text) == OLD.sub("", text), repr(text)


def test_linear_on_a_hostile_input():
    text = "\n" + "\n\n" * 50000 + "x"
    start = time.monotonic()
    assert _strip_trailing_blank_lines(text) == text
    assert time.monotonic() - start < 0.5
