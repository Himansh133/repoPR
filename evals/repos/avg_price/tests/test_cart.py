from shop.cart import average_price, total


def test_total():
    assert total([1, 2, 3]) == 6


def test_average_price_of_items():
    assert average_price([2, 4]) == 3


def test_average_price_of_empty_cart_is_zero():
    assert average_price([]) == 0
