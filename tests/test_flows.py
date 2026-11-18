from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from your_assistant.db import migrate
from your_assistant.flows import (
    build_briefing,
    confirm_draft,
    get_draft,
    latest_briefing,
    revision_of,
    save_draft,
)
from your_assistant.mail import MailRepository
from your_assistant.models import Category, Classification, Importance, MailMessage, Notify
from your_assistant.reminders import acknowledge as acknowledge_mail


@pytest.fixture
def flow_env(settings, conn):
    migrate(conn)
    note = settings.vault_dir / "notes" / "deadline.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "# 报名信息\n\n活动报名截止时间为 11 月 20 日，活动开始时间是 11 月 25 日。\n",
        encoding="utf-8",
    )
    repo = MailRepository(conn)
    repo.insert(
        MailMessage(
            message_id="flow@x",
            thread_key="flow@x",
            subject="讲座报名通知",
            sender="organizer@shanghaitech.edu.cn",
            recipients=("me@x",),
            sent_at=datetime.now(UTC) - timedelta(hours=1),
            body_text="请于 11 月 20 日前报名，活动 11 月 25 日开始。",
            account="me@x",
            uidvalidity=1,
            uid=1,
            server_unseen=True,
        ),
        is_new=True,
        classification=Classification(
            importance=Importance.CRITICAL,
            category=Category.PAPER_ADVISOR,
            notify=Notify.IMMEDIATE,
            tags=("paper",),
            reason="测试",
        ),
    )
    mail_id = int(conn.execute("SELECT id FROM mail_messages").fetchone()[0])
    return settings, conn, mail_id


def test_briefing_contains_mail_and_note_citations(flow_env):
    settings, conn, mail_id = flow_env
    draft = build_briefing(settings, conn, mail_id)

    assert draft.kind == "briefing"
    assert draft.target_ref == f"mail:{mail_id}"
    assert "邮件要点" in draft.body
    assert "报名截止" in draft.body
    assert "deadline.md:" in draft.body, "确认稿必须带资料引用"
    assert "确认前检查" in draft.body
    assert draft.revision == revision_of(draft.body)
    assert get_draft(conn, draft.id) is not None
    assert latest_briefing(conn, mail_id).id == draft.id


def test_briefing_does_not_cite_previous_reviews(flow_env):
    settings, conn, mail_id = flow_env
    review = settings.vault_dir / "reviews" / "2026-11-17.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("讲座 报名 通知 的每日复盘，活动报名截止时间。\n", encoding="utf-8")

    draft = build_briefing(settings, conn, mail_id)
    assert "deadline.md:" in draft.body
    assert "reviews/" not in draft.body, "不能引用 AI 自己写的复盘，避免自我循环"


def test_save_changes_revision_and_stale_confirm_is_rejected(flow_env):
    settings, conn, mail_id = flow_env
    draft = build_briefing(settings, conn, mail_id)
    old_revision = draft.revision

    updated = save_draft(conn, draft.id, draft.body + "\n\n我的决定：参加，11 月 20 日前报名。\n")
    assert updated.revision != old_revision

    stale = confirm_draft(settings, conn, draft.id, old_revision)
    assert not stale.ok
    assert stale.reason == "stale"
    assert not (settings.vault_dir / "outbox").exists(), "拒绝时不应写归档"

    ok = confirm_draft(settings, conn, draft.id, updated.revision)
    assert ok.ok and ok.path
    content = ok.path.read_text(encoding="utf-8")
    assert "我的决定：参加" in content
    assert "已确认" in content
    assert ok.path.parent == settings.vault_dir / "outbox"
    assert get_draft(conn, draft.id).confirmed_at is not None

    # 确认会停掉提醒，并把邮件标记为已处理
    assert conn.execute("SELECT ack_at FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()[0]

    again = confirm_draft(settings, conn, draft.id, updated.revision)
    assert not again.ok and again.reason == "already"
    assert len(list((settings.vault_dir / "outbox").glob("*.md"))) == 1


def test_confirmed_draft_cannot_be_edited(flow_env):
    settings, conn, mail_id = flow_env
    draft = build_briefing(settings, conn, mail_id)
    assert confirm_draft(settings, conn, draft.id, draft.revision).ok
    with pytest.raises(ValueError):
        save_draft(conn, draft.id, "改不动了")


def test_acknowledge_mail_clears_only_local_state(flow_env):
    settings, conn, mail_id = flow_env
    acknowledge_mail(conn, mail_id)
    assert conn.execute("SELECT ack_at FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()[0]
