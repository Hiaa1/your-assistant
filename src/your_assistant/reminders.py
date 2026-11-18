"""提醒：论文/导师相关反复提醒，活动提醒一次，其余不提醒。

提醒只发生在本地界面（红点 + 提醒列表），不发送邮件、不调用外部服务。
重复节奏按 `POLICY` 固定，写成常量便于以后改成用户可配置。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import log_event, now_iso
from .models import Category, Notify

# 距离邮件首次出现的时间偏移（秒）。即时类会反复提醒到用户确认。
POLICY: dict[Notify, tuple[int, ...]] = {
    Notify.IMMEDIATE: (0, 15 * 60, 45 * 60, 2 * 3600, 6 * 3600),
    Notify.NORMAL: (0,),
}


@dataclass
class Reminder:
    id: int
    mail_id: int
    seq: int
    scheduled_at: datetime
    sent_at: datetime | None
    subject: str
    sender: str
    category: Category
    notify: Notify
    importance: str

    @property
    def nudge(self) -> int:
        return self.seq


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def plan(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """为尚未确认的提醒类邮件排好后续提醒，幂等。"""
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT id, notify, first_seen_at FROM mail_messages
        WHERE ack_at IS NULL AND notify != 'none'
          AND (server_unseen = 1 OR is_new = 1)
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        try:
            notify = Notify(row["notify"])
        except ValueError:
            continue
        offsets = POLICY.get(notify)
        if not offsets:
            continue
        base = _parse(row["first_seen_at"]) or now
        for seq, offset in enumerate(offsets, start=1):
            scheduled = base + timedelta(seconds=offset)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO reminders (mail_id, seq, scheduled_at) VALUES (?, ?, ?)",
                (row["id"], seq, scheduled.isoformat()),
            )
            inserted += cursor.rowcount
    conn.commit()
    return inserted


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[Reminder]:
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT r.id, r.mail_id, r.seq, r.scheduled_at, r.sent_at,
               m.subject, m.sender, m.category, m.notify, m.importance
        FROM reminders r JOIN mail_messages m ON m.id = r.mail_id
        WHERE r.sent_at IS NULL AND m.ack_at IS NULL
          AND (m.server_unseen = 1 OR m.is_new = 1)
          AND r.scheduled_at <= ?
        ORDER BY r.scheduled_at
        """,
        (now.isoformat(),),
    ).fetchall()
    return [
        Reminder(
            id=int(row["id"]),
            mail_id=int(row["mail_id"]),
            seq=int(row["seq"]),
            scheduled_at=_parse(row["scheduled_at"]) or now,
            sent_at=_parse(row["sent_at"]),
            subject=row["subject"],
            sender=row["sender"],
            category=Category(row["category"]),
            notify=Notify(row["notify"]),
            importance=row["importance"],
        )
        for row in rows
    ]


def tick(conn: sqlite3.Connection, now: datetime | None = None) -> list[Reminder]:
    """标记到期的提醒为已发送，并留下事件记录。"""
    now = now or datetime.now(UTC)
    plan(conn, now)
    fired = due(conn, now)
    for item in fired:
        conn.execute("UPDATE reminders SET sent_at = ? WHERE id = ?", (now_iso(), item.id))
        item.sent_at = now
    if fired:
        conn.commit()
        log_event(
            conn,
            "reminder.sent",
            {
                "count": len(fired),
                "items": [
                    {"mail_id": item.mail_id, "seq": item.seq, "subject": item.subject[:60]}
                    for item in fired
                ],
            },
        )
    return fired


def acknowledge(conn: sqlite3.Connection, mail_id: int) -> None:
    stamp = now_iso()
    conn.execute("UPDATE mail_messages SET ack_at = ? WHERE id = ?", (stamp, mail_id))
    conn.execute("UPDATE reminders SET ack_at = ? WHERE mail_id = ?", (stamp, mail_id))
    conn.commit()


def panel(conn: sqlite3.Connection, limit: int = 20) -> list[Reminder]:
    """界面用：未确认的提醒项（每封邮件只显示最近一条）。"""
    rows = conn.execute(
        """
        SELECT r.id, r.mail_id, r.seq, r.scheduled_at, r.sent_at,
               m.subject, m.sender, m.category, m.notify, m.importance
        FROM reminders r JOIN mail_messages m ON m.id = r.mail_id
        WHERE m.ack_at IS NULL AND (m.server_unseen = 1 OR m.is_new = 1)
          AND r.id = (SELECT MAX(r2.id) FROM reminders r2 WHERE r2.mail_id = r.mail_id)
        ORDER BY m.importance DESC, r.scheduled_at
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        Reminder(
            id=int(row["id"]),
            mail_id=int(row["mail_id"]),
            seq=int(row["seq"]),
            scheduled_at=_parse(row["scheduled_at"]) or datetime.now(UTC),
            sent_at=_parse(row["sent_at"]),
            subject=row["subject"],
            sender=row["sender"],
            category=Category(row["category"]),
            notify=Notify(row["notify"]),
            importance=row["importance"],
        )
        for row in rows
    ]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM reminders r JOIN mail_messages m ON m.id = r.mail_id
             WHERE r.sent_at IS NULL AND m.ack_at IS NULL
               AND (m.server_unseen = 1 OR m.is_new = 1)) AS pending,
            (SELECT COUNT(*) FROM reminders r JOIN mail_messages m ON m.id = r.mail_id
             WHERE r.sent_at IS NOT NULL AND r.ack_at IS NULL AND m.ack_at IS NULL
               AND (m.server_unseen = 1 OR m.is_new = 1)) AS sent
        """
    ).fetchone()
    return {"pending": int(row["pending"]), "sent": int(row["sent"])}
