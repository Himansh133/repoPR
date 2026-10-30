"""Settings loaded from environment variables and an optional `.env` file."""

from decimal import Decimal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """RepoPilot configuration.

    Secrets are `SecretStr`, so they never show up in `repr` or logs.
    `gmi_model` has no default on purpose: the model ID is never hardcoded.
    Prices have no default either; if they are missing, cost is reported as unknown.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gmi_api_key: SecretStr
    gmi_model: str
    gmi_base_url: str = "https://api.gmi-serving.com/v1"
    github_token: SecretStr | None = None

    llm_timeout_s: float = 60
    llm_max_retries: int = 3
    max_output_tokens: int = 4000
    max_file_bytes: int = 50_000

    agent_max_steps: int = 12
    agent_max_total_tokens: int = 80_000
    agent_timeout_s: float = 120
    agent_max_invalid_calls: int = 3
    max_files_per_investigation: int = 10

    agent_run_max_steps: int = 24
    agent_run_max_tokens: int = 150_000
    agent_run_timeout_s: float = 300
    # Hard cap: 1 patch + at most 2 repairs = at most 3 patch/test cycles per issue.
    agent_max_repairs: int = Field(2, ge=0, le=2)

    daily_spend_cap: Decimal | None = Decimal("5.00")
    max_runs_per_day: int = 5
    allow_unpriced: bool = False

    sandbox_profile: str = "auto"  # or "python-pytest" / "node-npm"
    sandbox_image: str = "repopilot-pytest"
    sandbox_node_image: str = "repopilot-node"
    sandbox_timeout_s: int = 60

    price_in_per_m: Decimal | None = None
    price_out_per_m: Decimal | None = None
    # Prices of other models for `repopilot eval`, as JSON: {"org/model": [in_per_m, out_per_m]}.
    model_prices: dict[str, tuple[Decimal, Decimal]] = {}

    def prices_for(self, model: str) -> tuple[Decimal | None, Decimal | None]:
        """Prices of `model`: its `MODEL_PRICES` entry, else the PRICE_* pair if it is
        GMI_MODEL, else unknown. Another model is never costed at GMI_MODEL's prices."""
        if model in self.model_prices:
            return self.model_prices[model]
        if model == self.gmi_model:
            return (self.price_in_per_m, self.price_out_per_m)
        return (None, None)
