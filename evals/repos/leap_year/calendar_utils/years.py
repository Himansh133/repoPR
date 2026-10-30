"""Year helpers."""


def is_leap_year(year):
    """Return True if the year has 366 days in the Gregorian calendar."""
    return year % 4 == 0


def days_in_year(year):
    return 366 if is_leap_year(year) else 365
