"""SQLite 连接与迁移。数据库只是索引与缓存，删掉不丢失规则/记忆。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATIONS: tuple[str, ...] = (
    # 1: 事件审计（所有对外动作都留痕）
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, at);
    """,
    # 2: 资料库索引（文档 + 段落块 + FTS5）
    """
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL UNIQUE,
        mtime REAL NOT NULL DEFAULT 0,
        size INTEGER NOT NULL DEFAULT 0,
        sha TEXT NOT NULL,
        indexed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        text TEXT NOT NULL,
        tokens TEXT NOT NULL,
        heading TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS blocks_doc ON blocks(doc_id);
    CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(tokens);
    """,
    # 3: 只读邮件缓存（本地已读/新邮件状态，不写回服务器）
    """
    CREATE TABLE IF NOT EXISTS mail_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT NOT NULL,
        uidvalidity INTEGER NOT NULL,
        uid INTEGER NOT NULL,
        message_id TEXT NOT NULL UNIQUE,
        thread_key TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        sender TEXT NOT NULL DEFAULT '',
        recipients TEXT NOT NULL DEFAULT '[]',
        sent_at TEXT,
        body_text TEXT NOT NULL DEFAULT '',
        importance TEXT NOT NULL DEFAULT 'normal',
        tags TEXT NOT NULL DEFAULT '[]',
        classification_reason TEXT NOT NULL DEFAULT '',
        server_unseen INTEGER NOT NULL DEFAULT 0,
        is_new INTEGER NOT NULL DEFAULT 1,
        ack_at TEXT,
        raw_path TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        UNIQUE(account, uidvalidity, uid)
    );
    CREATE INDEX IF NOT EXISTS mail_thread ON mail_messages(thread_key, sent_at);
    CREATE INDEX IF NOT EXISTS mail_unread ON mail_messages(ack_at, importance);
    CREATE TABLE IF NOT EXISTS mail_sync_state (
        account TEXT PRIMARY KEY,
        uidvalidity INTEGER NOT NULL DEFAULT 0,
        last_uid INTEGER NOT NULL DEFAULT 0,
        initial_done INTEGER NOT NULL DEFAULT 0,
        last_sync_at TEXT,
        last_error TEXT
    );
    """,
    # 4: 失败待重试的 UID（网络抖动时不丢信）
    """
    ALTER TABLE mail_sync_state ADD COLUMN pending TEXT NOT NULL DEFAULT '[]';
    """,
    # 5: 邮件分类 + 提醒
    """
    ALTER TABLE mail_messages ADD COLUMN category TEXT NOT NULL DEFAULT 'other';
    ALTER TABLE mail_messages ADD COLUMN notify TEXT NOT NULL DEFAULT 'none';
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mail_id INTEGER NOT NULL REFERENCES mail_messages(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        scheduled_at TEXT NOT NULL,
        sent_at TEXT,
        ack_at TEXT,
        UNIQUE(mail_id, seq)
    );
    CREATE INDEX IF NOT EXISTS reminders_due ON reminders(scheduled_at, sent_at);
    """,
    # 6: 确认稿（手机编辑 + 版本哈希确认）
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        body TEXT NOT NULL,
        revision TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_at TEXT,
        confirmed_path TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS drafts_target ON drafts(target_ref, kind, id);
    """,
    # 7: 邮件 HTML 正文（展示邮件里的图片与排版）
    """
    ALTER TABLE mail_messages ADD COLUMN body_html TEXT NOT NULL DEFAULT '';
    """,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """按 PRAGMA user_version 顺序执行迁移，返回当前版本。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for index in range(version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        conn.execute(f"PRAGMA user_version = {index + 1}")
        conn.commit()
    return len(MIGRATIONS)


def log_event(conn: sqlite3.Connection, kind: str, payload: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO events (kind, payload, at) VALUES (?, ?, ?)",
        (kind, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
    )
    conn.commit()
