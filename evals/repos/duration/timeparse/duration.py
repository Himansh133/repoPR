"""Parse durations like '1h30m' or '45s'."""

import re

UNITS = {"m": 60, "s": 1}


def parse_duration(text):
    """Return the number of seconds in a duration string such as '2h15m10s'."""
    total = 0
    for amount, unit in re.findall(r"(\d+)([ms])", text):
        total += int(amount) * UNITS[unit]
    return total
