"""URL slugs."""


def slugify(title):
    """Turn a title into a URL slug: lowercase words joined by single hyphens."""
    return title.lower().replace(" ", "-")
