from __future__ import annotations

import imaplib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

from your_assistant.config import load_settings
from your_assistant.db import migrate
from your_assistant.imap import ImapMailSource
from your_assistant.mail import MailRepository, RuleClassifier
from your_assistant.models import Category, Importance, Notify

WRITE_COMMANDS = {"store", "copy", "move", "expunge", "append", "delete"}


def raw_mail(
    subject: str = "课程通知",
    sender: str = "career@shanghaitech.edu.cn",
    body: str = "正文内容：报名截止时间为 11 月 20 日。",
    message_id: str = "<a@example.com>",
    references: str = "",
    date: str = "Tue, 17 Nov 2026 10:00:00 +0800",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "me@shanghaitech.edu.cn"
    message["Date"] = date
    message["Message-ID"] = message_id
    if references:
        message["References"] = references
    message.set_content(body)
    return message.as_bytes()


class FakeImap:
    """只实现适配器用到的只读子集，并记录所有调用。"""

    def __init__(self) -> None:
        self.uidvalidity = 7
        self.box: dict[int, tuple[bytes, bool]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.readonly: bool | None = None
        self.untagged_responses: dict[str, list[bytes]] = {}
        self.sizes: dict[int, int] = {}
        self.fail_once: set[int] = set()

    def add(self, uid: int, raw: bytes, *, unseen: bool = False) -> None:
        self.box[uid] = (raw, unseen)

    def login(self, user: str, password: str):
        self.calls.append(("login", user))
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False):
        self.calls.append(("select", folder, readonly))
        self.readonly = readonly
        self.untagged_responses = {"UIDVALIDITY": [str(self.uidvalidity).encode()]}
        return "OK", [str(len(self.box)).encode()]

    def uid(self, command: str, *args):
        self.calls.append(("uid", command, *args))
        name = command.lower()
        if name == "search":
            return self._search(str(args[1]))
        if name == "fetch":
            return self._fetch(int(args[0]), str(args[1]))
        raise AssertionError(f"适配器不应调用 UID {command}")

    def _search(self, criterion: str):
        criterion = criterion.strip()
        uids = sorted(self.box)
        if criterion.upper() == "ALL":
            selected = uids
        elif criterion.upper() == "UNSEEN":
            selected = [uid for uid in uids if self.box[uid][1]]
        elif criterion.upper().startswith("SINCE"):
            selected = uids
        elif criterion.upper().startswith("UID "):
            low = int(criterion.split()[1].split(":")[0])
            selected = [uid for uid in uids if uid >= low]
        elif criterion.endswith(":*"):
            selected = [uid for uid in uids if uid >= int(criterion.split(":")[0])]
        else:
            raise AssertionError(f"未支持的检索条件：{criterion}")
        return "OK", [" ".join(str(uid) for uid in selected).encode()]

    def _fetch(self, uid: int, what: str):
        if uid in self.fail_once:
            self.fail_once.discard(uid)
            raise imaplib.IMAP4.error("模拟网络抖动")
        raw, unseen = self.box[uid]
        if "FLAGS" in what:
            flags = "" if unseen else "\\Seen"
            return "OK", [f"1 (FLAGS ({flags}))".encode()]
        if "RFC822.SIZE" in what:
            return "OK", [f"1 (RFC822.SIZE {self.sizes.get(uid, len(raw))})".encode()]
        header, _, body = raw.partition(b"\n\n")
        if "BODY.PEEK[HEADER]" in what:
            return "OK", [(b"1 (BODY[HEADER] {", header), b")"]
        if "BODY.PEEK[TEXT]" in what:
            partial = body[: 256 * 1024]
            return "OK", [(b"1 (BODY[TEXT] {", partial), b")"]
        return "OK", [(f"1 (BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]

    @property
    def write_calls(self) -> list[tuple[object, ...]]:
        return [
            call
            for call in self.calls
            if call[0] == "uid" and str(call[1]).lower() in WRITE_COMMANDS
        ]


@pytest.fixture
def mail_settings(monkeypatch):
    monkeypatch.setenv("ASSISTANT_EMAIL_ADDRESS", "me@shanghaitech.edu.cn")
    monkeypatch.setenv("ASSISTANT_EMAIL_PASSWORD", "client-password")
    monkeypatch.setenv("ASSISTANT_INITIAL_MAIL_LIMIT", "5")
    return load_settings(env_file=Path("/nonexistent/.env"))


@pytest.fixture
def mail_env(mail_settings, conn):
    migrate(conn)
    rules_file = mail_settings.mail_rules_file
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    rules_file.write_text(
        yaml.safe_dump(
            {
                "base": [
                    {
                        "name": "教务",
                        "when": {
                            "from_contains": ["student.sist@shanghaitech.edu.cn"],
                            "subject_regex": "成绩|选课",
                        },
                        "importance": "high",
                        "tags": ["school"],
                    }
                ],
                "learned": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return mail_settings, MailRepository(conn), RuleClassifier(rules_file), conn


def test_sync_is_read_only_and_deduplicates(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>", subject="成绩公示"))
    fake.add(11, raw_mail(message_id="<m2@x>", subject="讲座通知"), unseen=True)
    fake.add(12, raw_mail(message_id="<m3@x>", subject="社团招新"))

    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    first = source.sync()
    assert first.ok
    assert first.new == 3
    assert fake.readonly is True, "必须只读 SELECT"
    assert fake.write_calls == [], "不允许任何写操作"

    second = source.sync()
    assert second.ok
    assert second.new == 0
    assert len(repo.list_recent()) == 3

    counts = repo.counts()
    assert counts["unread"] == 1, "首次回填不算新邮件，只有服务器未读算未读"
    assert repo.list_unread()[0].subject == "讲座通知"


def test_new_mail_after_backfill_is_unread_and_threaded(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<root@x>", subject="论文投稿确认"))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    source.sync()

    fake.add(
        20,
        raw_mail(
            message_id="<reply@x>",
            subject="Re: 论文投稿确认",
            references="<root@x>",
            sender="advisor@shanghaitech.edu.cn",
        ),
    )
    result = source.sync()
    assert result.new == 1
    unread = repo.list_unread()
    assert len(unread) == 1
    assert unread[0].unread
    thread = repo.thread("root@x")
    assert [message.message_id for message in thread] == ["root@x", "reply@x"]


def test_uidvalidity_reset_does_not_duplicate_or_lose_mail(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>", subject="旧邮件一"))
    fake.add(11, raw_mail(message_id="<m2@x>", subject="旧邮件二"))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    assert source.sync().new == 2

    # 服务器重建信箱：UIDVALIDITY 变化、UID 全部重排，同时还有新邮件
    fake.uidvalidity = 9
    fake.box = {
        100: (raw_mail(message_id="<m1@x>", subject="旧邮件一"), False),
        101: (raw_mail(message_id="<m2@x>", subject="旧邮件二"), False),
        102: (raw_mail(message_id="<m3@x>", subject="新邮件三"), True),
    }
    result = source.sync()
    assert result.ok
    assert result.new == 1, "只应插入新邮件"
    assert len(repo.list_recent()) == 3
    assert len({message.message_id for message in repo.list_recent()}) == 3


def test_sync_failure_is_visible_for_status_page(mail_env):
    settings, repo, classifier, _ = mail_env

    def broken():
        raise ConnectionError("网络不可达")

    source = ImapMailSource(settings, repo, classifier, connector=broken)
    result = source.sync()
    assert not result.ok
    assert result.errors
    state = repo.sync_state(settings.email_address)
    assert state is not None
    assert "不可达" in state["last_error"]
    assert repo.counts()["total"] == 0


def test_rules_classify_and_learn_from_correction(mail_env):
    _, repo, classifier, conn = mail_env
    classified = classifier.classify("student.sist@shanghaitech.edu.cn", "关于选课的通知")
    assert classified.importance is Importance.HIGH
    assert "school" in classified.tags

    other = classifier.classify("random@example.com", "随便聊聊")
    assert other.importance is Importance.NORMAL

    name = classifier.learn(
        sender="boss@lab.edu.cn",
        subject="论文录用通知 - ICRA 2027",
        category=Category.PAPER_ADVISOR,
        tags=["paper"],
        evidence="<paper@x>",
    )
    assert name.startswith("learned-")
    again = classifier.classify("boss@lab.edu.cn", "论文录用通知 - ICRA 2027")
    assert again.importance is Importance.CRITICAL
    # 按发件人泛化：同一人换主题也仍然是 critical
    other_topic = classifier.classify("Boss <boss@lab.edu.cn>", "下周三组会")
    assert other_topic.importance is Importance.CRITICAL
    assert "论文" in classifier.rules_file.read_text(encoding="utf-8")


def test_failed_uid_is_retried_from_pending(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>"))
    fake.fail_once = {10}
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)

    first = source.sync()
    assert first.new == 0
    assert first.errors
    assert repo.pending_uids(settings.email_address) == [10]

    second = source.sync()
    assert second.new == 1
    assert repo.pending_uids(settings.email_address) == []


def test_second_sync_does_not_refetch_known_bodies(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>"))
    fake.add(11, raw_mail(message_id="<m2@x>"))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    source.sync()

    fake.add(12, raw_mail(message_id="<m3@x>", subject="新邮件"))
    fake.calls.clear()
    assert source.sync().new == 1

    body_fetches = [
        call
        for call in fake.calls
        if call[0] == "uid"
        and str(call[1]).lower() == "fetch"
        and "BODY.PEEK[HEADER]" not in str(call[2])
    ]
    assert {call[2] for call in body_fetches} == {"12"}, "已知邮件只取头部，不重下正文"


def test_large_mail_is_truncated_instead_of_downloaded(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<big@x>", subject="超大附件公告", body="正文很长" * 50))
    fake.sizes[10] = 30 * 1024 * 1024
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    assert source.sync().ok

    message = repo.list_recent()[0]
    assert message.subject == "超大附件公告"
    assert "正文很长" in message.body_text
    assert any("BODY.PEEK[HEADER]" in str(call) for call in fake.calls)
    assert any("BODY.PEEK[TEXT]<0." in str(call) for call in fake.calls)
    assert not any("BODY.PEEK[]" in str(call) for call in fake.calls), "大邮件不应整封下载"


def test_real_rules_categorize_today_mail():
    rules = Path(__file__).resolve().parents[1] / "rules" / "mail_rules.yaml"
    classifier = RuleClassifier(rules)
    cases = [
        (
            "OpenReview <notifications@openreview.net>",
            "Decision on your submission",
            Category.PAPER_ADVISOR,
            Notify.IMMEDIATE,
        ),
        (
            "career@shanghaitech.edu.cn",
            "【2027届校招宣讲会-44】某公司",
            Category.CAREER_TALK,
            Notify.NONE,
        ),
        (
            "sca-research@shanghaitech.edu.cn",
            "【就在明天】青年学者论坛邀请",
            Category.FORUM,
            Notify.NONE,
        ),
        ("shangdaocollege@shanghaitech.edu.cn", "鹮羽观鸟社快闪", Category.EVENT, Notify.NORMAL),
        ("ogs@shanghaitech.edu.cn", "关于学校体育馆闭馆的通知", Category.ANNOUNCEMENT, Notify.NONE),
        (
            "library@shanghaitech.edu.cn",
            "【培训通知】AI 赋能科研与论文写作",
            Category.ANNOUNCEMENT,
            Notify.NONE,
        ),
        (
            "competition@xsci.com",
            "Your paper award and $500 prize",
            Category.ADVERTISEMENT,
            Notify.NONE,
        ),
    ]
    for sender, subject, category, notify in cases:
        result = classifier.classify(sender, subject)
        assert result.category is category, subject
        assert result.notify is notify, subject


def test_advisor_contacts_are_immediate(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "contacts:\n  advisors: [boss@lab.edu.cn]\nbase: []\nlearned: []\n",
        encoding="utf-8",
    )
    classifier = RuleClassifier(rules)
    result = classifier.classify("Boss <boss@lab.edu.cn>", "随便一句")
    assert result.category is Category.PAPER_ADVISOR
    assert result.notify is Notify.IMMEDIATE
    assert result.importance is Importance.CRITICAL


def test_ack_is_local_only(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>"), unseen=True)
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    source.sync()
    message = repo.list_unread()[0]
    assert message.id is not None
    repo.ack(message.id)
    assert repo.list_unread() == []
    assert fake.write_calls == []


def test_sync_window_uses_last_uid(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>"))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    source.sync()

    fake.add(30, raw_mail(message_id="<m2@x>", subject="新的邮件"))
    source.sync()
    searches = [str(call[3]) for call in fake.calls if call[0] == "uid" and call[1] == "search"]
    assert any("UID 11:*" in item for item in searches), "增量同步应从 last_uid + 1 开始"

    # 旧 UID 的邮件不会被重复拉取
    assert source.sync().new == 0
    assert repo.sync_state(settings.email_address)["last_uid"] == 30


def test_unseen_mail_outside_window_is_still_fetched(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    for index in range(1, 7):
        fake.add(10 + index, raw_mail(message_id=f"<m{index}@x>", subject=f"第{index}封"))
    fake.box[11] = (fake.box[11][0], True)  # 很旧但未读
    source = ImapMailSource(
        replace(settings, initial_mail_limit=2), repo, classifier, connector=lambda: fake
    )
    source.sync()
    messages = {message.message_id for message in repo.list_recent()}
    assert "m1@x" in messages, "窗口外的未读邮件也必须拉取"


def test_repository_marks_only_new_arrivals_after_backfill(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    fake.add(10, raw_mail(message_id="<m1@x>"))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    source.sync()
    assert repo.list_unread() == []
    fake.add(11, raw_mail(message_id="<m2@x>", subject="新到邮件"))
    source.sync()
    assert len(repo.list_unread()) == 1


def test_old_dates_do_not_break_threads(mail_env):
    settings, repo, classifier, _ = mail_env
    fake = FakeImap()
    old = (datetime.now(UTC) - timedelta(days=400)).strftime("%a, %d %b %Y %H:%M:%S %z")
    fake.add(10, raw_mail(message_id="<old@x>", date=old))
    source = ImapMailSource(settings, repo, classifier, connector=lambda: fake)
    assert source.sync().ok
    message = repo.list_recent()[0]
    assert message.sent_at.year < datetime.now(UTC).year
