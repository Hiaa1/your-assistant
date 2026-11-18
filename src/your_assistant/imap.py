"""只读 IMAP 适配器。

结构性保证：本模块只调用 `login / select(readonly=True) / uid("search") /
uid("fetch", ... BODY.PEEK) / logout`，不提供 STORE / COPY / MOVE / EXPUNGE / APPEND。
`BODY.PEEK` 不会把邮件标成已读。
"""

from __future__ import annotations

import base64
import contextlib
import imaplib
import re
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .config import Settings
from .db import log_event
from .mail import MailRepository, RuleClassifier, thread_key_for
from .models import MailMessage, SyncResult

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_BODY_CHARS = 20_000
MAX_RAW_BYTES = 2 * 1024 * 1024  # 超过就只取头部 + 前 256KB 正文
PARTIAL_BODY_BYTES = 256 * 1024


def _imap_date(value: datetime) -> str:
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year}"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        return value


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


def _extract_body(message: Message) -> tuple[str, str]:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, "replace")
        except (LookupError, ValueError, AssertionError):
            continue
        if content_type == "text/plain":
            plain.append(text)
        else:
            html.append(text)
    raw_html = "\n".join(html)
    body = "\n\n".join(plain).strip() or _html_to_text(raw_html)
    return body[:MAX_BODY_CHARS], raw_html[:MAX_BODY_CHARS]


def _header_message_id(header: bytes) -> str:
    if not header:
        return ""
    message = message_from_bytes(header)
    return (message.get("Message-ID") or "").strip().strip("<>")


def _message_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [
        item.strip("<> \t\r\n") for item in re.findall(r"<[^<>]+>|[\w.@-]+", raw) if item.strip()
    ]


def _parse_message(
    raw: bytes,
    *,
    account: str,
    uidvalidity: int,
    uid: int,
    raw_path: str,
    unseen: bool,
    fallback_date: datetime | None = None,
    truncated: bool = False,
) -> MailMessage:
    message = message_from_bytes(raw)
    message_id = (message.get("Message-ID") or "").strip().strip("<>")
    subject = _decode(message.get("Subject"))
    sender = _decode(message.get("From"))
    recipients = tuple(
        item for item in (_decode(message.get("To")) or "").split(",") if item.strip()
    )
    sent_at = fallback_date
    if message.get("Date"):
        try:
            sent_at = parsedate_to_datetime(message["Date"])
        except (TypeError, ValueError):
            sent_at = fallback_date
    if sent_at is None:
        sent_at = datetime.now(UTC)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    references = _message_ids(message.get("References"))
    in_reply_to = (_message_ids(message.get("In-Reply-To")) or [""])[0]
    body, body_html = _extract_body(message)
    if not body and truncated:
        body = "（正文过大，未完整下载；请到邮箱客户端查看原文，链接与线程信息仍可用）"
    return MailMessage(
        message_id=message_id or f"uid:{uidvalidity}:{uid}",
        thread_key=thread_key_for(message_id, references, in_reply_to, subject),
        subject=subject,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        body_text=body,
        body_html=body_html,
        account=account,
        uidvalidity=uidvalidity,
        uid=uid,
        raw_path=raw_path,
        server_unseen=unseen,
    )


class ImapMailSource:
    def __init__(
        self,
        settings: Settings,
        repo: MailRepository,
        classifier: RuleClassifier,
        connector: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.classifier = classifier
        self.account = settings.email_address
        self._connector = connector or self._default_connector

    # ---- 连接 ----
    def _default_connector(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        context = ssl.create_default_context()
        if self.settings.imap_ssl:
            return imaplib.IMAP4_SSL(
                self.settings.imap_host, self.settings.imap_port, timeout=30, ssl_context=context
            )
        connection = imaplib.IMAP4(self.settings.imap_host, self.settings.imap_port, timeout=30)
        connection.starttls(ssl_context=context)
        return connection

    def _open(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        """校园网到邮件服务器时通时断，登录做有限重试。"""
        last: Exception | None = None
        for attempt in range(3):
            try:
                connection = self._connector()
                connection.login(self.settings.email_address, self.settings.email_password)
                return connection
            except (imaplib.IMAP4.error, OSError) as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise ConnectionError(f"IMAP 登录失败：{type(last).__name__}: {last}")

    # ---- 同步 ----
    def sync(self) -> SyncResult:
        started = datetime.now(UTC)
        result = SyncResult(account=self.account, started_at=started)
        if not self.settings.mail_configured:
            result.ok = False
            result.errors = ("邮箱未配置：缺少 ASSISTANT_EMAIL_ADDRESS / ASSISTANT_EMAIL_PASSWORD",)
            return result

        try:
            connection = self._open()
        except Exception as exc:  # noqa: BLE001 - 同步失败要写进状态供界面显示
            message = f"{type(exc).__name__}: {exc}"
            self._remember_failure(message)
            result.ok = False
            result.errors = (message,)
            result.finished_at = datetime.now(UTC)
            return result

        try:
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError(f"选择 INBOX 失败：{status}")
            uidvalidity = self._uidvalidity(connection)
            state = self.repo.sync_state(self.account)
            previous_validity = int(state["uidvalidity"]) if state else 0
            previous_uid = int(state["last_uid"]) if state else 0
            backfill = state is None or not int(state["initial_done"])
            reset = bool(state) and previous_validity not in (0, uidvalidity)

            uids = self._candidate_uids(connection, state, backfill, reset)
            last_ok_uid = previous_uid
            failed: list[int] = []
            for uid in uids:
                try:
                    if self._fetch_and_store(connection, uid, uidvalidity, is_new=not backfill):
                        result.new += 1
                    result.scanned += 1
                    last_ok_uid = max(last_ok_uid, uid)
                except (imaplib.IMAP4.error, OSError, ValueError) as exc:
                    failed.append(uid)
                    result.errors = (*result.errors, f"UID {uid}: {type(exc).__name__}: {exc}")

            self.repo.save_sync_state(
                self.account,
                uidvalidity=uidvalidity,
                last_uid=last_ok_uid,
                initial_done=True,
                pending=failed,
            )
            log_event(
                self.repo.conn,
                "mail.sync",
                {
                    "scanned": result.scanned,
                    "new": result.new,
                    "errors": list(result.errors),
                    "uidvalidity": uidvalidity,
                    "reset": reset,
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            self._remember_failure(message)
            result.ok = False
            result.errors = (*result.errors, message)
        finally:
            with contextlib.suppress(Exception):  # 退出失败不影响同步结果
                connection.logout()

        result.finished_at = datetime.now(UTC)
        result.ok = result.ok and not any("登录失败" in error for error in result.errors)
        return result

    def _remember_failure(self, message: str) -> None:
        state = self.repo.sync_state(self.account)
        self.repo.save_sync_state(
            self.account,
            uidvalidity=int(state["uidvalidity"]) if state else 0,
            last_uid=int(state["last_uid"]) if state else 0,
            initial_done=bool(state["initial_done"]) if state else False,
            error=message,
            pending=self.repo.pending_uids(self.account),
        )

    def _uidvalidity(self, connection: imaplib.IMAP4) -> int:
        values = connection.untagged_responses.get("UIDVALIDITY") or []
        if values:
            try:
                return int(values[0])
            except (TypeError, ValueError):
                return 0
        return 0

    def _search(self, connection: imaplib.IMAP4, criterion: str) -> list[int]:
        status, data = connection.uid("search", None, criterion)
        if status != "OK" or not data or not data[0]:
            return []
        return [int(item) for item in data[0].split()]

    def _candidate_uids(
        self,
        connection: imaplib.IMAP4,
        state: Any,
        backfill: bool,
        reset: bool,
    ) -> list[int]:
        limit = max(1, self.settings.initial_mail_limit)
        everything = self._search(connection, "ALL")
        unseen = self._search(connection, "UNSEEN")
        if backfill:
            return sorted(set(everything[-limit:]) | set(unseen))

        pending = self.repo.pending_uids(self.account)
        if reset:
            since = self.repo.last_sync_at(self.account) or (datetime.now(UTC) - timedelta(days=2))
            recent = self._search(connection, f"SINCE {_imap_date(since - timedelta(days=2))}")
            # UID 全部重排：按日期回看，靠 message_id 去重，不重复入库
            return sorted(set(everything[-limit:]) | set(unseen) | set(recent) | set(pending))

        last_uid = int(state["last_uid"]) if state else 0
        fresh = self._search(connection, f"UID {last_uid + 1}:*")
        return sorted(set(fresh) | set(unseen) | set(pending))

    def _fetch_and_store(
        self, connection: imaplib.IMAP4, uid: int, uidvalidity: int, *, is_new: bool
    ) -> bool:
        # 先只取头部判断是否已入库，避免每轮都把已知邮件的正文重下一遍
        header = self._fetch_header(connection, uid)
        message_id = _header_message_id(header)
        if message_id and self.repo.exists(message_id):
            return False

        unseen = self._is_unseen(connection, uid)
        size = self._message_size(connection, uid)
        truncated = bool(size and size > MAX_RAW_BYTES)
        raw = (
            self._fetch_truncated(connection, uid)
            if truncated
            else self._fetch_full(connection, uid)
        )
        if not raw:
            raise ValueError("没有返回内容")

        raw_path = self._store_raw(raw, uidvalidity, uid)
        message = _parse_message(
            raw,
            account=self.account,
            uidvalidity=uidvalidity,
            uid=uid,
            raw_path=str(raw_path),
            unseen=unseen,
            truncated=truncated,
        )
        if self.repo.exists(message.message_id):
            return False
        classification = self.classifier.classify(message.sender, message.subject)
        return self.repo.insert(message, is_new=is_new, classification=classification)

    def _message_size(self, connection: imaplib.IMAP4, uid: int) -> int:
        status, data = connection.uid("fetch", str(uid), "(RFC822.SIZE)")
        if status != "OK" or not data:
            return 0
        text = b" ".join(item for item in data if isinstance(item, bytes))
        match = re.search(rb"RFC822\.SIZE (\d+)", text)
        return int(match.group(1)) if match else 0

    def _fetch_full(self, connection: imaplib.IMAP4, uid: int) -> bytes:
        status, data = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
        if status != "OK" or not data:
            return b""
        return next((item[1] for item in data if isinstance(item, tuple)), b"")

    def _fetch_header(self, connection: imaplib.IMAP4, uid: int) -> bytes:
        status, data = connection.uid("fetch", str(uid), "(BODY.PEEK[HEADER])")
        if status != "OK" or not data:
            return b""
        return next((item[1] for item in data if isinstance(item, tuple)), b"")

    def _fetch_truncated(self, connection: imaplib.IMAP4, uid: int) -> bytes:
        """大附件邮件只取头部和正文前 256KB，避免一次拉几十 MB。"""
        header = self._fetch_header(connection, uid)
        status, data = connection.uid(
            "fetch", str(uid), f"(BODY.PEEK[TEXT]<0.{PARTIAL_BODY_BYTES}>)"
        )
        body = (
            next((item[1] for item in data if isinstance(item, tuple)), b"")
            if status == "OK" and data
            else b""
        )
        if header and body:
            return header + b"\n" + body
        return header or body

    def _is_unseen(self, connection: imaplib.IMAP4, uid: int) -> bool:
        status, data = connection.uid("fetch", str(uid), "(FLAGS)")
        if status != "OK":
            return False
        flags = b" ".join(item for item in data if isinstance(item, bytes))
        return b"\\seen" not in flags.lower()

    def _store_raw(self, raw: bytes, uidvalidity: int, uid: int) -> Path:
        directory = self.settings.data_dir / "mail" / self.account / str(uidvalidity)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uid}.eml"
        path.write_bytes(raw)
        return path


def mark_seen(settings: Settings, uid: int) -> bool:
    """在服务器上把邮件标为已读（写操作，需显式开启 ASSISTANT_MARK_SEEN_REMOTE）。"""
    if not settings.mail_configured or uid <= 0:
        return False
    context = ssl.create_default_context()
    connection = None
    try:
        if settings.imap_ssl:
            connection = imaplib.IMAP4_SSL(
                settings.imap_host, settings.imap_port, timeout=20, ssl_context=context
            )
        else:
            connection = imaplib.IMAP4(settings.imap_host, settings.imap_port, timeout=20)
            connection.starttls(ssl_context=context)
        connection.login(settings.email_address, settings.email_password)
        connection.select("INBOX", readonly=False)
        connection.uid("store", str(uid), "+FLAGS", "(\\Seen)")
        return True
    except (imaplib.IMAP4.error, OSError):
        return False
    finally:
        if connection is not None:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                connection.logout()


def inline_cid_images(html: str, raw_path: str) -> str:
    """把正文里的 cid: 图片替换成 data URI，只在详情页渲染时做。"""
    if "cid:" not in html or not raw_path:
        return html
    path = Path(raw_path)
    if not path.is_file():
        return html
    try:
        message = message_from_bytes(path.read_bytes(), policy=policy.default)
    except (OSError, ValueError):
        return html
    mapping: dict[str, str] = {}
    for part in message.walk():
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if not cid:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        encoded = base64.b64encode(payload).decode("ascii")
        mapping[cid] = f"data:{part.get_content_type()};base64,{encoded}"
    for cid, data_uri in mapping.items():
        html = html.replace(f"cid:{cid}", data_uri)
    return html
