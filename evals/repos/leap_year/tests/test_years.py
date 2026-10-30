from calendar_utils.years import days_in_year, is_leap_year


def test_ordinary_leap_year():
    assert is_leap_year(2024)
    assert not is_leap_year(2023)


def test_century_years():
    assert days_in_year(1900) == 365
    assert days_in_year(2000) == 366
