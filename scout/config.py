"""Secrets and machine-level config, read from the environment / .env.

Anything a user would plausibly want to tweak while hunting for a flat lives in
the database instead (see `models.Criteria` and `models.Settings`) so it can be
edited from the web UI. This module is only for things that belong in a secret
store: API keys, SMTP credentials, where the database file lives.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = ""
    digest_to: str = ""

    # Server
    scout_host: str = "0.0.0.0"
    scout_port: int = 8080
    scout_auth_user: str = ""
    scout_auth_password: str = ""

    # Storage
    scout_db_path: str = ""

    @property
    def db_path(self) -> Path:
        if self.scout_db_path:
            return Path(self.scout_db_path).expanduser()
        return Path.home() / ".appart-scout" / "scout.db"

    @property
    def mail_from(self) -> str:
        return self.smtp_from or self.smtp_user

    @property
    def default_recipients(self) -> list[str]:
        return [a.strip() for a in self.digest_to.split(",") if a.strip()]

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_smtp(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
