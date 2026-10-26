"""Text statistics."""


def count_words(text):
    """Return how many words the text has. Words are separated by whitespace."""
    return len(text.split(" "))


def longest_word(text):
    """Return the longest word, or an empty string for empty text."""
    words = text.split()
    return max(words, key=len) if words else ""
