from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./transcripts.db"
    api_token: str = ""
    default_languages: str = "en"
    allowed_origins: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def language_priority(self) -> list[str]:
        return [item.strip() for item in self.default_languages.split(",") if item.strip()]

    @property
    def normalized_api_token(self) -> str:
        return self.api_token.strip()

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
