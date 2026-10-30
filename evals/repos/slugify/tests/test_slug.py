from textkit.slug import slugify


def test_simple_title():
    assert slugify("Hello World") == "hello-world"


def test_punctuation_and_repeated_spaces_are_dropped():
    assert slugify("  Hello,   World!  It's 2026 ") == "hello-world-its-2026"
