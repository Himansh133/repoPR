"""Merge user settings over defaults."""

DEFAULTS = {"server": {"host": "localhost", "port": 8000}, "debug": False}


def merge(base, override):
    """Return base with override applied, merging nested dicts."""
    result = base
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def load(user):
    return merge(DEFAULTS, user)
