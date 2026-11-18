"""测试夹具：所有测试离线、同步、使用临时目录。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from your_assistant.config import Settings, load_settings
from your_assistant.db import connect, migrate


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把可能从真实环境继承的变量清掉，避免测试碰到私人数据。"""
    for name in (
        "ASSISTANT_DATA_DIR",
        "ASSISTANT_VAULT_DIR",
        "ASSISTANT_DB_PATH",
        "ASSISTANT_RULES_FILE",
        "ASSISTANT_MAIL_RULES_FILE",
        "ASSISTANT_MEMORY_DIR",
        "ASSISTANT_LLM_BASE_URL",
        "ASSISTANT_LLM_API_KEY",
        "ASSISTANT_LLM_MODEL",
        "ASSISTANT_EMAIL_ADDRESS",
        "ASSISTANT_EMAIL_PASSWORD",
        "ASSISTANT_SESSION_SECRET",
        "ASSISTANT_PASSWORD",
        "ASSISTANT_ALLOW_SEND",
        "ASSISTANT_ALLOW_DELETE",
        "ASSISTANT_REMOTE_AGENT_HOSTS",
        "ASSISTANT_CODEX_LOG_ROOT",
        "ASSISTANT_HOST",
        "ASSISTANT_ALLOW_LAN",
        "ASSISTANT_ACCESS_KEY",
        "ASSISTANT_SHOW_LOGIN_LINK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSISTANT_PI_CONFIG", str(tmp_path / "missing-models.json"))
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASSISTANT_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("ASSISTANT_DB_PATH", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("ASSISTANT_RULES_FILE", str(tmp_path / "rules" / "lessons.md"))
    monkeypatch.setenv("ASSISTANT_MAIL_RULES_FILE", str(tmp_path / "rules" / "mail_rules.yaml"))
    monkeypatch.setenv("ASSISTANT_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("ASSISTANT_AGENT_LOG_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("ASSISTANT_CODEX_LOG_ROOT", str(tmp_path / "codex-sessions"))
    yield


@pytest.fixture
def settings(_isolate_env: None) -> Settings:
    return load_settings(env_file=Path("/nonexistent/.env"))


@pytest.fixture
def conn(settings: Settings):
    connection = connect(settings.db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def fake_pi_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "models.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "deepseek": {
                        "baseUrl": "https://example.invalid",
                        "apiKey": "sk-test-key",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return config_path
