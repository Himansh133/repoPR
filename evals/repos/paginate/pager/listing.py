"""Split a list into pages."""

from pager.bounds import page_bounds


def get_page(items, page, per_page=10):
    """Return the items on a 1-based page."""
    start, end = page_bounds(page, per_page)
    return items[start:end]


def page_count(items, per_page=10):
    """Return how many pages the items fill."""
    return -(-len(items) // per_page)
