"""
Application configuration via pydantic-settings.
Reads from environment variables and .env file.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./supplychainos.db"
    secret_key: str = "change-me-in-production"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, alias="PORT")

    # LLM via Groq (Fast, high-throughput inference)
    llm_enabled: bool = True
    groq_api_key: str = ""
    llm_model: str = "qwen/qwen3.8-27b"

    # Planning defaults
    default_budget: float = 50000.0
    default_deadline_days: int = 30
    default_planning_horizon: int = 30
    max_retries: int = 3

    @property
    def is_llm_available(self) -> bool:
        return self.llm_enabled and bool(self.groq_api_key)


settings = Settings()
