"""Small numeric helpers."""


def clamp(value, low, high):
    """Return value limited to the range low..high."""
    if value > high:
        return high
    return value


def is_even(number):
    """Return True if the number is even."""
    return number % 2 == 0
