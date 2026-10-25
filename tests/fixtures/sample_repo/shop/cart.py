"""Cart helpers."""


def average_price(prices):
    """Return the average price of the items in the cart."""
    return sum(prices) / len(prices)


def total(prices):
    """Return the total price of the items in the cart."""
    return sum(prices)
