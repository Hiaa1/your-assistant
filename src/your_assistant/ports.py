"""所有对外接缝。用例层只依赖这里的 Protocol，适配器可替换。

设计约束（见 PLAN.md §4）：
- 全部同步调用；异步只发生在 HTTP / 调度边界。
- `MailSource` 刻意不提供删除、移动、标记已读的方法：只读需求是结构性保证，
  而不是靠调用方自觉。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import DeliveryResult, Draft, Hit, MailMessage, SyncResult, Task


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """Markdown 资料库：索引随文件变化更新，检索结果带行号。"""

    def sync(self) -> tuple[int, int]:
        """增量更新索引，返回 (新增/更新块数, 删除文档数)。"""

    def search(self, query: str, limit: int = 8) -> list[Hit]: ...


@runtime_checkable
class MailSource(Protocol):
    """只读邮件来源。没有 delete / move / store_flags / send 方法。"""

    def sync(self) -> SyncResult: ...

    def fetch_thread(self, thread_key: str) -> Sequence[MailMessage]: ...


@runtime_checkable
class MailSender(Protocol):
    """发送适配器。MVP 不装配（capability=send_mail 关闭时 UI 不渲染入口）。"""

    def send(self, draft: Draft) -> DeliveryResult: ...


@runtime_checkable
class Classifier(Protocol):
    """邮件重要性 / 标签判定。规则实现与 LLM 实现可互换。"""

    def classify(
        self, message: MailMessage, thread: Sequence[MailMessage]
    ) -> tuple[Any, tuple[str, ...]]: ...


@runtime_checkable
class Retriever(Protocol):
    """检索实现：现在关键字 + jieba，将来可换向量或混合。"""

    def search(self, query: str, limit: int = 8) -> list[Hit]: ...


@runtime_checkable
class Llm(Protocol):
    """可降级的 LLM。返回 None 表示"没有可用模型"，调用方必须走确定性分支。"""

    @property
    def available(self) -> bool: ...

    def complete_json(self, task: str, prompt: str) -> dict[str, Any] | None: ...

    def complete_text(self, task: str, prompt: str) -> str | None: ...


@runtime_checkable
class Notifier(Protocol):
    """提醒通道：站内、ntfy、Telegram……调用方不关心。"""

    def notify(self, title: str, body: str, *, level: str = "normal", ref: str = "") -> bool: ...


@runtime_checkable
class AgentLogSource(Protocol):
    """历史 agent 会话（默认读 pi 的 JSONL），用于每日复盘。"""

    def sessions_between(self, start: datetime, end: datetime) -> Iterable[Any]: ...


@runtime_checkable
class TaskRepository(Protocol):
    def add(self, task: Task) -> Task: ...

    def update(self, task: Task) -> Task: ...

    def get(self, task_id: int) -> Task | None: ...

    def list_open(self, limit: int = 50) -> Sequence[Task]: ...


@runtime_checkable
class LessonStore(Protocol):
    """人可读、可手改、进 Git 的经验条目。"""

    def all(self) -> Sequence[Any]: ...

    def append(self, lesson: Any) -> None: ...

    def match(self, text: str, limit: int = 5) -> Sequence[Any]: ...
