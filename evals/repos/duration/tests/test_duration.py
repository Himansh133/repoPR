from timeparse.duration import parse_duration


def test_minutes_and_seconds():
    assert parse_duration("2m5s") == 125


def test_hours():
    assert parse_duration("1h30m") == 5400
