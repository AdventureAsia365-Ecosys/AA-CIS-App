"""CodeQL py/redos: the trailing blank-line strip of the FactSheet budget render is linear and keeps the old result
(the old code was re.sub on "one or more newline-whitespace-newline groups at the end"; the expected values below were
taken from it, so the regex itself is not kept here)."""
import time

import pytest

from services.quality.factsheet import _strip_trailing_blank_lines


@pytest.mark.parametrize("text, expected", [
    ("", ""),
    ("a", "a"),
    ("a\n", "a\n"),
    ("a\n\n", "a"),
    ("a\n \n", "a"),
    ("a\n\n\n", "a"),
    ("a \n\n", "a "),
    ("a\t\n \t\n", "a\t"),
    ("a\n\n ", "a\n\n "),
    ("a\n- b\n\n", "a\n- b"),
    ("x  \n \n\n", "x  "),
    ("\n\n", ""),
    (" \n", " \n"),
    ("a\n \n x\n\n", "a\n \n x"),
])
def test_same_result_as_the_old_regex(text, expected):
    assert _strip_trailing_blank_lines(text) == expected


def test_linear_on_a_hostile_input():
    text = "\n" + "\n\n" * 50000 + "x"
    start = time.monotonic()
    assert _strip_trailing_blank_lines(text) == text
    assert time.monotonic() - start < 0.5
