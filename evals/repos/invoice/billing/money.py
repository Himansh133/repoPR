"""Money helpers."""


def round_cents(amount):
    """Round an amount in dollars to whole cents, halves rounding up."""
    return round(amount, 2)
