"""读取历史 agent 会话（pi / codex），用于每日复盘。

只读：不修改任何会话文件；只取用户与助手的可见文本，跳过 thinking 与系统提示。
远程主机（例如你的 2404）通过 `ssh <host> tar` 拉到本地缓存后同样解析；
连不上时记一条 warning，不影响本机复盘。
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_TURN_CHARS = 2000
MAX_TURNS_PER_SESSION = 60
CORRECTION_MARKERS = re.compile(
    r"不对|错了|不要|别把|不是这样|应该|改成|其实我是说|漏了|重新|再改|不准确|没按|又忘|我是说"
)


@dataclass
class Turn:
    role: str  # user / assistant
    text: str
    at: datetime | None = None


@dataclass
class Session:
    source: str  # pi / codex
    session_id: str
    cwd: str
    path: str
    started_at: datetime | None
    ended_at: datetime | None
    turns: list[Turn] = field(default_factory=list)
    tools: Counter[str] = field(default_factory=Counter)
    host: str = ""

    @property
    def project(self) -> str:
        name = Path(self.cwd).name if self.cwd else ""
        return name or self.host or "未知项目"

    @property
    def user_turns(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.role == "user" and turn.text]

    @property
    def assistant_turns(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.role == "assistant" and turn.text]


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))


def _clip(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text[:MAX_TURN_CHARS]


def _read_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def parse_pi_session(path: Path, *, host: str = "") -> Session | None:
    header: dict = {}
    turns: list[Turn] = []
    tools: Counter[str] = Counter()
    for event in _read_jsonl(path):
        kind = event.get("type")
        if kind == "session":
            header = event
        elif kind == "message":
            message = event.get("message") or {}
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            texts: list[str] = []
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        texts.append(str(item.get("text") or ""))
                    elif item.get("type") == "toolCall":
                        tools[str(item.get("name") or "unknown")] += 1
            text = _clip("\n".join(texts))
            if text or role == "user":
                turns.append(Turn(role=role, text=text, at=_parse_dt(event.get("timestamp"))))

    if not turns:
        return None
    started = _parse_dt(header.get("timestamp")) or next((t.at for t in turns if t.at), None)
    ended = next((t.at for t in reversed(turns) if t.at), started)
    return Session(
        source="pi",
        session_id=str(header.get("id") or path.stem),
        cwd=str(header.get("cwd") or ""),
        path=str(path),
        started_at=started,
        ended_at=ended,
        turns=turns[:MAX_TURNS_PER_SESSION],
        tools=tools,
        host=host,
    )


def parse_codex_session(path: Path, *, host: str = "") -> Session | None:
    meta: dict = {}
    turns: list[Turn] = []
    tools: Counter[str] = Counter()
    for event in _read_jsonl(path):
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "session_meta":
            meta = payload
        elif kind == "response_item":
            item_type = payload.get("type")
            if item_type == "message" and payload.get("role") in {"user", "assistant"}:
                texts = [
                    str(part.get("text") or "")
                    for part in payload.get("content") or []
                    if isinstance(part, dict)
                    and part.get("type") in {"input_text", "output_text", "text"}
                ]
                text = _clip("\n".join(texts))
                if text:
                    turns.append(
                        Turn(
                            role=str(payload["role"]),
                            text=text,
                            at=_parse_dt(event.get("timestamp")),
                        )
                    )
            elif item_type in {"custom_tool_call", "function_call"}:
                tools[str(payload.get("name") or "unknown")] += 1

    if not turns:
        return None
    started = _parse_dt(meta.get("timestamp")) or next((t.at for t in turns if t.at), None)
    ended = next((t.at for t in reversed(turns) if t.at), started)
    return Session(
        source="codex",
        session_id=str(meta.get("session_id") or path.stem),
        cwd=str(meta.get("cwd") or ""),
        path=str(path),
        started_at=started,
        ended_at=ended,
        turns=turns[:MAX_TURNS_PER_SESSION],
        tools=tools,
        host=host,
    )


def _overlaps(session: Session, start: datetime, end: datetime) -> bool:
    """会话区间与 [start, end) 是否有交集；跨天会话两天都算。"""
    began = session.started_at
    if not began:
        return False
    finished = session.ended_at or began
    return began < end and finished >= start


def collect_local(
    pi_root: Path,
    codex_root: Path,
    day: date,
    tz: ZoneInfo,
) -> list[Session]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    sessions: list[Session] = []
    for path in sorted(pi_root.rglob("*.jsonl")):
        session = parse_pi_session(path)
        if session and _overlaps(session, start, end):
            sessions.append(session)
    for path in sorted(codex_root.rglob("*.jsonl")):
        session = parse_codex_session(path)
        if session and _overlaps(session, start, end):
            sessions.append(session)
    return sessions


def fetch_remote_sessions(
    host: str, destination: Path, *, timeout: float = 120.0, runner=None
) -> tuple[list[Session], str]:
    """把远端 pi/codex 会话目录 tar 到本地缓存。返回 (未过滤的会话, 错误信息)。"""
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(min(20, timeout))}",
        host,
        "find .pi/agent/sessions .codex/sessions -name '*.jsonl' -mtime -3 -print0 "
        "| tar --null -T - -cf - 2>/dev/null",
    ]
    runner = runner or subprocess.run
    try:
        completed = runner(command, capture_output=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 远程不可达是常态，不能影响本机复盘
        return [], f"{host}: {type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", "replace")[:160]
        return [], f"{host}: 退出码 {completed.returncode} {detail}".strip()
    try:
        with tarfile.open(fileobj=BytesIO(completed.stdout), mode="r:") as archive:
            archive.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        return [], f"{host}: 解包失败 {type(exc).__name__}: {exc}"

    sessions: list[Session] = []
    pi_root = destination / ".pi" / "agent" / "sessions"
    codex_root = destination / ".codex" / "sessions"
    for path in sorted(pi_root.rglob("*.jsonl")):
        session = parse_pi_session(path, host=host)
        if session:
            sessions.append(session)
    for path in sorted(codex_root.rglob("*.jsonl")):
        session = parse_codex_session(path, host=host)
        if session:
            sessions.append(session)
    return sessions, ""


def corrections(sessions: list[Session]) -> list[dict[str, str]]:
    """找出"用户纠正助手"的回合，并保留上一轮助手的说法作为上下文。"""
    found: list[dict[str, str]] = []
    for session in sessions:
        previous_assistant = ""
        for turn in session.turns:
            if turn.role == "assistant":
                if turn.text:
                    previous_assistant = turn.text
                continue
            if turn.text and CORRECTION_MARKERS.search(turn.text):
                found.append(
                    {
                        "project": session.project,
                        "source": session.source,
                        "agent_said": previous_assistant[:400],
                        "user_said": turn.text[:400],
                    }
                )
    return found
