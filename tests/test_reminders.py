from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from your_assistant.db import migrate
from your_assistant.mail import MailRepository
from your_assistant.models import Category, Classification, Importance, MailMessage, Notify
from your_assistant.reminders import acknowledge, counts, due, panel, plan, tick


def _insert(repo: MailRepository, *, uid: int, category: Category, notify: Notify) -> int:
    message = MailMessage(
        message_id=f"m{uid}@x",
        thread_key=f"m{uid}@x",
        subject=f"测试邮件 {uid}",
        sender="sender@example.com",
        recipients=("me@x",),
        sent_at=datetime.now(UTC) - timedelta(hours=2),
        body_text="正文",
        account="me@x",
        uidvalidity=1,
        uid=uid,
        server_unseen=True,
    )
    repo.insert(
        message,
        is_new=True,
        classification=Classification(
            importance=Importance.CRITICAL if notify is Notify.IMMEDIATE else Importance.NORMAL,
            category=category,
            notify=notify,
            tags=(),
            reason="测试",
        ),
    )
    row = repo.conn.execute(
        "SELECT id FROM mail_messages WHERE message_id = ?", (f"m{uid}@x",)
    ).fetchone()
    return int(row["id"])


@pytest.fixture
def repo(conn):
    migrate(conn)
    return MailRepository(conn)


def test_categories_map_to_expected_notify_policy(repo):
    paper = _insert(repo, uid=1, category=Category.PAPER_ADVISOR, notify=Notify.IMMEDIATE)
    event = _insert(repo, uid=2, category=Category.EVENT, notify=Notify.NORMAL)
    announcement = _insert(repo, uid=3, category=Category.ANNOUNCEMENT, notify=Notify.NONE)
    # 把首见时间往前挪，模拟已经等了一会儿
    repo.conn.execute(
        "UPDATE mail_messages SET first_seen_at = ?",
        ((datetime.now(UTC) - timedelta(hours=3)).isoformat(),),
    )
    repo.conn.commit()

    assert plan(repo.conn) > 0
    assert plan(repo.conn) == 0, "重复规划不应插入重复提醒"

    fired = tick(repo.conn)
    touched = {item.mail_id for item in fired}
    assert paper in touched and event in touched
    assert announcement not in touched, "学校公示不应提醒"
    # 3 小时前收到的即时提醒：0/15/45 分钟和 2 小时这几档已经到期
    assert sum(1 for item in fired if item.mail_id == paper) == 4
    assert sum(1 for item in fired if item.mail_id == event) == 1
    assert counts(repo.conn)["pending"] >= 1, "后续还有提醒要发"

    again = tick(repo.conn)
    assert again == [], "已发送的提醒不应重复触发"

    acknowledge(repo.conn, paper)
    assert all(item.mail_id != paper for item in panel(repo.conn))
    assert counts(repo.conn)["pending"] == 0


def test_ack_stops_future_reminders(repo):
    mail_id = _insert(repo, uid=9, category=Category.PAPER_ADVISOR, notify=Notify.IMMEDIATE)
    repo.conn.execute(
        "UPDATE mail_messages SET first_seen_at = ?",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
    )
    repo.conn.commit()
    plan(repo.conn)
    assert due(repo.conn)

    acknowledge(repo.conn, mail_id)
    assert due(repo.conn) == []
    assert (
        repo.conn.execute("SELECT COUNT(*) FROM reminders WHERE ack_at IS NULL").fetchone()[0] == 0
    )
    assert repo.conn.execute(
        "SELECT ack_at FROM mail_messages WHERE id = ?", (mail_id,)
    ).fetchone()[0]
