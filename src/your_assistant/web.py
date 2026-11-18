"""手机可用的 Web 界面：收件箱、邮件详情、资料检索、状态页。

- 单用户密码登录 + 签名 cookie；所有写操作校验 CSRF。
- 只读邮件：界面不提供发送/删除按钮，能力开关也不允许。
- 端点全部 async，内部调用同步的 sqlite/文件接口（个人规模足够）。
"""

from __future__ import annotations

import calendar
import random
import re
import secrets
import threading
from collections import Counter, defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup, escape

from .blog import list_posts, read_post
from .config import Settings
from .db import connect, log_event, migrate
from .flows import build_briefing, confirm_draft, get_draft, latest_briefing, save_draft
from .imap import inline_cid_images, mark_seen
from .knowledge import KnowledgeIndex, display_path
from .mail import IMPORTANCE_BY_CATEGORY, MailRepository, RuleClassifier
from .models import (
    CATEGORY_LABELS,
    NOTIFY_BY_CATEGORY,
    Category,
    Classification,
    Importance,
    Notify,
)
from .reminders import acknowledge
from .reminders import panel as reminder_panel
from .reminders import plan as reminder_plan
from .tasks import (
    DEFAULT_MINUTES,
    day_path,
    elapsed_seconds,
    overtime_seconds,
    read_tasks,
    remaining_seconds,
    replace_tasks,
    write_tasks,
)
from .tasks import (
    reset as task_reset,
)
from .tasks import (
    start as task_start,
)
from .tasks import (
    start_overtime as task_overtime,
)
from .tasks import (
    stop as task_stop,
)
from .tasks import (
    toggle as task_toggle,
)
from .weather import fetch_weather

FESTIVALS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}
GREETINGS = (
    "今天也会是顺利的一天。",
    "别急着开始，先喝口水。",
    "今天做成一件事就很好。",
    "祝你少踩坑，多摸鱼。",
    "慢慢来，比较快。",
    "今天也有人在偷偷关心你。",
    "记得吃饭，别一直坐着。",
)

COOKIE = "gse_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_DIR / "templates"
STATIC = PACKAGE_DIR / "static"
IMPORTANCE_LABELS = {
    Importance.CRITICAL: "非常重要",
    Importance.HIGH: "重要",
    Importance.NORMAL: "普通",
    Importance.LOW: "低",
    Importance.BULK: "批量/广告",
}


def _asset_version() -> str:
    """静态资源版本号：改 CSS 后换 URL，避开浏览器启发式缓存。"""
    try:
        return str(int((STATIC / "app.css").stat().st_mtime))
    except OSError:
        return "1"


def _category_or_none(raw: str) -> Category | None:
    try:
        return Category(raw)
    except ValueError:
        return None


def _fmt_dt(value: datetime | None, pattern: str = "%m-%d %H:%M") -> str:
    if not value:
        return ""
    return value.astimezone().strftime(pattern)


def task_icons(count: int = 2) -> list[str]:
    """每次开始时从本地 emoji 库里随机挑几个。"""
    names = sorted(path.name for path in (STATIC / "emoji").glob("*.png"))
    if not names:
        return []
    return [
        f'<img src="/static/emoji/{name}" alt="" loading="lazy">'
        for name in random.sample(names, min(count, len(names)))
    ]


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _review_summary(text: str, limit: int = 88) -> str:
    """取复盘里「今日高亮」的第一条，没有就退回第一段正文。"""
    section = ""
    for line in text.splitlines():
        if line.startswith("**一句话**："):
            return _clip(line[len("**一句话**：") :].strip(), limit)
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section == "今日高亮" and line.startswith("- "):
            return _clip(line[2:].strip(), limit)
    for line in text.splitlines():
        if line.startswith("- "):
            return _clip(line[2:].strip(), limit)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ">", "-", "*", "（")):
            return _clip(stripped, limit)
    return ""


def _inline_markdown(text: str) -> str:
    escaped = str(escape(text))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img src="\2" alt="\1">', escaped)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', escaped)


def _render_markdown(text: str) -> Markup:
    """极简 Markdown 渲染：只覆盖复盘文件里的标题、列表、引用与强调。"""
    parts: list[str] = []
    in_list = False
    in_ol = False
    in_code = False
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal in_list, in_ol
        if in_list:
            parts.append("</ul>")
            in_list = False
        if in_ol:
            parts.append("</ol>")
            in_ol = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                parts.append("<pre><code>" + str(escape("\n".join(code_lines))) + "</code></pre>")
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            close_list()
            continue
        if line.startswith("### "):
            close_list()
            parts.append(f"<h3>{_inline_markdown(line[4:])}</h3>")
        elif line.startswith("## "):
            close_list()
            parts.append(f"<h2>{_inline_markdown(line[3:])}</h2>")
        elif line.startswith("# "):
            close_list()
            parts.append(f"<h1>{_inline_markdown(line[2:])}</h1>")
        elif line.startswith("> "):
            close_list()
            parts.append(f"<blockquote>{_inline_markdown(line[2:])}</blockquote>")
        elif line.startswith(("- ", "* ")):
            if in_ol:
                parts.append("</ol>")
                in_ol = False
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{_inline_markdown(line[2:])}</li>")
        elif re.match(r"^\d+\. ", line):
            if in_list:
                parts.append("</ul>")
                in_list = False
            if not in_ol:
                parts.append("<ol>")
                in_ol = True
            parts.append(f"<li>{_inline_markdown(line.split('. ', 1)[1])}</li>")
        else:
            close_list()
            parts.append(f"<p>{_inline_markdown(line)}</p>")
    close_list()
    return Markup("".join(parts))


def _day_label(day: date) -> str:
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{weekdays[day.weekday()]}, {months[day.month - 1]} {day.day}"


def _today_label(settings: Settings) -> tuple[str, str]:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    return today.isoformat(), _day_label(today)


def create_app(settings: Settings, *, start_jobs: bool = False) -> FastAPI:
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["dt"] = _fmt_dt
    templates.env.filters["dpath"] = display_path
    templates.env.filters["dur"] = _fmt_duration
    templates.env.globals["asset_version"] = _asset_version()
    templates.env.globals["importance_labels"] = IMPORTANCE_LABELS
    templates.env.globals["category_labels"] = CATEGORY_LABELS
    templates.env.globals["notify_labels"] = {
        Notify.IMMEDIATE: "立即提醒",
        Notify.NORMAL: "提醒一次",
        Notify.NONE: "不提醒",
    }
    signer = URLSafeTimedSerializer(settings.session_secret, salt="your-assistant-web-session")
    failures: dict[str, deque[datetime]] = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        if start_jobs:
            from .worker import build_scheduler

            scheduler = build_scheduler(settings)
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="个人助手", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    app.state.settings = settings

    # ---- 基础设施 ----
    def session_of(request: Request) -> dict[str, Any] | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            data = signer.loads(raw, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return None
        return data if isinstance(data, dict) else None

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        session = session_of(request) or {}
        base = {
            "request": request,
            "csrf": session.get("csrf", ""),
            "authenticated": bool(session),
            "settings_summary": settings.public_summary(),
        }
        base.update(extra)
        return base

    def require_session(request: Request) -> dict[str, Any]:
        session = session_of(request)
        if session:
            return session
        # 只读 API 也接受 Bearer key，方便脚本/将来的 Android 客户端
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            candidate = header.split(" ", 1)[1].strip()
            if settings.access_key and secrets.compare_digest(candidate, settings.access_key):
                return {"user": "me", "csrf": "", "api": True}
        raise HTTPException(status_code=307, headers={"Location": "/login"})

    async def require_csrf(request: Request) -> Any:
        session = require_session(request)
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), session.get("csrf", "")):
            raise HTTPException(status_code=400, detail="CSRF 校验失败，请刷新页面重试")
        return form

    def open_conn():
        connection = connect(settings.db_path)
        migrate(connection)
        return connection

    def mail_repo(connection) -> MailRepository:
        return MailRepository(connection)

    def index_of(connection) -> KnowledgeIndex:
        return KnowledgeIndex(connection, settings.index_roots)

    def classifier() -> RuleClassifier:
        return RuleClassifier(settings.mail_rules_file)

    # ---- 登录 ----
    def locked(ip: str) -> bool:
        attempts = failures[ip]
        while attempts and datetime.now(UTC) - attempts[0] > timedelta(minutes=10):
            attempts.popleft()
        return len(attempts) >= 6

    def login_response() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            signer.dumps({"user": "me", "csrf": secrets.token_urlsafe(18)}),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookie,
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Any:
        if session_of(request):
            return RedirectResponse("/", status_code=303)
        key = request.query_params.get("key", "").strip()
        if key:
            ip = request.client.host if request.client else "unknown"
            if locked(ip):
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    context(
                        request,
                        error="尝试次数过多，请 10 分钟后再试",
                        password_set=True,
                        key_link=True,
                    ),
                    status_code=429,
                )
            if settings.access_key and secrets.compare_digest(key, settings.access_key):
                failures.pop(ip, None)
                return login_response()
            failures[ip].append(datetime.now(UTC))
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="key 不正确", password_set=True, key_link=True),
                status_code=401,
            )
        return templates.TemplateResponse(
            request,
            "login.html",
            context(
                request,
                error="",
                password_set=bool(settings.password),
                key_link=bool(settings.access_key),
            ),
        )

    @app.post("/login")
    async def login(request: Request) -> Any:
        form = await request.form()
        ip = request.client.host if request.client else "unknown"
        if locked(ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="尝试次数过多，请 10 分钟后再试", password_set=True),
                status_code=429,
            )
        if not settings.password:
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="服务端未设置 ASSISTANT_PASSWORD", password_set=False),
                status_code=500,
            )
        if not secrets.compare_digest(str(form.get("password", "")), settings.password):
            failures[ip].append(datetime.now(UTC))
            return templates.TemplateResponse(
                request,
                "login.html",
                context(request, error="密码不正确", password_set=True),
                status_code=401,
            )
        failures.pop(ip, None)
        return login_response()

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        await require_csrf(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ---- 首页 ----
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Any:
        require_session(request)
        today_iso, today_label = _today_label(settings)
        blog_posts = list_posts(settings.vault_dir / "blog", limit=1)
        latest_post = blog_posts[0] if blog_posts else None
        review_path = settings.vault_dir / "reviews" / f"{today_iso}.md"
        review_text = ""
        if review_path.is_file():
            review_text = review_path.read_text(encoding="utf-8")
        review_summary = _review_summary(review_text) if review_text else ""
        tasks, task_text = _task_state()
        running_task = next((view for view in tasks if view["running"]), None)
        focus_html = _focus_html(running_task)
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            counts = repo.counts()
            state = repo.sync_state(settings.email_address)
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            recent = repo.list_recent(limit=300)
            today_mails = [
                message
                for message in recent
                if message.sent_at and message.sent_at.astimezone(tz).date() == now.date()
            ]
            unread = repo.list_unread(limit=50)
            important_mails = [
                message
                for message in unread
                if message.importance in (Importance.CRITICAL, Importance.HIGH)
            ][:3]
            category_counts = Counter(message.category for message in today_mails)
            mail_categories = [
                {"value": category.value, "label": CATEGORY_LABELS[category], "count": count}
                for category, count in category_counts.most_common()
            ]
            reminder_plan(connection)
            reminders = [
                item
                for item in reminder_panel(connection)
                if item.importance in ("critical", "high")
            ]
        finally:
            connection.close()
        return templates.TemplateResponse(
            request,
            "index.html",
            context(
                request,
                counts=counts,
                sync_state=state,
                reminders=reminders,
                mail_configured=settings.mail_configured,
                today_label=today_label,
                review_summary=review_summary,
                today_mail_count=len(today_mails),
                mail_categories=mail_categories,
                important_mails=important_mails,
                latest_post=latest_post,
                tasks=tasks,
                task_text=task_text,
                focus_html=focus_html,
            ),
        )

    # ---- 今日任务 ----
    def _task_file() -> tuple[str, Path]:
        today_iso, _ = _today_label(settings)
        return today_iso, day_path(settings.vault_dir, today_iso)

    def _task_state() -> tuple[list[dict[str, Any]], str]:
        today_iso, path = _task_file()
        data = read_tasks(path, today_iso)
        now = datetime.now(ZoneInfo(settings.timezone))
        views = [
            {
                "index": index,
                "title": task.title,
                "done": task.done,
                "minutes": task.minutes,
                "spent": task.spent,
                "total": task.total_seconds,
                "running": task.started_at is not None,
                "counting_up": task.counting_up,
                "can_overtime": task.started_at is None and task.spent >= task.total_seconds,
                "remaining": remaining_seconds(task, now),
                "overtime": overtime_seconds(task, now),
                "elapsed": elapsed_seconds(task, now),
                "started_at": task.started_at.isoformat() if task.started_at else "",
            }
            for index, task in enumerate(data.tasks)
        ]
        text = "\n".join(task.title for task in data.tasks)
        return views, text

    def _focus_html(view: dict[str, Any] | None) -> str:
        if not view:
            return ""
        seconds = view["overtime"] if view["counting_up"] else view["remaining"]
        return templates.get_template("_focus_card.html").render(
            icons=task_icons(),
            title=view["title"],
            time=_fmt_duration(seconds),
        )

    def _wants_json(request: Request) -> bool:
        return request.headers.get("x-requested-with") == "fetch"

    def _tasks_json(request: Request, text: str = "") -> JSONResponse:
        views, current_text = _task_state()
        running = next((view for view in views if view["running"]), None)
        csrf = (session_of(request) or {}).get("csrf", "")
        html = templates.get_template("_tasks.html").render(tasks=views, csrf=csrf)
        editor = templates.get_template("_tasks_editor.html").render(tasks=views, csrf=csrf)
        return JSONResponse(
            {
                "ok": True,
                "html": html,
                "editor": editor,
                "count": len(views),
                "text": text or current_text,
                "focus": _focus_html(running),
            }
        )

    def _mutate_task(index: int, action, *, exclusive: bool = False) -> None:
        today_iso, path = _task_file()
        data = read_tasks(path, today_iso)
        if exclusive:
            now = datetime.now(ZoneInfo(settings.timezone))
            for position, task in enumerate(data.tasks):
                if position != index and task.started_at:
                    task_stop(task, now)
        if 0 <= index < len(data.tasks):
            action(data.tasks[index])
            write_tasks(path, data)

    @app.post("/tasks/save")
    async def tasks_save(request: Request) -> Any:
        form = await require_csrf(request)
        today_iso, path = _task_file()
        data = read_tasks(path, today_iso)
        titles = [str(value) for value in form.getlist("title")]
        minutes_raw = [str(value) for value in form.getlist("minutes")]
        items: list[tuple[str, int]] = []
        for index, title in enumerate(titles):
            raw = minutes_raw[index] if index < len(minutes_raw) else ""
            items.append((title, int(raw) if raw.isdigit() else DEFAULT_MINUTES))
        replace_tasks(data, items)
        write_tasks(path, data)
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{index}/start")
    async def tasks_start(request: Request, index: int) -> Any:
        await require_csrf(request)
        now = datetime.now(ZoneInfo(settings.timezone))
        _mutate_task(index, lambda task: task_start(task, now), exclusive=True)
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{index}/overtime")
    async def tasks_overtime(request: Request, index: int) -> Any:
        await require_csrf(request)
        now = datetime.now(ZoneInfo(settings.timezone))
        _mutate_task(index, lambda task: task_overtime(task, now), exclusive=True)
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{index}/stop")
    async def tasks_stop(request: Request, index: int) -> Any:
        await require_csrf(request)
        now = datetime.now(ZoneInfo(settings.timezone))
        _mutate_task(index, lambda task: task_stop(task, now))
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{index}/reset")
    async def tasks_reset(request: Request, index: int) -> Any:
        await require_csrf(request)
        _mutate_task(index, task_reset)
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{index}/toggle")
    async def tasks_toggle(request: Request, index: int) -> Any:
        await require_csrf(request)
        _mutate_task(index, task_toggle)
        if _wants_json(request):
            return _tasks_json(request)
        return RedirectResponse("/", status_code=303)

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_history(request: Request) -> Any:
        require_session(request)
        rows = []
        for path in sorted((settings.vault_dir / "tasks").glob("*.md"), reverse=True):
            data = read_tasks(path, path.stem)
            rows.append(
                {
                    "day": path.stem,
                    "done": sum(1 for task in data.tasks if task.done),
                    "total": len(data.tasks),
                    "spent": sum(task.spent for task in data.tasks),
                    "text": path.read_text(encoding="utf-8"),
                }
            )
        return templates.TemplateResponse(
            request, "tasks_history.html", context(request, rows=rows)
        )

    # ---- 今日总结 ----
    review_state: dict[str, Any] = {"running": False, "step": "", "error": ""}
    review_lock = threading.Lock()

    def _set_review_step(step: str) -> None:
        with review_lock:
            review_state["step"] = step

    def _review_worker() -> None:
        try:
            from .review import run_digest as build_digest
            from .review import write_digest

            today = datetime.now(ZoneInfo(settings.timezone)).date()
            digest = build_digest(settings, today, progress=_set_review_step)
            write_digest(settings, digest)
            with review_lock:
                review_state.update(running=False, step="完成", error="")
        except Exception as exc:  # noqa: BLE001 - 后台失败通过状态接口暴露
            with review_lock:
                review_state.update(running=False, step="", error=f"{type(exc).__name__}: {exc}")

    @app.post("/review/run")
    async def review_run(request: Request) -> JSONResponse:
        await require_csrf(request)
        with review_lock:
            if review_state["running"]:
                return JSONResponse({"ok": True, "running": True})
            review_state.update(running=True, step="准备", error="")
        threading.Thread(target=_review_worker, daemon=True).start()
        return JSONResponse({"ok": True, "running": True})

    @app.get("/api/review/status")
    async def review_status(request: Request) -> JSONResponse:
        require_session(request)
        with review_lock:
            payload = dict(review_state)
        today_iso, _ = _today_label(settings)
        path = settings.vault_dir / "reviews" / f"{today_iso}.md"
        summary = ""
        if path.is_file():
            summary = _review_summary(path.read_text(encoding="utf-8"))
        return JSONResponse({"ok": True, **payload, "summary": summary})

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request) -> Any:
        require_session(request)
        today_iso, _ = _today_label(settings)
        raw = request.query_params.get("date", "").strip() or today_iso
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            day = date.fromisoformat(today_iso)
        review_path = settings.vault_dir / "reviews" / f"{day.isoformat()}.md"
        review_html = None
        if review_path.is_file():
            review_html = _render_markdown(review_path.read_text(encoding="utf-8"))
        payload = context(
            request,
            today_label=_day_label(day),
            review_html=review_html,
            review_day=day.isoformat(),
        )
        if request.query_params.get("partial"):
            return HTMLResponse(templates.get_template("_review_body.html").render(**payload))
        return templates.TemplateResponse(request, "review.html", payload)

    @app.get("/reviews", response_class=HTMLResponse)
    async def reviews_calendar(request: Request) -> Any:
        require_session(request)
        today_iso, _ = _today_label(settings)
        raw = request.query_params.get("month", "").strip() or today_iso[:7]
        try:
            year, month = (int(part) for part in raw.split("-")[:2])
            first = date(year, month, 1)
        except (ValueError, TypeError):
            first = date.fromisoformat(today_iso).replace(day=1)
        existing = {
            path.stem
            for path in (settings.vault_dir / "reviews").glob("*.md")
            if path.stem[:4].isdigit()
        }
        weeks = []
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
            weeks.append(
                [
                    {
                        "day": item.day,
                        "iso": item.isoformat(),
                        "in_month": item.month == first.month,
                        "has": item.isoformat() in existing,
                    }
                    for item in week
                ]
            )
        prev_month = (first - timedelta(days=1)).strftime("%Y-%m")
        next_month = (first + timedelta(days=32)).strftime("%Y-%m")
        payload = context(
            request,
            month_label=first.strftime("%Y-%m"),
            weeks=weeks,
            prev_month=prev_month,
            next_month=next_month,
        )
        if request.query_params.get("partial"):
            return HTMLResponse(templates.get_template("_reviews_calendar.html").render(**payload))
        return templates.TemplateResponse(request, "reviews.html", payload)

    # ---- 博客 ----
    @app.get("/blog", response_class=HTMLResponse)
    async def blog_index(request: Request) -> Any:
        require_session(request)
        posts = list_posts(settings.vault_dir / "blog")
        return templates.TemplateResponse(request, "blog_list.html", context(request, posts=posts))

    @app.get("/blog/{slug}", response_class=HTMLResponse)
    async def blog_post(request: Request, slug: str) -> Any:
        require_session(request)
        post = read_post(settings.vault_dir / "blog", slug)
        if not post:
            raise HTTPException(status_code=404, detail="文章不存在")
        body = _render_markdown(post.body)
        return templates.TemplateResponse(
            request, "blog_post.html", context(request, post=post, body=body)
        )

    # ---- 收件箱 ----
    @app.get("/mail", response_class=HTMLResponse)
    async def mail_list(request: Request) -> Any:
        require_session(request)
        unread_only = request.query_params.get("view", "unread") != "all"
        category = request.query_params.get("category", "")
        query = request.query_params.get("q", "").strip()
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            messages = repo.list_filtered(
                unread_only=unread_only,
                category=_category_or_none(category),
                query=query,
                limit=100,
            )
            counts = repo.counts()
        finally:
            connection.close()
        return templates.TemplateResponse(
            request,
            "mail_list.html",
            context(
                request,
                messages=messages,
                counts=counts,
                unread_only=unread_only,
                category=category,
                query=query,
            ),
        )

    @app.get("/mail/{message_id}", response_class=HTMLResponse)
    async def mail_detail(request: Request, message_id: int) -> Any:
        require_session(request)
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            message = repo.get(message_id)
            if not message:
                raise HTTPException(status_code=404, detail="邮件不存在")
            if message.unread:
                acknowledge(connection, message_id)
                if settings.mark_seen_remote and message.uid:
                    threading.Thread(
                        target=mark_seen, args=(settings, message.uid), daemon=True
                    ).start()
            thread = repo.thread(message.thread_key, limit=40)
            briefing = latest_briefing(connection, message_id)
        finally:
            connection.close()
        body_html = (
            inline_cid_images(message.body_html, message.raw_path) if message.body_html else ""
        )
        return templates.TemplateResponse(
            request,
            "mail_detail.html",
            context(
                request,
                message=message,
                thread=thread,
                briefing=briefing,
                body_html=body_html,
            ),
        )

    @app.post("/mail/{message_id}/briefing")
    async def mail_briefing(request: Request, message_id: int) -> Any:
        await require_csrf(request)
        connection = open_conn()
        try:
            draft = build_briefing(settings, connection, message_id)
        finally:
            connection.close()
        return RedirectResponse(f"/drafts/{draft.id}", status_code=303)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    async def draft_page(request: Request, draft_id: int) -> Any:
        require_session(request)
        connection = open_conn()
        try:
            draft = get_draft(connection, draft_id)
            if not draft:
                raise HTTPException(status_code=404, detail="确认稿不存在")
            message = None
            if draft.target_ref.startswith("mail:"):
                message = mail_repo(connection).get(int(draft.target_ref.split(":", 1)[1]))
        finally:
            connection.close()
        return templates.TemplateResponse(
            request,
            "draft.html",
            context(
                request,
                draft=draft,
                message=message,
                error="",
                saved=request.query_params.get("saved") == "1",
                confirmed=request.query_params.get("confirmed") == "1",
            ),
        )

    @app.post("/drafts/{draft_id}/save")
    async def draft_save(request: Request, draft_id: int) -> Any:
        form = await require_csrf(request)
        connection = open_conn()
        try:
            save_draft(connection, draft_id, str(form.get("body", "")))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        finally:
            connection.close()
        return RedirectResponse(f"/drafts/{draft_id}?saved=1", status_code=303)

    @app.post("/drafts/{draft_id}/confirm")
    async def draft_confirm(request: Request, draft_id: int) -> Any:
        form = await require_csrf(request)
        revision = str(form.get("revision", ""))
        connection = open_conn()
        try:
            result = confirm_draft(settings, connection, draft_id, revision)
            draft = result.draft or get_draft(connection, draft_id)
            message = None
            if draft and draft.target_ref.startswith("mail:"):
                message = mail_repo(connection).get(int(draft.target_ref.split(":", 1)[1]))
        finally:
            connection.close()
        if result.ok:
            return RedirectResponse(f"/drafts/{draft_id}?confirmed=1", status_code=303)
        if result.reason == "stale":
            return templates.TemplateResponse(
                request,
                "draft.html",
                context(
                    request,
                    draft=draft,
                    message=message,
                    error="内容已更新，请复核当前内容后再次确认",
                    saved=False,
                    confirmed=False,
                ),
                status_code=409,
            )
        if result.reason == "already":
            return RedirectResponse(f"/drafts/{draft_id}?confirmed=1", status_code=303)
        raise HTTPException(status_code=404, detail="确认稿不存在")

    @app.post("/mail/{message_id}/ack")
    async def mail_ack(request: Request, message_id: int) -> Any:
        await require_csrf(request)
        connection = open_conn()
        try:
            acknowledge(connection, message_id)
            log_event(connection, "mail.ack", {"id": message_id})
        finally:
            connection.close()
        target = request.headers.get("referer", "/mail")
        if not target.startswith("/") or target.startswith("//"):
            target = "/mail"
        return RedirectResponse(target, status_code=303)

    @app.post("/reminders/{mail_id}/ack")
    async def reminder_ack(request: Request, mail_id: int) -> Any:
        await require_csrf(request)
        connection = open_conn()
        try:
            acknowledge(connection, mail_id)
            log_event(connection, "reminder.ack", {"mail_id": mail_id})
        finally:
            connection.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/mail/{message_id}/label")
    async def mail_label(request: Request, message_id: int) -> Any:
        form = await require_csrf(request)
        category = _category_or_none(str(form.get("category", "")))
        if category is None:
            raise HTTPException(status_code=400, detail="无效的类别")
        tags_raw = str(form.get("tags", "")).replace("，", ",")
        tags = tuple(tag.strip() for tag in tags_raw.split(",") if tag.strip())
        result = Classification(
            importance=IMPORTANCE_BY_CATEGORY[category],
            category=category,
            notify=NOTIFY_BY_CATEGORY.get(category, Notify.NONE),
            tags=tags,
            reason="人工改判",
        )
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            message = repo.get(message_id)
            if not message:
                raise HTTPException(status_code=404, detail="邮件不存在")
            repo.set_classification(message_id, result)
            rule = classifier().learn(
                sender=message.sender,
                subject=message.subject,
                category=category,
                tags=tags,
                evidence=message.message_id,
            )
            log_event(
                connection,
                "mail.relabel",
                {"id": message_id, "category": category.value, "rule": rule},
            )
        finally:
            connection.close()
        return RedirectResponse(f"/mail/{message_id}", status_code=303)

    # ---- 检索 ----
    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request) -> Any:
        require_session(request)
        query = request.query_params.get("q", "").strip()
        hits: list[Any] = []
        mails: list[Any] = []
        written = deleted = 0
        if query:
            connection = open_conn()
            try:
                index = index_of(connection)
                written, deleted = index.sync()
                hits = index.search(query, limit=12)
                mails = mail_repo(connection).search_mail(query, limit=10)
            finally:
                connection.close()
        return templates.TemplateResponse(
            request,
            "search.html",
            context(
                request,
                query=query,
                hits=hits,
                mails=mails,
                written=written,
                deleted=deleted,
            ),
        )

    @app.get("/doc", response_class=HTMLResponse)
    async def doc(request: Request) -> Any:
        require_session(request)
        raw_path = request.query_params.get("path", "")
        path = Path(raw_path).expanduser().resolve()
        roots = [root.resolve() for root in settings.index_roots]
        if not any(path == root or root in path.parents for root in roots):
            raise HTTPException(status_code=403, detail="只能查看索引目录内的文件")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        start = max(1, int(request.query_params.get("start", "1") or 1))
        end = max(start, int(request.query_params.get("end", str(start + 40)) or start + 40))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        left = max(1, start - 30)
        right = min(len(lines), end + 30)
        excerpt = [(number, lines[number - 1]) for number in range(left, right + 1)]
        return templates.TemplateResponse(
            request,
            "doc.html",
            context(
                request,
                path=str(path),
                shown=display_path(str(path)),
                start=start,
                end=end,
                excerpt=excerpt,
            ),
        )

    # ---- 状态与 API ----
    @app.get("/status", response_class=HTMLResponse)
    async def status(request: Request) -> Any:
        require_session(request)
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            state = repo.sync_state(settings.email_address)
            counts = repo.counts()
            docs, blocks = index_of(connection).stats()
            recent_events = connection.execute(
                "SELECT kind, payload, at FROM events ORDER BY id DESC LIMIT 12"
            ).fetchall()
        finally:
            connection.close()
        return templates.TemplateResponse(
            request,
            "status.html",
            context(
                request,
                sync_state=state,
                counts=counts,
                docs=docs,
                blocks=blocks,
                events=recent_events,
            ),
        )

    @app.get("/api/mail/summary")
    async def mail_summary(request: Request) -> JSONResponse:
        require_session(request)
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            counts = repo.counts()
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            recent = repo.list_recent(limit=300)
            today_mails = [
                message
                for message in recent
                if message.sent_at and message.sent_at.astimezone(tz).date() == now.date()
            ]
            category_counts = Counter(message.category for message in today_mails)
            categories = [
                {"value": category.value, "label": CATEGORY_LABELS[category], "count": count}
                for category, count in category_counts.most_common()
            ]
            unread = repo.list_unread(limit=50)
            important = [
                message
                for message in unread
                if message.importance in (Importance.CRITICAL, Importance.HIGH)
            ][:3]
            reminder_plan(connection)
            reminders = [
                item
                for item in reminder_panel(connection)
                if item.importance in ("critical", "high")
            ]
        finally:
            connection.close()
        html = templates.get_template("_mail_card.html").render(
            today_mail_count=len(today_mails),
            mail_categories=categories,
            important_mails=important,
            reminders=reminders,
            counts=counts,
        )
        return JSONResponse({"ok": True, "html": html})

    @app.get("/api/greeting")
    async def greeting_api(request: Request) -> JSONResponse:
        require_session(request)
        now = datetime.now(ZoneInfo(settings.timezone))
        today = now.date()
        names = [path.name for path in (STATIC / "emoji").glob("*.png")]
        icons = random.sample(names, min(10, len(names))) if names else []
        return JSONResponse(
            {
                "ok": True,
                "hello": "早上好" if now.hour < 12 else "你好",
                "date": _day_label(today),
                "festival": FESTIVALS.get((today.month, today.day), ""),
                "message": random.choice(GREETINGS),
                "icons": icons,
            }
        )

    @app.get("/api/weather")
    async def weather_api(request: Request) -> JSONResponse:
        require_session(request)
        data = fetch_weather()
        if not data:
            return JSONResponse({"ok": False})
        return JSONResponse({"ok": True, **data})

    @app.get("/api/v1/mail")
    async def api_mail(request: Request) -> Any:
        require_session(request)
        unread_only = request.query_params.get("view", "unread") != "all"
        connection = open_conn()
        try:
            repo = mail_repo(connection)
            messages = repo.list_unread(limit=100) if unread_only else repo.list_recent(limit=100)
        finally:
            connection.close()
        return JSONResponse(
            [
                {
                    "id": message.id,
                    "subject": message.subject,
                    "from": message.sender,
                    "date": message.sent_at.isoformat(),
                    "importance": message.importance.value,
                    "category": message.category.value,
                    "notify": message.notify.value,
                    "unread": message.unread,
                    "thread": message.thread_key,
                }
                for message in messages
            ]
        )

    @app.get("/manifest.webmanifest")
    async def manifest() -> Any:
        return JSONResponse(
            {
                "name": "个人助手",
                "short_name": "助手",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0f1115",
                "theme_color": "#0f1115",
                "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
            },
            media_type="application/manifest+json",
        )

    return app
