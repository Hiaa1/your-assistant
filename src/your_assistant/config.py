"""环境配置。所有可变行为都通过环境变量暴露，不写死在代码里。"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import Capability

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _path(name: str, default: str) -> Path:
    path = Path(_env(name, default)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _paths(name: str, defaults: tuple[str, ...]) -> tuple[Path, ...]:
    """冒号分隔的目录列表；相对路径按项目根解析。"""
    raw = _env(name)
    items = raw.split(os.pathsep) if raw else list(defaults)
    result: list[Path] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        path = Path(item).expanduser()
        result.append(path if path.is_absolute() else PROJECT_ROOT / path)
    return tuple(result)


@dataclass(frozen=True)
class LlmSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    proxy: str = ""
    timeout: float = 60.0

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def describe(self) -> str:
        if not self.available:
            return "未配置（确定性模式）"
        return f"{self.model} @ {self.base_url}"


def _discover_pi_llm() -> tuple[str, str]:
    """从 pi 的模型配置里取 baseUrl / apiKey，免得再抄一份密钥。

    model 由 ASSISTANT_LLM_MODEL 决定（默认 deepseek-flash），因为 models.json 里
    可能同时存在多个候选。
    """
    provider = _env("ASSISTANT_PI_PROVIDER", "deepseek")
    config_path = Path(_env("ASSISTANT_PI_CONFIG", "~/.pi/agent/models.json")).expanduser()
    if not config_path.exists():
        return "", ""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    provider_config = (data.get("providers") or {}).get(provider) or {}
    return str(provider_config.get("baseUrl") or ""), str(provider_config.get("apiKey") or "")


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    vault_dir: Path
    db_path: Path
    rules_file: Path
    mail_rules_file: Path
    memory_dir: Path
    agent_log_root: Path
    codex_log_root: Path
    index_roots: tuple[Path, ...] = ()
    remote_agent_hosts: tuple[str, ...] = ()

    host: str = "127.0.0.1"
    port: int = 8000
    allow_lan: bool = False
    secure_cookie: bool = False
    session_secret: str = ""
    password: str = ""
    access_key: str = ""
    show_login_link: bool = True

    email_address: str = ""
    email_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_ssl: bool = True
    mark_seen_remote: bool = False
    initial_mail_limit: int = 50
    mail_interval_seconds: int = 300

    llm: LlmSettings = LlmSettings()
    timezone: str = "Asia/Shanghai"
    daily_review_at: str = "07:30"
    capabilities: frozenset[Capability] = frozenset()

    @property
    def mail_configured(self) -> bool:
        return bool(self.email_address and self.email_password and self.imap_host)

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.vault_dir, self.memory_dir, self.rules_file.parent):
            path.mkdir(parents=True, exist_ok=True)

    def public_summary(self) -> dict[str, str]:
        """可安全打印的配置摘要（不含任何密钥）。"""
        return {
            "root": str(self.root),
            "db": str(self.db_path),
            "vault": str(self.vault_dir),
            "rules": str(self.rules_file),
            "mail": self.email_address or "未配置",
            "imap": f"{self.imap_host}:{self.imap_port}" if self.imap_host else "未配置",
            "llm": self.llm.describe(),
            "capabilities": ", ".join(sorted(self.capabilities)) or "无",
        }


def _capabilities() -> frozenset[Capability]:
    caps = {Capability.READ_MAIL, Capability.DRAFT, Capability.NOTIFY}
    # 只读是默认策略：要写入必须显式打开，且需要对应适配器。
    if _bool("ASSISTANT_ALLOW_SEND"):
        caps.add(Capability.SEND_MAIL)
    if _bool("ASSISTANT_ALLOW_DELETE"):
        caps.add(Capability.DELETE_MAIL)
    return frozenset(caps)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    base_url = _env("ASSISTANT_LLM_BASE_URL")
    api_key = _env("ASSISTANT_LLM_API_KEY")
    if not base_url or not api_key:
        discovered_url, discovered_key = _discover_pi_llm()
        base_url = base_url or discovered_url
        api_key = api_key or discovered_key

    vault_dir = _path("ASSISTANT_VAULT_DIR", "./vault")
    password = _env("ASSISTANT_PASSWORD")

    settings = Settings(
        root=PROJECT_ROOT,
        data_dir=_path("ASSISTANT_DATA_DIR", "./data"),
        vault_dir=vault_dir,
        db_path=_path("ASSISTANT_DB_PATH", "./data/assistant.db"),
        rules_file=_path("ASSISTANT_RULES_FILE", "./rules/lessons.md"),
        mail_rules_file=_path("ASSISTANT_MAIL_RULES_FILE", "./rules/mail_rules.yaml"),
        memory_dir=_path("ASSISTANT_MEMORY_DIR", "./memory"),
        agent_log_root=Path(_env("ASSISTANT_AGENT_LOG_ROOT", "~/.pi/agent/sessions")).expanduser(),
        codex_log_root=Path(_env("ASSISTANT_CODEX_LOG_ROOT", "~/.codex/sessions")).expanduser(),
        host=_env("ASSISTANT_HOST", "127.0.0.1"),
        allow_lan=_bool("ASSISTANT_ALLOW_LAN"),
        port=_int("ASSISTANT_PORT", 8000),
        secure_cookie=_bool("ASSISTANT_SECURE_COOKIE"),
        session_secret=_env("ASSISTANT_SESSION_SECRET") or secrets.token_urlsafe(32),
        password=password,
        access_key=_env("ASSISTANT_ACCESS_KEY") or password,
        show_login_link=_bool("ASSISTANT_SHOW_LOGIN_LINK", True),
        email_address=_env("ASSISTANT_EMAIL_ADDRESS"),
        email_password=_env("ASSISTANT_EMAIL_PASSWORD"),
        imap_host=_env("ASSISTANT_IMAP_HOST", "imap.shanghaitech.edu.cn"),
        imap_port=_int("ASSISTANT_IMAP_PORT", 993),
        imap_ssl=_bool("ASSISTANT_IMAP_SSL", True),
        mark_seen_remote=_bool("ASSISTANT_MARK_SEEN_REMOTE", False),
        initial_mail_limit=_int("ASSISTANT_INITIAL_MAIL_LIMIT", 50),
        mail_interval_seconds=_int("ASSISTANT_MAIL_INTERVAL_SECONDS", 300),
        llm=LlmSettings(
            base_url=base_url,
            model=_env("ASSISTANT_LLM_MODEL", "deepseek-flash"),
            api_key=api_key,
            # 默认不走代理：邮箱和本机服务都在内网，继承 shell 的 socks 代理
            # 反而会失败。需要代理时显式设置 http://host:port。
            proxy=_env("ASSISTANT_LLM_PROXY"),
            timeout=_float("ASSISTANT_LLM_TIMEOUT", 60.0),
        ),
        timezone=_env("ASSISTANT_TIMEZONE", "Asia/Shanghai"),
        daily_review_at=_env("ASSISTANT_DAILY_REVIEW_AT", "20:00"),
        index_roots=_paths("ASSISTANT_INDEX_DIRS", (str(vault_dir),)),
        remote_agent_hosts=tuple(
            host.strip() for host in _env("ASSISTANT_REMOTE_AGENT_HOSTS").split(",") if host.strip()
        ),
        capabilities=_capabilities(),
    )
    return settings
