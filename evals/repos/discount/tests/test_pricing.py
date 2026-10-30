from shop.pricing import apply_discount


def test_no_discount():
    assert apply_discount(80, 0) == 80


def test_percent_discount():
    assert apply_discount(80, 25) == 60
