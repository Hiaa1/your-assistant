"""后台调度：定时拉邮件、更新索引。只读，不会写邮箱。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .db import connect, migrate
from .imap import ImapMailSource
from .knowledge import KnowledgeIndex
from .mail import MailRepository, RuleClassifier
from .reminders import tick as reminder_tick
from .tasks import archive_day

log = logging.getLogger("your-assistant.worker")


def _with_connection(settings: Settings, action) -> None:
    connection = connect(settings.db_path)
    try:
        migrate(connection)
        action(connection)
    except Exception:  # noqa: BLE001 - 后台任务失败只记录，不拖垮进程
        log.exception("后台任务失败")
    finally:
        connection.close()


def sync_mail(settings: Settings) -> None:
    def action(connection) -> None:
        source = ImapMailSource(
            settings, MailRepository(connection), RuleClassifier(settings.mail_rules_file)
        )
        result = source.sync()
        if not result.ok:
            log.warning("邮件同步失败：%s", result.errors)
        else:
            log.info("邮件同步：%s", result.summary())

    _with_connection(settings, action)


def sync_index(settings: Settings) -> None:
    def action(connection) -> None:
        index = KnowledgeIndex(connection, settings.index_roots)
        written, deleted = index.sync()
        if written or deleted:
            log.info("索引更新：写入 %s 块，删除 %s 个文档", written, deleted)

    _with_connection(settings, action)


def tick_reminders(settings: Settings) -> None:
    def action(connection) -> None:
        fired = reminder_tick(connection)
        if fired:
            log.info("提醒：%s", ", ".join(item.subject[:30] for item in fired))

    _with_connection(settings, action)


def run_digest(settings: Settings) -> None:
    from .review import run_digest as build_digest
    from .review import write_digest

    day = datetime.now(ZoneInfo(settings.timezone)).date()
    digest = build_digest(settings, day)
    write_digest(settings, digest)
    log.info("每日复盘：%s", digest.review_path)


def archive_tasks(settings: Settings) -> None:
    """每天 00:05 把昨天的任务状态写进月度归档。"""
    yesterday = datetime.now(ZoneInfo(settings.timezone)).date() - timedelta(days=1)
    path = archive_day(settings.vault_dir, yesterday)
    if path:
        log.info("任务归档：%s", path)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        sync_mail,
        "interval",
        seconds=max(60, settings.mail_interval_seconds),
        args=[settings],
        id="mail-sync",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(ZoneInfo(settings.timezone)),
    )
    try:
        hour, minute = (int(part) for part in settings.daily_review_at.split(":", 1))
    except ValueError:
        hour, minute = 20, 0
    scheduler.add_job(
        archive_tasks,
        "cron",
        hour=0,
        minute=5,
        args=[settings],
        id="task-archive",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_digest,
        "cron",
        hour=hour,
        minute=minute,
        args=[settings],
        id="daily-review",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        tick_reminders,
        "interval",
        seconds=60,
        args=[settings],
        id="reminders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync_index,
        "interval",
        minutes=10,
        args=[settings],
        id="index-sync",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(ZoneInfo(settings.timezone)),
    )
    return scheduler
