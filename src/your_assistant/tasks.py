"""今日任务：`vault/tasks/YYYY-MM-DD.md` 的读写与倒计时状态。

文件即真相：一行一个任务，人类可读、可手改、可进 Git。

    # 2026-11-18 今日任务

    - [ ] 迁移 your-assistant 到 Mac <!-- minutes=25 spent=0 -->
    - [x] 重做首页 UI <!-- minutes=25 spent=750 -->
    - [ ] 写周报 <!-- minutes=25 spent=0 started=2026-11-18T10:30:00+08:00 -->
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

DEFAULT_MINUTES = 25

_LINE = re.compile(
    r"^- \[(?P<mark>[ xX])\] (?P<title>.+?)"
    r"(?: <!-- minutes=(?P<minutes>\d+) spent=(?P<spent>\d+)"
    r"(?: mode=(?P<mode>up))?"
    r"(?: started=(?P<started>\S+))? -->)?$"
)


@dataclass
class Task:
    title: str
    done: bool = False
    minutes: int = DEFAULT_MINUTES
    spent: int = 0
    started_at: datetime | None = None
    counting_up: bool = False

    @property
    def total_seconds(self) -> int:
        return max(1, self.minutes) * 60


@dataclass
class TaskDay:
    day: str
    tasks: list[Task] = field(default_factory=list)


def day_path(vault_dir: Path, day: str) -> Path:
    return vault_dir / "tasks" / f"{day}.md"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_tasks(path: Path, day: str) -> TaskDay:
    result = TaskDay(day=day)
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        result.tasks.append(
            Task(
                title=match.group("title").strip(),
                done=match.group("mark").lower() == "x",
                minutes=int(match.group("minutes") or DEFAULT_MINUTES),
                spent=int(match.group("spent") or 0),
                started_at=_parse_time(match.group("started")),
                counting_up=match.group("mode") == "up",
            )
        )
    return result


def write_tasks(path: Path, data: TaskDay) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {data.day} 今日任务", ""]
    for task in data.tasks:
        mark = "x" if task.done else " "
        extra = f" <!-- minutes={task.minutes} spent={task.spent}"
        if task.counting_up:
            extra += " mode=up"
        if task.started_at:
            extra += f" started={task.started_at.isoformat(timespec='seconds')}"
        extra += " -->"
        lines.append(f"- [{mark}] {task.title}{extra}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def replace_titles(data: TaskDay, text: str) -> None:
    """按行替换标题，保留同名任务的完成/时长/累计。"""
    titles = [line.strip().lstrip("-•").strip() for line in text.splitlines()]
    titles = [title for title in titles if title]
    existing = {task.title: task for task in data.tasks}
    data.tasks = [
        existing.get(title) or Task(title=title, minutes=DEFAULT_MINUTES, spent=0, done=False)
        for title in titles
    ]


def replace_tasks(data: TaskDay, items: list[tuple[str, int]]) -> None:
    """编辑表单保存：(标题, 分钟) 列表，保留同名任务的完成/累计/运行状态。"""
    existing = {task.title: task for task in data.tasks}
    seen: set[str] = set()
    result: list[Task] = []
    for title, minutes in items:
        title = title.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        task = existing.get(title) or Task(title=title)
        task.minutes = max(1, min(180, int(minutes)))
        result.append(task)
    data.tasks = result


def start(task: Task, now: datetime) -> None:
    if task.started_at or task.done:
        return
    task.counting_up = False
    task.started_at = now


def start_overtime(task: Task, now: datetime) -> None:
    """倒计时走完之后，继续正计时。"""
    if task.started_at or task.done or task.spent < task.total_seconds:
        return
    task.counting_up = True
    task.started_at = now


def stop(task: Task, now: datetime) -> None:
    if not task.started_at:
        return
    elapsed = max(0, int((now - task.started_at).total_seconds()))
    if task.counting_up:
        task.spent += elapsed
    else:
        task.spent = min(task.total_seconds, task.spent + elapsed)
    task.started_at = None
    task.counting_up = False


def reset(task: Task) -> None:
    """清零累计时间并停止计时，完成状态不变。"""
    task.spent = 0
    task.started_at = None


def toggle(task: Task) -> None:
    task.done = not task.done
    if task.done:
        task.started_at = None


def remaining_seconds(task: Task, now: datetime) -> int:
    if task.counting_up and task.started_at:
        return 0
    used = task.spent
    if task.started_at:
        used += max(0, int((now - task.started_at).total_seconds()))
    return max(0, task.total_seconds - used)


def overtime_seconds(task: Task, now: datetime) -> int:
    extra = max(0, task.spent - task.total_seconds)
    if task.counting_up and task.started_at:
        extra += max(0, int((now - task.started_at).total_seconds()))
    return extra


def elapsed_seconds(task: Task, now: datetime) -> int:
    used = task.spent
    if task.started_at:
        used += max(0, int((now - task.started_at).total_seconds()))
    return min(task.total_seconds, used)


def _duration(seconds: int) -> str:
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def archive_day(vault_dir: Path, day: date) -> Path | None:
    """把某天的任务状态写成月度归档，原文一并保留。幂等：当天已存在就不重复写。"""
    path = day_path(vault_dir, day.isoformat())
    if not path.is_file():
        return None
    data = read_tasks(path, day.isoformat())
    if not data.tasks:
        return None
    done = sum(1 for task in data.tasks if task.done)
    spent = sum(task.spent for task in data.tasks)
    pending = [task.title for task in data.tasks if not task.done]
    lines = [
        f"## {day.isoformat()}",
        "",
        f"- 完成 {done}/{len(data.tasks)}，计时 {_duration(spent)}",
    ]
    if pending:
        lines.append("- 未完成：" + "、".join(pending))
    lines += ["", "```markdown", path.read_text(encoding="utf-8").rstrip(), "```", ""]

    archive = vault_dir / "tasks" / "archive" / f"{day:%Y-%m}.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    header = f"# {day:%Y-%m} 任务归档\n\n"
    existing = archive.read_text(encoding="utf-8") if archive.exists() else header
    if f"## {day.isoformat()}" in existing:
        return archive
    body = existing if existing.endswith("\n") else existing + "\n"
    archive.write_text(body + "\n".join(lines) + "\n", encoding="utf-8")
    return archive
