"""命令行入口：先做体检，其余命令随里程碑补齐。"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Settings, load_settings
from .db import connect, migrate
from .imap import ImapMailSource
from .knowledge import KnowledgeIndex, display_path
from .mail import MailRepository, RuleClassifier, reclassify_messages
from .models import CATEGORY_LABELS, Category, Importance

PENDING = {
    "sync": "M2（只读邮件同步）",
    "demo": "M4（端到端样例）",
}


def _mark(ok: bool, label: str, detail: str = "", *, warn_only: bool = False) -> bool:
    mark = "✓" if ok else ("!" if warn_only else "✗")
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    return ok


def _probe_tcp(host: str, port: int, timeout: float = 4.0) -> tuple[bool, str]:
    """逐个地址族尝试：校园网经常只有 IPv4 可达，IPv6 会一直超时。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return False, f"DNS 失败：{exc}"

    errors: list[str] = []
    seen: set[str] = set()
    for family, socktype, proto, _, address in infos:
        ip = str(address[0])
        if ip in seen:
            continue
        seen.add(ip)
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                sock.connect(address)
                try:
                    banner = sock.recv(120).decode("utf-8", "replace").strip()
                except OSError:
                    banner = ""
                return True, f"{ip} — {banner[:60] or '已连接'}"
        except OSError as exc:
            errors.append(f"{ip}: {type(exc).__name__}")
    return False, "；".join(errors) or "无可用地址"


def _probe_llm(settings: Settings) -> tuple[bool, str]:
    import httpx

    url = settings.llm.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm.model,
        "messages": [{"role": "user", "content": "只回复 ok"}],
        "max_tokens": 64,
    }
    headers = {"Authorization": f"Bearer {settings.llm.api_key}"}
    proxy = settings.llm.proxy or None
    try:
        with httpx.Client(proxy=proxy, trust_env=bool(proxy), timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            text = (data["choices"][0]["message"].get("content") or "").strip()
            return True, f"HTTP 200，模型回复：{text[:40]!r}" if text else "HTTP 200（无文本输出）"
    except Exception as exc:  # noqa: BLE001 - 体检需要报告任意失败
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(path)
    except OSError as exc:
        return False, f"{path}: {exc}"


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings()
    print(f"your-assistant {__version__} 体检")
    for key, value in settings.public_summary().items():
        print(f"  · {key}: {value}")
    print()

    failures = 0
    print("本地环境")
    print(f"  · python {sys.version.split()[0]}")
    for label, path in (
        ("数据目录", settings.data_dir),
        ("资料目录", settings.vault_dir),
        ("规则目录", settings.rules_file.parent),
        ("记忆目录", settings.memory_dir),
    ):
        ok, detail = _writable(path)
        failures += not _mark(ok, label, detail)

    try:
        conn = connect(settings.db_path)
        version = migrate(conn)
        conn.close()
        _mark(True, "数据库迁移", f"{settings.db_path} @ v{version}")
    except Exception as exc:  # noqa: BLE001
        failures += not _mark(False, "数据库迁移", f"{type(exc).__name__}: {exc}")

    for label, path in (("经验文件", settings.rules_file), ("邮件规则", settings.mail_rules_file)):
        exists = path.exists()
        detail = str(path) if exists else f"缺失：{path}（M1 会创建）"
        _mark(exists, label, detail, warn_only=True)

    sessions = (
        sorted(settings.agent_log_root.glob("**/*.jsonl"))
        if settings.agent_log_root.exists()
        else []
    )
    _mark(bool(sessions), "历史 agent 会话", f"{len(sessions)} 个 JSONL")

    print("\n外部连接")
    mail_ok = settings.mail_configured
    _mark(
        mail_ok,
        "邮箱配置",
        settings.email_address
        or "缺少 ASSISTANT_EMAIL_ADDRESS / ASSISTANT_EMAIL_PASSWORD（M2 需要）",
        warn_only=True,
    )
    if mail_ok:
        print(f"     · 密码长度 {len(settings.email_password)}（不显示内容）")
    _mark(settings.llm.available, "LLM 配置", settings.llm.describe())

    if args.net:
        if settings.imap_host:
            ok, detail = _probe_tcp(settings.imap_host, settings.imap_port)
            failures += not _mark(ok, "IMAP TCP", detail)
        if settings.llm.available:
            ok, detail = _probe_llm(settings)
            failures += not _mark(ok, "LLM 调用", detail)
    else:
        print("  （加 --net 才会实际连接 IMAP / LLM）")

    print(f"\n结论：{'通过' if failures == 0 else f'{failures} 项失败'}")
    return 0 if failures == 0 else 1


def _open_index(args: argparse.Namespace):
    settings = load_settings()
    settings.ensure_dirs()
    roots = tuple(Path(item).expanduser() for item in args.root) or settings.index_roots
    conn = connect(settings.db_path)
    migrate(conn)
    return conn, KnowledgeIndex(conn, roots)


def cmd_index(args: argparse.Namespace) -> int:
    conn, index = _open_index(args)
    try:
        written, deleted = index.rebuild() if args.rebuild else index.sync()
        docs, blocks = index.stats()
        action = "重建" if args.rebuild else "增量更新"
        print(f"索引{action}完成：写入 {written} 块，删除 {deleted} 个文档")
        print(f"当前：{docs} 个文档 / {blocks} 个块")
        for root in index.roots:
            print(f"  · {display_path(str(root))}")
        if args.query:
            hits = index.search(args.query, limit=args.limit)
            print(f"\n检索 {args.query!r}：{len(hits)} 条")
            for hit in hits:
                lines = (
                    f"{hit.start_line}"
                    if hit.end_line <= hit.start_line
                    else f"{hit.start_line}-{hit.end_line}"
                )
                print(f"  [{display_path(hit.path)}:{lines}] {hit.snippet}")
    finally:
        conn.close()
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    migrate(conn)
    try:
        repo = MailRepository(conn)
        source = ImapMailSource(settings, repo, RuleClassifier(settings.mail_rules_file))
        result = source.sync()
        print(result.summary())
        for error in result.errors:
            print(f"  ! {error}")
        counts = repo.counts()
        print(
            f"缓存 {counts['total']} 封；未读 {counts['unread']} 封；"
            f"critical {counts['critical']} 封"
        )
        messages = repo.list_unread(limit=args.limit) or repo.list_recent(limit=args.limit)
        if not messages:
            print("（收件箱缓存为空）")
        for message in messages:
            flag = "●" if message.unread else " "
            print(
                f" {flag} [{message.importance.value}] {message.sent_at:%m-%d %H:%M} "
                f"{message.sender[:32]} | {message.subject[:60]}"
            )
            if message.importance is Importance.CRITICAL:
                print(f"     ! 需要尽快处理：{message.classification_reason}")
        return 0 if result.ok else 1
    finally:
        conn.close()


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            ips.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def _is_loopback(host: str) -> bool:
    return (host or "").strip() in {"127.0.0.1", "localhost", "::1"}


def _guard_host(host: str, *, allow_lan: bool, cli_allow: bool) -> str:
    """默认只允许本机监听；要在局域网暴露必须显式授权。"""
    if _is_loopback(host) or allow_lan or cli_allow:
        return host
    raise PermissionError(
        "拒绝在局域网暴露端口。请使用默认的 127.0.0.1，"
        "或在本次启动加 --allow-lan（并在 .env 设置 ASSISTANT_ALLOW_LAN=true 作为长期授权）。"
    )


def _print_access(settings, host: str, port: int) -> None:
    key = settings.access_key if settings.show_login_link else ""
    if _is_loopback(host):
        base = f"http://127.0.0.1:{port}"
        if key:
            print(f"  本机登录：{base}/login?key={key}", flush=True)
        print(f"  登录页：  {base}/login", flush=True)
        print("  （仅本机可访问；手机访问需显式 --allow-lan）", flush=True)
        return
    ips = _lan_ips() or ["127.0.0.1"]
    for ip in ips:
        base = f"http://{ip}:{port}"
        if key:
            print(f"  手机/局域网：{base}/login?key={key}", flush=True)
        print(f"  登录页：    {base}/login", flush=True)
    print(f"  本机：      http://127.0.0.1:{port}", flush=True)
    if key:
        print(
            "  （key 来自 ASSISTANT_ACCESS_KEY，未设置则等于 ASSISTANT_PASSWORD；"
            "改 .env 后重启生效）",
            flush=True,
        )
    else:
        print("  （未打印带 key 的链接：ASSISTANT_SHOW_LOGIN_LINK=false）", flush=True)


def cmd_link(args: argparse.Namespace) -> int:
    settings = load_settings()
    port = args.port or settings.port
    try:
        host = _guard_host(
            args.host or settings.host, allow_lan=settings.allow_lan, cli_allow=args.allow_lan
        )
    except PermissionError as exc:
        print(f"！{exc}")
        return 2
    print("登录链接（服务需已启动）：")
    _print_access(settings, host, port)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    settings.ensure_dirs()
    if not settings.password:
        print("！未设置 ASSISTANT_PASSWORD，Web 无法登录（先写进 .env）。")
    port = args.port or settings.port
    try:
        host = _guard_host(
            args.host or settings.host, allow_lan=settings.allow_lan, cli_allow=args.allow_lan
        )
    except PermissionError as exc:
        print(f"！{exc}")
        return 2
    app = create_app(settings, start_jobs=True)
    print("Web 已启动，保持本终端开着：", flush=True)
    _print_access(settings, host, port)
    uvicorn.run(app, host=host, port=port)
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    migrate(conn)
    try:
        total = reclassify_messages(conn, RuleClassifier(settings.mail_rules_file))
        rows = conn.execute(
            "SELECT category, notify, COUNT(*) AS n FROM mail_messages GROUP BY category, notify"
        ).fetchall()
        print(f"已按当前规则重算 {total} 封邮件")
        for row in rows:
            label = CATEGORY_LABELS.get(Category(row["category"]), row["category"])
            print(f"  {label:10} notify={row['notify']:9} {row['n']} 封")
        return 0
    finally:
        conn.close()


def cmd_remind(args: argparse.Namespace) -> int:
    from .reminders import counts, panel, tick

    settings = load_settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    migrate(conn)
    try:
        if args.tick:
            fired = tick(conn)
            print(f"本次触发 {len(fired)} 条提醒")
            for item in fired:
                print(
                    f"  [{CATEGORY_LABELS[item.category]}] 第 {item.seq} 次 · {item.subject[:50]}"
                )
        items = panel(conn, limit=args.limit)
        print()
        print(f"待确认提醒：{len(items)} 条，待发 {counts(conn)['pending']} 条")
        for item in items:
            print(
                f"  ● [{CATEGORY_LABELS[item.category]}] {item.subject[:52]} "
                f"（{item.sender[:28]} · 第 {item.seq} 次 · {item.scheduled_at:%m-%d %H:%M}）"
            )
        return 0
    finally:
        conn.close()


def cmd_digest(args: argparse.Namespace) -> int:
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from .review import run_digest, write_digest

    settings = load_settings()
    settings.ensure_dirs()
    day = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(ZoneInfo(settings.timezone)).date()
    )
    digest = run_digest(settings, day, use_llm=not args.no_llm)
    write_digest(settings, digest)
    print(
        f"复盘 {day.isoformat()}：{len(digest.sessions)} 个会话，"
        f"{'LLM 归纳' if digest.used_llm else '确定性摘要'}"
    )
    for warning in digest.warnings:
        print(f"  ! {warning}")
    if digest.review_path:
        print(f"  → {display_path(str(digest.review_path))}")
    if digest.cases_path:
        print(f"  → {display_path(str(digest.cases_path))}")
    if args.show:
        print()
        print(digest.markdown)
    return 0


def _pending(name: str) -> int:
    print(f"`your-assistant {name}` 属于 {PENDING[name]}，尚未实现。")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="your-assistant", description="会成长的个人助手")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="检查配置、目录、数据库与外部连通性")
    doctor.add_argument("--net", action="store_true", help="实际连接 IMAP 和 LLM")
    doctor.set_defaults(func=cmd_doctor)

    index = sub.add_parser("index", help="更新资料索引并检索自测")
    index.add_argument("--query", "-q", default="", help="索引后顺带检索一个词")
    index.add_argument("--limit", type=int, default=8)
    index.add_argument("--rebuild", action="store_true", help="清空重建索引")
    index.add_argument(
        "--root",
        action="append",
        default=[],
        help="额外/替代索引目录，可重复（默认取 ASSISTANT_INDEX_DIRS）",
    )
    index.set_defaults(func=cmd_index)

    sync = sub.add_parser("sync", help="拉取未读邮件（只读，不写服务器）")
    sync.add_argument("--limit", type=int, default=20, help="列表显示条数")
    sync.set_defaults(func=cmd_sync)

    link = sub.add_parser("link", help="打印登录链接（含 key）")
    link.add_argument("--port", type=int, default=0)
    link.add_argument("--host", default="")
    link.add_argument("--allow-lan", action="store_true", help="允许打印局域网地址")
    link.set_defaults(func=cmd_link)

    serve = sub.add_parser("serve", help="启动 Web 界面（默认只监听本机）")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument(
        "--allow-lan",
        action="store_true",
        help="显式允许在局域网监听（默认只允许 127.0.0.1）",
    )
    serve.set_defaults(func=cmd_serve)

    reclassify = sub.add_parser("reclassify", help="用当前规则重算所有已缓存邮件的类别")
    reclassify.set_defaults(func=cmd_reclassify)

    remind = sub.add_parser("remind", help="查看提醒，或用 --tick 触发到期提醒")
    remind.add_argument("--tick", action="store_true", help="把到期的提醒标记为已发送")
    remind.add_argument("--limit", type=int, default=20)
    remind.set_defaults(func=cmd_remind)

    digest = sub.add_parser("digest", help="生成每日复盘（pi + codex 会话）")
    digest.add_argument("--date", default="", help="YYYY-MM-DD，默认今天")
    digest.add_argument("--no-llm", action="store_true", help="只用确定性摘要")
    digest.add_argument("--show", action="store_true", help="同时打印 Markdown")
    digest.set_defaults(func=cmd_digest)

    demo = sub.add_parser("demo", help="用样例数据跑完整流程")
    demo.set_defaults(func=lambda _args, n="demo": _pending(n))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
