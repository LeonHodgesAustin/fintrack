from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    plaid_client_id: str
    plaid_secret: str
    plaid_env: Literal["sandbox", "production"] = "sandbox"

    db_path: str = "fintrack.db"

    # Comma-separated classifier names; resolved by classification.build_chain()
    classifier_chain: str = "rules,plaid"

    link_server_port: int = 5000
    flask_secret_key: str = "dev-secret-change-me"

    # Optional — only needed when classification/llm.py is added
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"

    def get_classifier_chain(self) -> list[str]:
        return [name.strip() for name in self.classifier_chain.split(",") if name.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
