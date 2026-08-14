"""Secrets and machine-level config, read from the environment / .env.

Anything a user would plausibly want to tweak while hunting for a flat lives in
the database instead (see `models.Criteria` and `models.Settings`) so it can be
edited from the web UI. This module is only for things that belong in a secret
store: API keys, SMTP credentials, where the database file lives.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel
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

    # IMAP — the mailbox source reads saved-search alert emails from here.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_ssl: bool = True

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

    @property
    def has_imap(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def resolve(self, settings: Any = None) -> Credentials:
        """Merge the UI-editable credentials over the ones from `.env`.

        Two homes for one password is a deliberate trade. `.env` is where a
        secret belongs, and it survives a database reset — but on a headless Pi,
        changing it means SSH, an editor and `systemctl --user restart`. So the
        web UI can hold the same values and wins wherever it is filled in; a
        blank there falls through to here.

        The result quacks like `Config` (same attribute names, same `has_*` and
        `mail_from` properties), so `EmailSender` and the mailbox source take it
        without knowing which layer any given value came from.
        """
        return Credentials(
            openai_api_key=_pick_str(settings, "openai_api_key", self.openai_api_key),
            openai_model=_pick_str(settings, "openai_model", self.openai_model),
            smtp_host=_pick_str(settings, "smtp_host", self.smtp_host),
            smtp_port=_pick(settings, "smtp_port", self.smtp_port),
            smtp_user=_pick_str(settings, "smtp_user", self.smtp_user),
            smtp_password=_pick_str(settings, "smtp_password", self.smtp_password),
            smtp_starttls=_pick(settings, "smtp_starttls", self.smtp_starttls),
            smtp_from=_pick_str(settings, "smtp_from", self.smtp_from),
            imap_host=_pick_str(settings, "imap_host", self.imap_host),
            imap_port=_pick(settings, "imap_port", self.imap_port),
            imap_user=_pick_str(settings, "imap_user", self.imap_user),
            imap_password=_pick_str(settings, "imap_password", self.imap_password),
            imap_folder=_pick_str(settings, "imap_folder", self.imap_folder),
            imap_ssl=_pick(settings, "imap_ssl", self.imap_ssl),
        )


class Credentials(BaseModel):
    """Effective credentials after merging the database over the environment.

    Deliberately shaped like `Config` so it can be passed anywhere `Config` was.
    """

    openai_api_key: str = ""
    openai_model: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = ""

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_ssl: bool = True

    @property
    def mail_from(self) -> str:
        return self.smtp_from or self.smtp_user

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_smtp(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    @property
    def has_imap(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def source_of(self, field: str, env: Config) -> str:
        """Where the effective value came from — for the Settings page."""
        value = getattr(self, field)
        if not value:
            return "unset"
        return "ui" if value != getattr(env, field, None) else "env"


def _pick(settings: Any, field: str, fallback: Any) -> Any:
    """The stored setting, unless it is unset."""
    value = getattr(settings, field, None)
    return fallback if value is None or value == "" else value


def _pick_str(settings: Any, field: str, fallback: str) -> str:
    return str(_pick(settings, field, fallback) or "").strip()


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
