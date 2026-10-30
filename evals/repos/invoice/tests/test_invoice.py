from decimal import Decimal

from billing.invoice import line_total
from billing.money import round_cents


def test_line_total_without_tax():
    assert line_total(2.5, 4, 0) == 10.0


def test_half_cents_round_up():
    assert round_cents(2.675) == 2.68
    assert round_cents(Decimal("0.125")) == Decimal("0.13")
