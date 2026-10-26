from textstats import count_words, longest_word


def test_count_words_simple():
    assert count_words("one two three") == 3


def test_count_words_ignores_extra_spaces():
    assert count_words("  one   two  ") == 2


def test_longest_word():
    assert longest_word("a bbb cc") == "bbb"
