"""Index math for pages."""


def page_bounds(page, per_page):
    """Return (start, end) slice indexes for a 1-based page number."""
    start = page * per_page
    return start, start + per_page
