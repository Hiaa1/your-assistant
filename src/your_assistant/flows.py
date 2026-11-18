"""跨工具流程：邮件 → 确认稿（带资料引用）→ 手机编辑 → 确认归档。

确认的核心是**版本哈希**：页面里带着你看到的那一版的 revision，
提交时与服务端当前 revision 比对；不一致就拒绝，避免"确认了别的内容"。
确认成功后写入 `vault/outbox/`（人可读、可 diff），并停止该邮件的提醒。
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jieba

from .config import Settings
from .db import log_event, now_iso
from .knowledge import KnowledgeIndex
from .mail import MailRepository
from .models import (
    CATEGORY_LABELS,
    Draft,
    MailMessage,
    Notify,
)
from .reminders import acknowledge as acknowledge_mail

NOTIFY_HINTS = {
    Notify.IMMEDIATE: "立即提醒",
    Notify.NORMAL: "正常提醒",
    Notify.NONE: "不提醒",
}


def revision_of(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:12]


def mail_target(mail_id: int) -> str:
    return f"mail:{mail_id}"


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=int(row["id"]),
        kind=row["kind"],
        target_ref=row["target_ref"],
        body=row["body"],
        revision=row["revision"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        confirmed_at=_parse_dt(row["confirmed_at"]),
        confirmed_path=row["confirmed_path"],
    )


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Draft | None:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def latest_briefing(conn: sqlite3.Connection, mail_id: int) -> Draft | None:
    row = conn.execute(
        "SELECT * FROM drafts WHERE target_ref = ? AND kind = 'briefing' ORDER BY id DESC LIMIT 1",
        (mail_target(mail_id),),
    ).fetchone()
    return _row_to_draft(row) if row else None


def _slug(subject: str, fallback: str = "briefing") -> str:
    text = re.sub(r"[\\/:*?\"<>|\s]+", "-", subject or "").strip("-")
    text = re.sub(r"-{2,}", "-", text)[:24]
    return text or fallback


def _match_lessons(rules_file: Path, text: str, limit: int = 4) -> list[str]:
    if not rules_file.exists():
        return []
    lessons = [
        line.strip()[2:].strip()
        for line in rules_file.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ")
    ]
    words = {word for word in jieba.lcut(text) if len(word) >= 2}
    scored: list[tuple[int, str]] = []
    for lesson in lessons:
        lesson_words = {word for word in jieba.lcut(lesson) if len(word) >= 2}
        overlap = len(words & lesson_words)
        if overlap >= 2:
            scored.append((overlap, lesson))
    scored.sort(reverse=True)
    return [lesson for _, lesson in scored[:limit]]


def _excerpt(text: str, limit: int = 900) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _note_hits(hits) -> list:
    """确认稿只引用个人资料，不引用过去的 AI 复盘/归档，避免自我循环。"""
    return [hit for hit in hits if "/reviews/" not in hit.path and "/outbox/" not in hit.path]


def render_briefing(
    message: MailMessage,
    hits,
    lessons: list[str],
    *,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now().astimezone()
    lines = [
        f"# 处理确认稿：{message.subject or '（无主题）'}",
        "",
        f"- 邮件：{message.sender} · {message.sent_at:%Y-%m-%d %H:%M}（本地缓存 #{message.id}）",
        f"- 分类：{CATEGORY_LABELS[message.category]} · {NOTIFY_HINTS.get(message.notify, '')}",
        f"- 生成时间：{now:%Y-%m-%d %H:%M}",
        "",
        "## 邮件要点（节选）",
        "",
        _excerpt(message.body_text) or "（正文为空或过大，请查看邮箱客户端）",
        "",
        "## 相关资料（来自 vault）",
        "",
    ]
    if hits:
        for hit in hits:
            lines.append(f"- `{hit.path}:{hit.start_line}-{hit.end_line}` — {hit.snippet}")
    else:
        lines.append("- 资料库里没有找到直接相关的内容。")
    lines += ["", "## 相关经验（rules/lessons.md）", ""]
    lines += [f"- {lesson}" for lesson in lessons] or ["- （无命中）"]
    lines += [
        "",
        "## 我的处理决定",
        "",
        "（在这里写：是否参加 / 报名截止 / 需要回复的内容 / 其他安排）",
        "",
        "## 确认前检查",
        "",
        "- [ ] 时间、地点、截止时间分别核对（不要把报名截止当成活动开始）",
        "- [ ] 引用的资料确实与本次事件相关",
        "- [ ] 以上内容是我确认过的最终版本",
        "",
    ]
    return "\n".join(lines)


def build_briefing(settings: Settings, conn: sqlite3.Connection, mail_id: int) -> Draft:
    message = MailRepository(conn).get(mail_id)
    if not message:
        raise ValueError("邮件不存在")

    index = KnowledgeIndex(conn, settings.index_roots)
    with contextlib.suppress(Exception):  # 索引失败不应阻塞确认稿
        index.sync()
    # 用主题里的词做 OR 检索：只要有一段资料相关就引用，避免 AND 过严一条都搜不到
    hits = _note_hits(index.search(message.subject, limit=8, match_all=False))
    if not hits:
        hits = _note_hits(index.search(message.subject, limit=8))
    lessons = _match_lessons(settings.rules_file, f"{message.subject}\n{message.body_text[:2000]}")
    body = render_briefing(message, hits, lessons)
    revision = revision_of(body)
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO drafts (kind, target_ref, body, revision, created_at, updated_at) "
        "VALUES ('briefing', ?, ?, ?, ?, ?)",
        (mail_target(mail_id), body, revision, stamp, stamp),
    )
    conn.commit()
    log_event(conn, "draft.created", {"draft_id": cursor.lastrowid, "mail_id": mail_id})
    draft = get_draft(conn, int(cursor.lastrowid))
    assert draft is not None
    return draft


def save_draft(conn: sqlite3.Connection, draft_id: int, body: str) -> Draft:
    draft = get_draft(conn, draft_id)
    if not draft:
        raise ValueError("草稿不存在")
    if draft.confirmed_at:
        raise ValueError("草稿已确认，不能修改")
    revision = revision_of(body)
    conn.execute(
        "UPDATE drafts SET body = ?, revision = ?, updated_at = ? WHERE id = ?",
        (body, revision, now_iso(), draft_id),
    )
    conn.commit()
    log_event(conn, "draft.saved", {"draft_id": draft_id, "revision": revision})
    updated = get_draft(conn, draft_id)
    assert updated is not None
    return updated


@dataclass
class ConfirmResult:
    ok: bool
    reason: str = ""
    draft: Draft | None = None
    path: Path | None = None


def confirm_draft(
    settings: Settings,
    conn: sqlite3.Connection,
    draft_id: int,
    revision: str,
) -> ConfirmResult:
    draft = get_draft(conn, draft_id)
    if not draft:
        return ConfirmResult(False, "missing")
    if draft.confirmed_at:
        return ConfirmResult(
            False, "already", draft, Path(draft.confirmed_path) if draft.confirmed_path else None
        )
    if revision_of(draft.body) != revision or draft.revision != revision:
        return ConfirmResult(False, "stale", draft)

    today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    outbox = settings.vault_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    if draft.target_ref.startswith("mail:"):
        mail_id = int(draft.target_ref.split(":", 1)[1])
        message = MailRepository(conn).get(mail_id)
        subject = message.subject if message else draft.kind
    else:
        subject = draft.kind
    path = outbox / f"{today}-draft-{draft.id}-{_slug(subject)}.md"
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    content = (
        f"{draft.body.rstrip()}\n\n---\n\n"
        f"已确认：{stamp} · 草稿 #{draft.id} · 版本 `{draft.revision}`\n"
    )
    path.write_text(content, encoding="utf-8")

    conn.execute(
        "UPDATE drafts SET confirmed_at = ?, confirmed_path = ? WHERE id = ?",
        (now_iso(), str(path), draft.id),
    )
    if draft.target_ref.startswith("mail:"):
        acknowledge_mail(conn, int(draft.target_ref.split(":", 1)[1]))
    conn.commit()
    log_event(
        conn,
        "draft.confirmed",
        {"draft_id": draft.id, "path": str(path), "revision": draft.revision},
    )
    confirmed = get_draft(conn, draft.id)
    return ConfirmResult(True, "confirmed", confirmed, path)
