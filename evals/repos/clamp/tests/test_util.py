from util import clamp, is_even


def test_clamp_inside_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_above_range():
    assert clamp(50, 0, 10) == 10


def test_clamp_below_range_returns_low():
    assert clamp(-5, 0, 10) == 0


def test_is_even():
    assert is_even(4) and not is_even(5)
