"""Tests for app.config.Settings."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


def _clear(monkeypatch):
    for name in (
        "GMI_API_KEY", "GMI_MODEL", "GMI_BASE_URL", "GITHUB_TOKEN",
        "PRICE_IN_PER_M", "PRICE_OUT_PER_M", "MODEL_PRICES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_loads_from_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "sk-secret")
    monkeypatch.setenv("GMI_MODEL", "some/model-id")
    monkeypatch.setenv("PRICE_IN_PER_M", "2.5")
    monkeypatch.setenv("PRICE_OUT_PER_M", "10")
    s = Settings(_env_file=None)
    assert s.gmi_model == "some/model-id"
    assert s.gmi_api_key.get_secret_value() == "sk-secret"
    assert s.price_in_per_m == Decimal("2.5")
    assert s.price_out_per_m == Decimal("10")


def test_defaults(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "k")
    monkeypatch.setenv("GMI_MODEL", "m")
    s = Settings(_env_file=None)
    assert s.gmi_base_url == "https://api.gmi-serving.com/v1"
    assert s.llm_timeout_s == 60
    assert s.llm_max_retries == 3
    assert s.max_output_tokens == 4000
    assert s.max_file_bytes == 50_000
    assert s.github_token is None
    assert s.price_in_per_m is None
    assert s.price_out_per_m is None


def test_model_has_no_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "k")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_not_in_repr(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "sk-very-secret")
    monkeypatch.setenv("GMI_MODEL", "m")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_very_secret")
    text = repr(Settings(_env_file=None)) + str(Settings(_env_file=None))
    assert "sk-very-secret" not in text
    assert "ghp_very_secret" not in text


@pytest.mark.parametrize("value", ["3", "10", "-1"])
def test_repairs_are_capped_at_two_so_an_issue_gets_at_most_three_patch_cycles(
    monkeypatch, value
):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "k")
    monkeypatch.setenv("GMI_MODEL", "m")
    monkeypatch.setenv("AGENT_MAX_REPAIRS", value)
    with pytest.raises(ValidationError, match="agent_max_repairs"):
        Settings(_env_file=None)
    for ok in ("0", "1", "2"):
        monkeypatch.setenv("AGENT_MAX_REPAIRS", ok)
        assert Settings(_env_file=None).agent_max_repairs == int(ok)


def test_prices_for_each_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "k")
    monkeypatch.setenv("GMI_MODEL", "a/main")
    monkeypatch.setenv("PRICE_IN_PER_M", "0.14")
    monkeypatch.setenv("PRICE_OUT_PER_M", "0.28")
    monkeypatch.setenv("MODEL_PRICES", '{"b/other": ["0.05", "0.25"]}')
    s = Settings(_env_file=None)
    assert s.prices_for("a/main") == (Decimal("0.14"), Decimal("0.28"))
    assert s.prices_for("b/other") == (Decimal("0.05"), Decimal("0.25"))
    assert s.prices_for("c/unknown") == (None, None)  # never GMI_MODEL's prices


def test_model_prices_must_be_pairs(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GMI_API_KEY", "k")
    monkeypatch.setenv("GMI_MODEL", "m")
    monkeypatch.setenv("MODEL_PRICES", '{"b/other": ["0.05"]}')
    with pytest.raises(ValidationError, match="model_prices"):
        Settings(_env_file=None)
