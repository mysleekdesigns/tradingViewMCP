"""English ordinals in report warnings ("82nd percentile", never "82th")."""

from __future__ import annotations

import pytest

from lab.runner import ordinal


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0th"),
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (23, "23rd"),
        (82, "82nd"),
        (83, "83rd"),
        (100, "100th"),
        (101, "101st"),
        (111, "111th"),
        (112, "112th"),
    ],
)
def test_ordinal_suffix(n: int, expected: str) -> None:
    assert ordinal(n) == expected
