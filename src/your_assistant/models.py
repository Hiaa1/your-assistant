"""领域对象：与存储、框架无关的纯数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Importance(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    BULK = "bulk"

    @property
    def rank(self) -> int:
        return _IMPORTANCE_RANK[self]


_IMPORTANCE_RANK = {
    Importance.CRITICAL: 4,
    Importance.HIGH: 3,
    Importance.NORMAL: 2,
    Importance.LOW: 1,
    Importance.BULK: 0,
}


class Category(StrEnum):
    """邮件分类：决定界面分组与提醒策略。"""

    PAPER_ADVISOR = "paper_advisor"
    CAREER_TALK = "career_talk"
    FORUM = "forum"
    EVENT = "event"
    ANNOUNCEMENT = "announcement"
    ADVERTISEMENT = "advertisement"
    OTHER = "other"


class Notify(StrEnum):
    IMMEDIATE = "immediate"  # 立即处理并反复提醒
    NORMAL = "normal"  # 正常提醒一次
    NONE = "none"  # 不提醒


CATEGORY_LABELS: dict[Category, str] = {
    Category.PAPER_ADVISOR: "论文/导师",
    Category.CAREER_TALK: "宣讲会",
    Category.FORUM: "论坛",
    Category.EVENT: "活动",
    Category.ANNOUNCEMENT: "学校公示",
    Category.ADVERTISEMENT: "外部广告",
    Category.OTHER: "其他",
}

# 需求约定：论文/导师立即提醒；活动正常提醒；其余不提醒。
NOTIFY_BY_CATEGORY: dict[Category, Notify] = {
    Category.PAPER_ADVISOR: Notify.IMMEDIATE,
    Category.EVENT: Notify.NORMAL,
}


class TaskStatus(StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    SNOOZED = "snoozed"
    DROPPED = "dropped"


class Capability(StrEnum):
    """软件对邮箱、通知等外部系统的写入能力。

    最新版需求是只读邮箱；这里把"能不能写"变成显式开关，
    以后需求变化只改配置，不改用例层。
    """

    READ_MAIL = "read_mail"
    DRAFT = "draft"
    SEND_MAIL = "send_mail"
    DELETE_MAIL = "delete_mail"
    NOTIFY = "notify"


@dataclass(frozen=True)
class Hit:
    """检索结果。`path:start_line-end_line` 就是"能定位到原文"的凭证。"""

    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    text: str = ""

    @property
    def citation(self) -> str:
        if self.end_line <= self.start_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class MailMessage:
    message_id: str
    thread_key: str
    subject: str
    sender: str
    recipients: tuple[str, ...]
    sent_at: datetime
    body_text: str
    body_html: str = ""
    account: str = ""
    uidvalidity: int = 0
    uid: int = 0
    importance: Importance = Importance.NORMAL
    category: Category = Category.OTHER
    notify: Notify = Notify.NONE
    tags: tuple[str, ...] = ()
    raw_path: str = ""
    id: int | None = None
    server_unseen: bool = False
    is_new: bool = True
    ack_at: datetime | None = None
    classification_reason: str = ""

    @property
    def unread(self) -> bool:
        """服务器未读，或"同步进来的新邮件"尚未在本界面确认。"""
        return self.ack_at is None and (self.server_unseen or self.is_new)


@dataclass(frozen=True)
class Classification:
    importance: Importance = Importance.NORMAL
    category: Category = Category.OTHER
    notify: Notify = Notify.NONE
    tags: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class Thread:
    thread_key: str
    subject: str
    participants: tuple[str, ...]
    message_ids: tuple[str, ...]
    last_at: datetime
    importance: Importance = Importance.NORMAL
    summary: str = ""


@dataclass
class Task:
    title: str
    id: int | None = None
    due_at: datetime | None = None
    status: TaskStatus = TaskStatus.TODO
    source_kind: str = ""  # "mail" / "note" / "manual"
    source_ref: str = ""
    notes: str = ""
    checklist: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Draft:
    """草稿与确认。`revision` 是内容哈希：确认时对不上就拒绝，避免确认了别的内容。"""

    kind: str  # "briefing" / "reply" / "task_note"
    target_ref: str
    body: str
    revision: str = ""
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_path: str = ""


@dataclass
class Lesson:
    text: str
    source: str = ""
    tags: tuple[str, ...] = ()
    created_at: datetime | None = None


@dataclass
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime | None = None
    id: int | None = None


@dataclass
class SyncResult:
    account: str = ""
    scanned: int = 0
    new: int = 0
    errors: tuple[str, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None
    ok: bool = True

    def summary(self) -> str:
        if not self.ok:
            return f"同步失败：{'；'.join(self.errors) or '未知错误'}"
        return f"扫描 {self.scanned} 封，新增 {self.new} 封"


@dataclass
class DeliveryResult:
    ok: bool
    detail: str = ""
    uncertain: bool = False
