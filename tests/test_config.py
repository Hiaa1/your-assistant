from __future__ import annotations

from pathlib import Path

from your_assistant.config import load_settings
from your_assistant.models import Capability


def test_default_host_is_loopback_only(settings):
    assert settings.host == "127.0.0.1"
    assert settings.allow_lan is False


def test_default_capabilities_are_read_only(settings):
    assert settings.can(Capability.READ_MAIL)
    assert settings.can(Capability.DRAFT)
    assert settings.can(Capability.NOTIFY)
    assert not settings.can(Capability.SEND_MAIL)
    assert not settings.can(Capability.DELETE_MAIL)


def test_send_and_delete_stay_off_by_default_even_with_mail_credentials(monkeypatch):
    monkeypatch.setenv("ASSISTANT_EMAIL_ADDRESS", "someone@shanghaitech.edu.cn")
    monkeypatch.setenv("ASSISTANT_EMAIL_PASSWORD", "client-password")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    assert settings.mail_configured
    assert not settings.can(Capability.SEND_MAIL)
    assert not settings.can(Capability.DELETE_MAIL)


def test_capabilities_follow_explicit_switches(monkeypatch):
    monkeypatch.setenv("ASSISTANT_ALLOW_SEND", "true")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    assert settings.can(Capability.SEND_MAIL)
    assert not settings.can(Capability.DELETE_MAIL)


def test_llm_credentials_are_discovered_from_pi_config(monkeypatch, fake_pi_config):
    monkeypatch.setenv("ASSISTANT_PI_CONFIG", str(fake_pi_config))
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    assert settings.llm.available
    assert settings.llm.base_url == "https://example.invalid"
    assert settings.llm.model == "deepseek-flash"


def test_explicit_llm_settings_win_over_pi_config(monkeypatch, fake_pi_config):
    monkeypatch.setenv("ASSISTANT_PI_CONFIG", str(fake_pi_config))
    monkeypatch.setenv("ASSISTANT_LLM_BASE_URL", "https://explicit.invalid")
    monkeypatch.setenv("ASSISTANT_LLM_API_KEY", "sk-explicit")
    monkeypatch.setenv("ASSISTANT_LLM_MODEL", "deepseek-v4-pro")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    assert settings.llm.base_url == "https://explicit.invalid"
    assert settings.llm.model == "deepseek-v4-pro"


def test_public_summary_never_leaks_secrets(monkeypatch, fake_pi_config):
    monkeypatch.setenv("ASSISTANT_PI_CONFIG", str(fake_pi_config))
    monkeypatch.setenv("ASSISTANT_PASSWORD", "web-password")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    summary = " ".join(settings.public_summary().values())
    assert "sk-test-key" not in summary
    assert "web-password" not in summary


def test_relative_paths_resolve_under_project_root(monkeypatch):
    monkeypatch.setenv("ASSISTANT_DATA_DIR", "./tmp-data")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    assert settings.data_dir.is_absolute()
    assert settings.data_dir.name == "tmp-data"
