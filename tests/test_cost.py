"""Tests for the cost calculator."""

from decimal import Decimal

from app.llm import calculate_cost


def test_exact_cost():
    # (1000 * 2 + 500 * 8) / 1,000,000 = 0.006
    assert calculate_cost(1000, 500, Decimal("2"), Decimal("8")) == Decimal("0.006")


def test_zero_tokens():
    assert calculate_cost(0, 0, Decimal("2"), Decimal("8")) == Decimal("0")


def test_one_million_tokens_equals_price():
    assert calculate_cost(1_000_000, 0, Decimal("3.5"), Decimal("9")) == Decimal("3.5")
    assert calculate_cost(0, 1_000_000, Decimal("3.5"), Decimal("9")) == Decimal("9")


def test_returns_none_when_price_unknown():
    assert calculate_cost(1000, 500, None, Decimal("8")) is None
    assert calculate_cost(1000, 500, Decimal("2"), None) is None
