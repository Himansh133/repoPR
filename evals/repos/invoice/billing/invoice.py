"""Invoice totals."""

from billing.money import round_cents


def line_total(unit_price, quantity, tax_rate):
    """Price of one invoice line including tax, rounded to cents."""
    return round_cents(unit_price * quantity * (1 + tax_rate))
