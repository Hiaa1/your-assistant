from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from your_assistant.config import load_settings
from your_assistant.db import connect, migrate
from your_assistant.mail import MailRepository
from your_assistant.models import (
    Category,
    Classification,
    Importance,
    MailMessage,
    Notify,
)
from your_assistant.web import create_app

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def message(
    *,
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    importance: Importance = Importance.NORMAL,
    unseen: bool = True,
    uid: int = 1,
) -> MailMessage:
    return MailMessage(
        message_id=message_id,
        thread_key=message_id,
        subject=subject,
        sender=sender,
        recipients=("me@shanghaitech.edu.cn",),
        sent_at=datetime.now(UTC) - timedelta(hours=uid),
        body_text=body,
        account="me@shanghaitech.edu.cn",
        uidvalidity=1,
        uid=uid,
        server_unseen=unseen,
        is_new=True,
    )


@pytest.fixture
def web(settings, monkeypatch):
    monkeypatch.setenv("ASSISTANT_PASSWORD", "test-pass")
    settings = load_settings(env_file=Path("/nonexistent/.env"))
    connection = connect(settings.db_path)
    migrate(connection)
    repo = MailRepository(connection)
    repo.insert(
        message(
            message_id="critical@x",
            subject="论文录用通知",
            sender="导师 <boss@lab.edu.cn>",
            body="祝贺，论文被接收了。",
            importance=Importance.CRITICAL,
            uid=1,
        ),
        is_new=True,
        classification=Classification(
            importance=Importance.CRITICAL,
            category=Category.PAPER_ADVISOR,
            notify=Notify.IMMEDIATE,
            tags=("paper",),
            reason="规则「论文」",
        ),
    )
    repo.insert(
        message(
            message_id="normal@x",
            subject="社团活动",
            sender="club@example.com",
            body="周末有活动。",
            importance=Importance.NORMAL,
            uid=2,
        ),
        is_new=True,
        classification=Classification(
            importance=Importance.NORMAL,
            category=Category.OTHER,
            notify=Notify.NONE,
            tags=("bulk",),
            reason="默认",
        ),
    )
    note = settings.vault_dir / "notes" / "course.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "# 课程通知\n\n报名截止时间为 11 月 20 日，请在教务系统提交材料。\n", encoding="utf-8"
    )
    connection.close()
    client = TestClient(create_app(settings))
    return client, settings


def login(client: TestClient) -> None:
    response = client.post("/login", data={"password": "test-pass"}, follow_redirects=False)
    assert response.status_code == 303


def csrf_of(client: TestClient) -> str:
    page = client.get("/")
    match = CSRF_RE.search(page.text)
    assert match, "页面里应该有 CSRF token"
    return match.group(1)


def test_login_is_required_and_wrong_password_is_rejected(web):
    client, _ = web
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"

    wrong = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert wrong.status_code == 401

    login(client)
    assert client.get("/").status_code == 200
    assert "今天收到" in client.get("/").text


def test_key_link_logs_in_without_password(web):
    client, _ = web
    response = client.get("/login?key=test-pass", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_wrong_key_is_rejected(web):
    client, _ = web
    response = client.get("/login?key=not-the-key")
    assert response.status_code == 401
    assert "key 不正确" in response.text


def test_api_accepts_bearer_key(web):
    client, _ = web
    response = client.get("/api/v1/mail", headers={"Authorization": "Bearer test-pass"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_ack_requires_csrf_and_is_local_only(web):
    client, settings = web
    login(client)
    page = client.get("/mail?view=unread")
    assert "论文录用通知" in page.text

    missing = client.post("/mail/1/ack", data={}, follow_redirects=False)
    assert missing.status_code == 400

    acked = client.post("/mail/1/ack", data={"csrf": csrf_of(client)}, follow_redirects=False)
    assert acked.status_code == 303
    assert "论文录用通知" not in client.get("/mail?view=unread").text
    assert client.get("/api/v1/mail?view=all").json()


def test_relabel_writes_a_reviewable_rule(web):
    client, settings = web
    login(client)
    response = client.post(
        "/mail/2/label",
        data={"csrf": csrf_of(client), "category": "paper_advisor", "tags": "paper, deadline"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    rules = settings.mail_rules_file.read_text(encoding="utf-8")
    assert "learned-" in rules
    assert "club@example.com" in rules
    assert "category: paper_advisor" in rules
    detail = client.get("/mail/2")
    assert "论文/导师" in detail.text


def test_search_returns_line_citations(web):
    client, settings = web
    login(client)
    page = client.get("/search", params={"q": "报名截止"})
    assert page.status_code == 200
    assert "course.md" in page.text
    assert "报名" in page.text
    assert re.search(r"course\.md:\d", page.text), "检索结果必须带行号"

    blocked = client.get("/doc", params={"path": "/etc/passwd"})
    assert blocked.status_code == 403


def test_doc_view_shows_original_lines(web):
    client, settings = web
    login(client)
    note = settings.vault_dir / "notes" / "course.md"
    page = client.get("/doc", params={"path": str(note), "start": 3, "end": 3})
    assert page.status_code == 200
    assert "报名截止时间" in page.text


def test_mobile_draft_edit_and_confirm_with_revision(web):
    client, settings = web
    login(client)
    csrf = csrf_of(client)

    created = client.post("/mail/1/briefing", data={"csrf": csrf}, follow_redirects=False)
    assert created.status_code == 303
    location = created.headers["location"]
    assert location.startswith("/drafts/")

    page = client.get(location)
    assert page.status_code == 200
    assert "确认并归档" in page.text
    old_revision = re.search(r'name="revision" value="([0-9a-f]+)"', page.text).group(1)

    saved = client.post(
        f"{location}/save",
        data={"csrf": csrf, "body": "我的决定：参加，周五前报名。\n"},
        follow_redirects=False,
    )
    assert saved.status_code == 303

    stale = client.post(
        f"{location}/confirm",
        data={"csrf": csrf, "revision": old_revision},
        follow_redirects=False,
    )
    assert stale.status_code == 409
    assert "内容已更新" in stale.text
    assert not list((settings.vault_dir / "outbox").glob("*.md"))

    page = client.get(location)
    new_revision = re.search(r'name="revision" value="([0-9a-f]+)"', page.text).group(1)
    assert new_revision != old_revision
    confirmed = client.post(
        f"{location}/confirm",
        data={"csrf": csrf, "revision": new_revision},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert "confirmed=1" in confirmed.headers["location"]

    files = list((settings.vault_dir / "outbox").glob("*.md"))
    assert len(files) == 1
    assert "我的决定：参加" in files[0].read_text(encoding="utf-8")

    done = client.get(location)
    assert "已确认" in done.text
    assert "我知道了，停止提醒" not in client.get("/").text


def test_confirm_requires_csrf(web):
    client, _ = web
    login(client)
    created = client.post(
        "/mail/1/briefing", data={"csrf": csrf_of(client)}, follow_redirects=False
    )
    location = created.headers["location"]
    response = client.post(
        f"{location}/confirm", data={"revision": "deadbeef"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_manifest_for_mobile(web):
    client, _ = web
    payload = client.get("/manifest.webmanifest").json()
    assert payload["start_url"] == "/"
    assert payload["display"] == "standalone"
