from pager.listing import get_page, page_count


def test_page_count():
    assert page_count(list(range(25)), per_page=10) == 3


def test_first_page_starts_at_first_item():
    assert get_page(list(range(25)), 1, per_page=10) == list(range(10))
