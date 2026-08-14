"""Settings page credential handling.

The rule being pinned down: the UI can hold every credential, the environment is
the fallback, and a stored secret is never rendered back into the page.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scout.config import Config, get_config
from scout.db import init_db, load_settings
from scout.db import connect as db_connect

BASE_FORM = {
    "run_every_hours": "6",
    "digest_every_days": "3",
    "digest_hour": "8",
    "digest_size": "8",
    "recipients": "you@example.com",
    "enabled_sources": ["flatfox"],
    "vision_enabled": "on",
    "vision_top_n": "10",
    "vision_max_photos": "4",
    "instant_alert_enabled": "on",
    "instant_alert_min_score": "85",
    "max_listing_age_days": "45",
    "max_commute_calls_per_run": "150",
    "smtp_port": "",
    "imap_port": "",
    "smtp_starttls": "",
    "imap_ssl": "",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A web app pointed at a throwaway database with an empty environment."""
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "scout.db"))
    # Config reads .env by default; a developer's real one must not leak in.
    monkeypatch.setattr(Config, "model_config", {"env_file": None, "extra": "ignore"})
    get_config.cache_clear()
    init_db()

    from scout.web.app import app

    yield TestClient(app)
    get_config.cache_clear()


def post(client, **overrides):
    return client.post(
        "/settings", data={**BASE_FORM, **overrides}, follow_redirects=False
    )


def stored():
    with db_connect() as conn:
        return load_settings(conn)


def test_every_credential_can_be_set_from_the_page(client):
    assert post(
        client,
        smtp_host="smtp.gmail.com",
        smtp_port="587",
        smtp_user="me@gmail.com",
        smtp_password="app-password-1",
        imap_host="imap.gmail.com",
        imap_user="me@gmail.com",
        imap_password="app-password-2",
        imap_folder="Alerts",
        openai_api_key="sk-live-key",
    ).status_code == 303

    settings = stored()
    assert settings.smtp_host == "smtp.gmail.com"
    assert settings.smtp_port == 587
    assert settings.smtp_password == "app-password-1"
    assert settings.imap_password == "app-password-2"
    assert settings.imap_folder == "Alerts"
    assert settings.openai_api_key == "sk-live-key"

    creds = get_config().resolve(settings)
    assert creds.has_smtp and creds.has_imap and creds.has_openai


@pytest.mark.parametrize(
    "field,secret",
    [
        ("openai_api_key", "sk-live-key"),
        ("smtp_password", "app-password-1"),
        ("imap_password", "app-password-2"),
    ],
)
def test_a_saved_secret_is_never_rendered_back_into_the_page(client, field, secret):
    post(client, **{field: secret})
    page = client.get("/settings").text
    assert secret not in page
    # ...but you can tell which one is loaded.
    assert f"…{secret[-4:]} (saved)" in page


@pytest.mark.parametrize(
    "field", ["openai_api_key", "smtp_password", "imap_password"]
)
def test_submitting_a_blank_secret_keeps_the_stored_one(client, field):
    post(client, **{field: "secret-value"})
    post(client, **{field: ""})
    assert getattr(stored(), field) == "secret-value"


@pytest.mark.parametrize(
    "field", ["openai_api_key", "smtp_password", "imap_password"]
)
def test_clearing_a_secret_takes_an_explicit_button(client, field):
    post(client, **{field: "secret-value"})
    post(client, **{f"clear_{field}": "1", field: ""})
    assert getattr(stored(), field) == ""


def test_blank_port_means_fall_back_rather_than_zero(client):
    post(client, smtp_port="", imap_port="")
    settings = stored()
    assert settings.smtp_port is None
    assert settings.imap_port is None
    # The fallback is the documented default, not a crash or a 0.
    creds = get_config().resolve(settings)
    assert (creds.smtp_port, creds.imap_port) == (587, 993)


def test_tls_toggles_are_tri_state(client):
    post(client, smtp_starttls="", imap_ssl="")
    assert stored().smtp_starttls is None  # "from .env"

    post(client, smtp_starttls="false", imap_ssl="false")
    settings = stored()
    assert settings.smtp_starttls is False
    creds = get_config().resolve(settings)
    assert creds.smtp_starttls is False
    assert creds.imap_ssl is False


def test_environment_is_used_when_the_page_is_left_blank(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.env.example")
    monkeypatch.setenv("SMTP_USER", "env@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "from-env")
    get_config.cache_clear()

    creds = get_config().resolve(stored())
    assert creds.smtp_host == "smtp.env.example"
    assert creds.smtp_password == "from-env"
    assert creds.has_smtp


def test_the_page_wins_over_the_environment(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.env.example")
    monkeypatch.setenv("SMTP_PASSWORD", "from-env")
    get_config.cache_clear()

    post(client, smtp_host="smtp.ui.example", smtp_password="from-ui")
    creds = get_config().resolve(stored())
    assert creds.smtp_host == "smtp.ui.example"
    assert creds.smtp_password == "from-ui"


def test_saving_settings_does_not_disturb_unrelated_fields(client):
    post(client, smtp_password="app-password-1")
    before = stored()
    post(client, openai_api_key="sk-live-key")
    after = stored()
    assert after.smtp_password == before.smtp_password
    assert after.recipients == before.recipients
    assert after.enabled_sources == before.enabled_sources


def test_the_database_holding_the_passwords_is_owner_only(client):
    post(client, smtp_password="app-password-1")
    assert (get_config().db_path.stat().st_mode & 0o077) == 0
