"""邮件缓存与规则分类。

原则：
- 服务器是只读的。这里只缓存正文、维护**本地**的已读/新邮件状态。
- 分类优先用 `rules/mail_rules.yaml`；用户改判会追加 `learned` 规则，下次直接命中。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .db import now_iso
from .models import (
    CATEGORY_LABELS,
    NOTIFY_BY_CATEGORY,
    Category,
    Classification,
    Importance,
    MailMessage,
    Notify,
)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def normalize_subject(subject: str) -> str:
    text = re.sub(r"(?i)^\s*((re|fw|fwd|答复|转发)\s*[:：]\s*)+", "", subject or "")
    text = re.sub(r"^\s*\[[^\]]{1,20}\]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def thread_key_for(
    message_id: str, references: Sequence[str], in_reply_to: str, subject: str
) -> str:
    """优先用引用链根节点；没有引用时退化为规范化主题。"""
    if references:
        return references[0]
    if in_reply_to:
        return in_reply_to
    if message_id:
        return message_id
    return f"subject:{normalize_subject(subject)}"


IMPORTANCE_BY_CATEGORY: dict[Category, Importance] = {
    Category.PAPER_ADVISOR: Importance.CRITICAL,
    Category.EVENT: Importance.NORMAL,
    Category.CAREER_TALK: Importance.LOW,
    Category.FORUM: Importance.LOW,
    Category.ANNOUNCEMENT: Importance.LOW,
    Category.ADVERTISEMENT: Importance.BULK,
    Category.OTHER: Importance.NORMAL,
}


def _category(raw: str | None) -> Category:
    try:
        return Category(str(raw))
    except ValueError:
        return Category.OTHER


def _notify(raw: str | None) -> Notify:
    try:
        return Notify(str(raw))
    except ValueError:
        return Notify.NONE


def _row_to_message(row: sqlite3.Row) -> MailMessage:
    return MailMessage(
        id=int(row["id"]),
        account=row["account"],
        uidvalidity=int(row["uidvalidity"]),
        uid=int(row["uid"]),
        message_id=row["message_id"],
        thread_key=row["thread_key"],
        subject=row["subject"],
        sender=row["sender"],
        recipients=tuple(json.loads(row["recipients"] or "[]")),
        sent_at=_parse_dt(row["sent_at"]) or datetime.now(UTC),
        body_text=row["body_text"],
        body_html=row["body_html"],
        importance=Importance(row["importance"]),
        category=_category(row["category"]),
        notify=_notify(row["notify"]),
        tags=tuple(json.loads(row["tags"] or "[]")),
        classification_reason=row["classification_reason"],
        server_unseen=bool(row["server_unseen"]),
        is_new=bool(row["is_new"]),
        ack_at=_parse_dt(row["ack_at"]),
        raw_path=row["raw_path"],
    )


class MailRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 同步状态 ----
    def sync_state(self, account: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM mail_sync_state WHERE account = ?", (account,)
        ).fetchone()

    def save_sync_state(
        self,
        account: str,
        *,
        uidvalidity: int,
        last_uid: int,
        initial_done: bool,
        error: str = "",
        pending: Sequence[int] = (),
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO mail_sync_state
                (account, uidvalidity, last_uid, initial_done, last_sync_at, last_error, pending)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET
                uidvalidity = excluded.uidvalidity,
                last_uid = excluded.last_uid,
                initial_done = excluded.initial_done,
                last_sync_at = excluded.last_sync_at,
                last_error = excluded.last_error,
                pending = excluded.pending
            """,
            (
                account,
                uidvalidity,
                last_uid,
                int(initial_done),
                now_iso(),
                error,
                json.dumps(sorted(set(pending))),
            ),
        )
        self.conn.commit()

    def pending_uids(self, account: str) -> list[int]:
        state = self.sync_state(account)
        if not state:
            return []
        try:
            return [int(uid) for uid in json.loads(state["pending"] or "[]")]
        except (TypeError, ValueError):
            return []

    def last_sync_at(self, account: str) -> datetime | None:
        row = self.sync_state(account)
        return _parse_dt(row["last_sync_at"]) if row else None

    # ---- 消息 ----
    def exists(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mail_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def insert(
        self,
        message: MailMessage,
        *,
        is_new: bool,
        classification: Classification,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO mail_messages (
                account, uidvalidity, uid, message_id, thread_key, subject, sender,
                recipients, sent_at, body_text, body_html, importance, category, notify, tags,
                classification_reason, server_unseen, is_new, raw_path, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.account,
                message.uidvalidity,
                message.uid,
                message.message_id,
                message.thread_key,
                message.subject,
                message.sender,
                json.dumps(list(message.recipients), ensure_ascii=False),
                _iso(message.sent_at),
                message.body_text,
                message.body_html,
                classification.importance.value,
                classification.category.value,
                classification.notify.value,
                json.dumps(list(classification.tags), ensure_ascii=False),
                classification.reason,
                int(message.server_unseen),
                int(is_new),
                message.raw_path,
                now_iso(),
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get(self, message_id: int) -> MailMessage | None:
        row = self.conn.execute(
            "SELECT * FROM mail_messages WHERE id = ?", (message_id,)
        ).fetchone()
        return _row_to_message(row) if row else None

    def list_unread(self, limit: int = 50) -> list[MailMessage]:
        rows = self.conn.execute(
            """
            SELECT * FROM mail_messages
            WHERE ack_at IS NULL AND (server_unseen = 1 OR is_new = 1)
            ORDER BY COALESCE(sent_at, '') DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row_to_message(row) for row in rows]

    def list_recent(self, limit: int = 50) -> list[MailMessage]:
        rows = self.conn.execute(
            "SELECT * FROM mail_messages ORDER BY COALESCE(sent_at, '') DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_message(row) for row in rows]

    def list_filtered(
        self,
        *,
        unread_only: bool = False,
        importance: Importance | None = None,
        category: Category | None = None,
        query: str = "",
        limit: int = 50,
    ) -> list[MailMessage]:
        clauses: list[str] = []
        params: list[Any] = []
        if unread_only:
            clauses.append("ack_at IS NULL AND (server_unseen = 1 OR is_new = 1)")
        if importance is not None:
            clauses.append("importance = ?")
            params.append(importance.value)
        if category is not None:
            clauses.append("category = ?")
            params.append(category.value)
        if query:
            clauses.append("(subject LIKE ? OR sender LIKE ? OR body_text LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM mail_messages{where} ORDER BY COALESCE(sent_at, '') DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_message(row) for row in rows]

    def search_mail(self, query: str, limit: int = 20) -> list[MailMessage]:
        return self.list_filtered(query=query, limit=limit)

    def thread(self, thread_key: str, limit: int = 50) -> list[MailMessage]:
        rows = self.conn.execute(
            "SELECT * FROM mail_messages WHERE thread_key = ? "
            "ORDER BY COALESCE(sent_at, '') ASC LIMIT ?",
            (thread_key, limit),
        ).fetchall()
        return [_row_to_message(row) for row in rows]

    def ack(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE mail_messages SET ack_at = ? WHERE id = ? AND ack_at IS NULL",
            (now_iso(), message_id),
        )
        self.conn.commit()

    def set_classification(self, message_id: int, classification: Classification) -> None:
        self.conn.execute(
            "UPDATE mail_messages SET importance = ?, category = ?, notify = ?, tags = ?, "
            "classification_reason = ? WHERE id = ?",
            (
                classification.importance.value,
                classification.category.value,
                classification.notify.value,
                json.dumps(list(classification.tags), ensure_ascii=False),
                classification.reason,
                message_id,
            ),
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM mail_messages) AS total,
                (SELECT COUNT(*) FROM mail_messages
                 WHERE ack_at IS NULL AND (server_unseen = 1 OR is_new = 1)) AS unread,
                (SELECT COUNT(*) FROM mail_messages WHERE importance = 'critical') AS critical,
                (SELECT COUNT(*) FROM mail_messages
                 WHERE ack_at IS NULL AND (server_unseen = 1 OR is_new = 1)
                   AND importance IN ('critical', 'high')) AS important_unread,
                (SELECT COUNT(*) FROM mail_messages
                 WHERE ack_at IS NULL AND (server_unseen = 1 OR is_new = 1)
                   AND notify != 'none') AS alert
            """
        ).fetchone()
        return {
            key: int(row[key])
            for key in ("total", "unread", "critical", "alert", "important_unread")
        }


class RuleClassifier:
    """确定性分类器：先看导师联系人，再按规则顺序匹配。

    规则字段：`category`（论文/活动/宣讲会/论坛/公示/广告/其他）、
    可选 `importance`、可选 `notify`（immediate/normal/none），
    不写时按类别取默认策略（论文→立即，活动→正常，其余→不提醒）。
    """

    def __init__(self, rules_file: Path, default: Importance = Importance.NORMAL) -> None:
        self.rules_file = rules_file
        self.default = default
        self._cache: list[dict[str, Any]] = []
        self._advisors: list[str] = []
        self._mtime: float | None = None

    def _load(self) -> list[dict[str, Any]]:
        if not self.rules_file.exists():
            return []
        mtime = self.rules_file.stat().st_mtime
        if self._mtime == mtime:
            return self._cache
        data = yaml.safe_load(self.rules_file.read_text(encoding="utf-8")) or {}
        rules: list[dict[str, Any]] = []
        for section in ("learned", "base"):
            for rule in data.get(section) or []:
                if isinstance(rule, dict):
                    rules.append({**rule, "_section": section})
        contacts = (data.get("contacts") or {}).get("advisors") or []
        self._advisors = [str(item).lower() for item in contacts if str(item).strip()]
        self._cache, self._mtime = rules, mtime
        return rules

    def classify(self, sender: str, subject: str) -> Classification:
        rules = self._load()
        lowered_sender = (sender or "").lower()
        address = _sender_address(sender)
        for advisor in self._advisors:
            if advisor and (advisor in lowered_sender or advisor == address):
                return Classification(
                    importance=Importance.CRITICAL,
                    category=Category.PAPER_ADVISOR,
                    notify=Notify.IMMEDIATE,
                    tags=("advisor",),
                    reason="联系人：导师/合作者",
                )

        for rule in rules:
            when = rule.get("when") or {}
            from_contains = [str(item).lower() for item in when.get("from_contains") or []]
            if from_contains and not any(item in lowered_sender for item in from_contains):
                continue
            pattern = when.get("subject_regex")
            if pattern and not re.search(pattern, subject or "", flags=re.I):
                continue
            if not from_contains and not pattern:
                continue
            category = _category(rule.get("category"))
            importance = (
                Importance(str(rule["importance"]))
                if rule.get("importance")
                else IMPORTANCE_BY_CATEGORY[category]
            )
            notify = (
                _notify(rule["notify"])
                if rule.get("notify")
                else NOTIFY_BY_CATEGORY.get(category, Notify.NONE)
            )
            tags = tuple(str(tag) for tag in rule.get("tags") or [])
            return Classification(
                importance=importance,
                category=category,
                notify=notify,
                tags=tags,
                reason=f"{CATEGORY_LABELS[category]}·规则「{rule.get('name', '未命名')}」",
            )
        return Classification(
            importance=self.default,
            category=Category.OTHER,
            notify=Notify.NONE,
            reason="默认：没有命中规则",
        )

    def learn(
        self,
        *,
        sender: str,
        subject: str,
        category: Category,
        tags: Sequence[str],
        evidence: str,
    ) -> str:
        """把用户的改判写成一条可读、可回滚的规则。返回规则名。"""
        data = (
            yaml.safe_load(self.rules_file.read_text(encoding="utf-8"))
            if self.rules_file.exists()
            else {}
        ) or {}
        learned = data.setdefault("learned", [])
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
        name = f"learned-{stamp}-{len(learned) + 1}"
        address = _sender_address(sender)
        rule: dict[str, Any] = {
            "name": name,
            "when": {"from_contains": [address or sender.lower()]},
            "category": category.value,
            "tags": list(tags) or ["learned"],
            # 下面两项只给人看，不参与匹配：按发件人泛化，避免"只有这一封有用"
            "subject_hint": subject,
            "evidence": [evidence],
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        learned.append(rule)
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        self.rules_file.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self._mtime = None
        return name


def reclassify_messages(conn: sqlite3.Connection, classifier: RuleClassifier) -> int:
    """用当前规则重算所有已缓存邮件（规则改动后跑一次）。"""
    rows = conn.execute("SELECT id, sender, subject FROM mail_messages").fetchall()
    for row in rows:
        result = classifier.classify(row["sender"], row["subject"])
        conn.execute(
            "UPDATE mail_messages SET importance = ?, category = ?, notify = ?, "
            "classification_reason = ? WHERE id = ?",
            (
                result.importance.value,
                result.category.value,
                result.notify.value,
                result.reason,
                row["id"],
            ),
        )
    conn.commit()
    return len(rows)


def _sender_address(sender: str) -> str:
    """从 "名字 <a@b>" 里取出地址；learned 规则按地址（而不是整个域名）泛化。"""
    match = re.search(r"<([^<>@\s]+@[^<>\s]+)>", sender or "")
    if match:
        return match.group(1).lower()
    match = re.search(r"[\w.+-]+@[\w.-]+", sender or "")
    return match.group(0).lower() if match else ""


__all__ = [
    "IMPORTANCE_BY_CATEGORY",
    "MailRepository",
    "RuleClassifier",
    "normalize_subject",
    "reclassify_messages",
    "thread_key_for",
]
