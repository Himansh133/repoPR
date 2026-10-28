"""Settings loaded from environment variables and an optional `.env` file."""

from decimal import Decimal

from pydantic import SecretStr
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
    max_output_tokens: int = 1200
    max_file_bytes: int = 50_000

    agent_max_steps: int = 12
    agent_max_total_tokens: int = 30_000
    agent_timeout_s: float = 120
    agent_max_invalid_calls: int = 3
    max_files_per_investigation: int = 10

    agent_run_max_steps: int = 24
    agent_run_max_tokens: int = 60_000
    agent_run_timeout_s: float = 300
    agent_max_repairs: int = 2

    daily_spend_cap: Decimal | None = Decimal("5.00")
    max_runs_per_day: int = 5
    allow_unpriced: bool = False

    sandbox_profile: str = "auto"  # or "python-pytest" / "node-npm"
    sandbox_image: str = "repopilot-pytest"
    sandbox_node_image: str = "repopilot-node"
    sandbox_timeout_s: int = 60

    price_in_per_m: Decimal | None = None
    price_out_per_m: Decimal | None = None
