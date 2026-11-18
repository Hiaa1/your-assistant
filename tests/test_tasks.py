from __future__ import annotations

from datetime import UTC, datetime, timedelta

from your_assistant.tasks import (
    Task,
    TaskDay,
    day_path,
    elapsed_seconds,
    read_tasks,
    remaining_seconds,
    replace_titles,
    start,
    stop,
    toggle,
    write_tasks,
)

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def test_read_missing_file_is_empty(tmp_path):
    data = read_tasks(day_path(tmp_path, "2026-11-18"), "2026-11-18")
    assert data.day == "2026-11-18"
    assert data.tasks == []


def test_write_and_read_roundtrip(tmp_path):
    path = day_path(tmp_path, "2026-11-18")
    data = TaskDay(
        day="2026-11-18",
        tasks=[
            Task(title="迁移 your-assistant 到 Mac", spent=750, minutes=30),
            Task(title="重做首页 UI", done=True, spent=90),
        ],
    )
    write_tasks(path, data)

    text = path.read_text(encoding="utf-8")
    assert "# 2026-11-18 今日任务" in text
    assert "- [x] 重做首页 UI" in text

    again = read_tasks(path, "2026-11-18")
    assert [task.title for task in again.tasks] == ["迁移 your-assistant 到 Mac", "重做首页 UI"]
    assert again.tasks[0].spent == 750
    assert again.tasks[0].minutes == 30
    assert again.tasks[1].done is True


def test_replace_titles_keeps_state_for_same_title():
    data = TaskDay(day="2026-11-18", tasks=[Task(title="旧任务", spent=300, done=True)])
    replace_titles(data, "旧任务\n新任务\n\n")
    assert [task.title for task in data.tasks] == ["旧任务", "新任务"]
    assert data.tasks[0].spent == 300
    assert data.tasks[0].done is True
    assert data.tasks[1].spent == 0


def test_start_stop_accumulates_and_counts_down():
    task = Task(title="写周报", minutes=25)
    start(task, NOW)
    assert remaining_seconds(task, NOW + timedelta(minutes=5)) == 20 * 60
    assert elapsed_seconds(task, NOW + timedelta(minutes=5)) == 5 * 60

    stop(task, NOW + timedelta(minutes=5))
    assert task.started_at is None
    assert task.spent == 5 * 60
    assert remaining_seconds(task, NOW + timedelta(hours=1)) == 20 * 60


def test_stop_caps_at_total_and_toggle_clears_timer():
    task = Task(title="写周报", minutes=1)
    start(task, NOW)
    stop(task, NOW + timedelta(minutes=10))
    assert task.spent == 60
    assert remaining_seconds(task, NOW) == 0

    task2 = Task(title="另一个", minutes=25)
    start(task2, NOW)
    toggle(task2)
    assert task2.done is True
    assert task2.started_at is None


def test_started_timer_is_visible_after_reload(tmp_path):
    path = day_path(tmp_path, "2026-11-18")
    data = TaskDay(day="2026-11-18", tasks=[Task(title="读论文", minutes=25)])
    start(data.tasks[0], NOW)
    write_tasks(path, data)

    again = read_tasks(path, "2026-11-18")
    task = again.tasks[0]
    assert task.started_at == NOW
    assert remaining_seconds(task, NOW + timedelta(minutes=10)) == 15 * 60
