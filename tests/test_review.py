from __future__ import annotations

import json
import tarfile
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from your_assistant.agentlog import (
    collect_local,
    corrections,
    fetch_remote_sessions,
    parse_codex_session,
    parse_pi_session,
)
from your_assistant.review import run_digest, write_digest

TZ = ZoneInfo("Asia/Shanghai")


def _iso(day: date, hour: int = 10) -> str:
    return datetime.combine(day, time(hour, 0), tzinfo=TZ).isoformat()


def write_pi(path: Path, day: date, *, cwd: str = "/home/hia/proj/demo") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "session", "id": "pi-1", "timestamp": _iso(day), "cwd": cwd},
        {
            "type": "message",
            "timestamp": _iso(day, 10),
            "message": {"role": "user", "content": [{"type": "text", "text": "帮我更新索引"}]},
        },
        {
            "type": "message",
            "timestamp": _iso(day, 11),
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我把报名截止时间当成了活动开始时间。"},
                    {"type": "toolCall", "name": "edit"},
                ],
            },
        },
        {
            "type": "message",
            "timestamp": _iso(day, 12),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "不对，不要把这个当成活动开始时间，分开标注"}],
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8"
    )


def write_codex(path: Path, day: date, *, cwd: str = "/home/hia/paper_2404") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": _iso(day),
            "type": "session_meta",
            "payload": {"session_id": "codex-1", "cwd": cwd, "timestamp": _iso(day)},
        },
        {
            "timestamp": _iso(day, 10),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "在 2404 上跑一遍实验"}],
            },
        },
        {
            "timestamp": _iso(day, 11),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "已在 2404 上启动，日志在 /tmp/run.log"}
                ],
            },
        },
        {
            "timestamp": _iso(day, 12),
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec"},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8"
    )


@pytest.fixture
def logs(settings):
    today = datetime.now(TZ).date()
    write_pi(settings.agent_log_root / "proj-demo" / "s1.jsonl", today)
    write_codex(settings.codex_log_root / "2026" / "11" / "17" / "c1.jsonl", today)
    return today


def test_parse_pi_and_codex_sessions(logs, settings):
    pi_file = next(settings.agent_log_root.rglob("*.jsonl"))
    codex_file = next(settings.codex_log_root.rglob("*.jsonl"))
    pi = parse_pi_session(pi_file)
    codex = parse_codex_session(codex_file)
    assert pi and pi.project == "demo" and pi.tools["edit"] == 1
    assert codex and codex.project == "paper_2404" and codex.tools["exec"] == 1
    assert len(pi.user_turns) == 2


def test_collect_filters_by_day(logs, settings):
    day = logs
    sessions = collect_local(settings.agent_log_root, settings.codex_log_root, day, TZ)
    assert {session.source for session in sessions} == {"pi", "codex"}
    tomorrow = day + timedelta(days=1)
    assert collect_local(settings.agent_log_root, settings.codex_log_root, tomorrow, TZ) == []


def test_digest_detects_corrections_and_writes_files(logs, settings):
    digest = run_digest(settings, logs, use_llm=False)
    assert len(digest.sessions) == 2
    assert not digest.used_llm
    assert "paper_2404" in digest.markdown
    assert "报名截止时间" in digest.markdown

    detected = corrections(digest.sessions)
    assert detected and "不要把这个当成活动开始时间" in detected[0]["user_said"]

    write_digest(settings, digest)
    assert digest.review_path and digest.review_path.exists()
    assert digest.cases_path and digest.cases_path.exists()
    assert "纠错案例" in digest.cases_path.read_text(encoding="utf-8")


def test_llm_payload_is_normalized(logs, settings, monkeypatch):
    class FakeLlm:
        available = True

        def complete_json(self, task, prompt):
            assert "报名截止时间" in prompt
            return {
                "projects": [
                    {
                        "name": "your-assistant 助手",
                        "did": ["写索引"],
                        "decisions": ["用 FTS5"],
                        "artifacts": [],
                    }
                ],
                "corrections": [
                    {
                        "problem": "混淆截止与开始时间",
                        "correction": "分开标注并给出处",
                        "rule": "不要混用截止时间和开始时间",
                    }
                ],
                "highlights": ["完成 Web 界面"],
                "followups": ["验证手机访问"],
            }

    monkeypatch.setattr("your_assistant.review.build_llm", lambda _settings: FakeLlm())
    digest = run_digest(settings, logs)
    assert digest.used_llm
    assert "your-assistant 助手" in digest.markdown
    assert "不要混用截止时间和开始时间" in digest.markdown
    assert "验证手机访问" in digest.markdown


def test_llm_failure_falls_back(logs, settings, monkeypatch):
    class BrokenLlm:
        available = True

        def complete_json(self, task, prompt):
            return None

    monkeypatch.setattr("your_assistant.review.build_llm", lambda _settings: BrokenLlm())
    digest = run_digest(settings, logs)
    assert not digest.used_llm
    assert any("LLM" in warning for warning in digest.warnings)


def _remote_tar(day: date) -> bytes:
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        path = ".pi/agent/sessions/remote/s1.jsonl"
        tmp = Path("/tmp/your-assistant-remote-pi.jsonl")
        write_pi(tmp, day, cwd="/home/hia/remote-project")
        data = tmp.read_bytes()
        info = tarfile.TarInfo(path)
        info.size = len(data)
        archive.addfile(info, BytesIO(data))
    return buffer.getvalue()


def test_remote_sessions_are_parsed_from_ssh_tar(logs, settings, tmp_path):
    payload = _remote_tar(logs)

    class Result:
        returncode = 0
        stdout = payload
        stderr = b""

    sessions, error = fetch_remote_sessions(
        "2404", tmp_path / "remote", runner=lambda *a, **k: Result()
    )
    assert error == ""
    assert sessions and sessions[0].host == "2404"
    assert sessions[0].project == "remote-project"


def test_remote_failure_becomes_warning(logs, settings, monkeypatch, tmp_path):
    class Result:
        returncode = 255
        stdout = b""
        stderr = b"ssh: connect to host 2404 port 22: Connection timed out"

    monkeypatch.setenv("ASSISTANT_REMOTE_AGENT_HOSTS", "2404")
    from your_assistant.config import load_settings

    settings = load_settings(env_file=Path("/nonexistent/.env"))
    monkeypatch.setattr(
        "your_assistant.review.fetch_remote_sessions",
        lambda host, destination: ([], "2404: 连接超时"),
    )
    digest = run_digest(settings, logs, use_llm=False)
    assert any("2404" in warning for warning in digest.warnings)
    assert "2404" in digest.markdown
