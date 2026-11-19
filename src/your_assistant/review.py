"""每日复盘：把 pi/codex 会话整理成"今天干了什么 + 我纠正了什么"。

输出两份人可读文件：
- `vault/reviews/YYYY-MM-DD.md`：当天项目与讨论总结（不入库，私人）
- `memory/cases/YYYY-MM-DD.md`：纠错案例与可复用规则建议（入库，可 diff）

LLM 只负责润色归纳；没有模型时用确定性摘要，功能不消失。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .agentlog import (
    Session,
    clip_to_window,
    collect_local,
    corrections,
    fetch_remote_sessions,
)
from .config import Settings
from .model import build_llm

log = logging.getLogger("your-assistant.review")
MAX_OUTLINE_CHARS = 60_000

PROMPT = """你在为"会成长的个人助手"写每日复盘。下面是用户今天与编码 agent 的会话摘要，
以及用户长期积累的经验。请按项目归纳：今天做了什么、讨论了什么、产出/决定是什么；
并找出用户纠正 agent 的地方，提炼成可复用规则。

只输出 JSON，不要 markdown。结构：
{{
  "summary": "一句话总结今天最重要的进展（40 字内，不要罗列）",
  "projects": [{{"name": "...", "did": ["..."], "decisions": ["..."], "artifacts": ["..."]}}],
  "corrections": [{{"problem": "...", "correction": "...", "rule": "..."}}],
  "highlights": ["..."],
  "followups": ["..."]
}}

要求：
- 每个列表最多 5 条，每条不超过 80 字。
- 不编造材料里没有的事实；材料不足就留空。
- "corrections" 必须来自用户明确表达不满/纠正的轮次。

用户长期经验（rules/lessons.md）：
{lessons}

今天的会话摘要：
{outline}
"""


@dataclass
class Digest:
    day: date
    markdown: str
    sessions: list[Session] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_llm: bool = False
    cases_path: Path | None = None
    review_path: Path | None = None


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _stamp(value: datetime | None, pattern: str = "%H:%M") -> str:
    return value.strftime(pattern) if value else "--:--"


def _window_note(session: Session) -> str:
    """跨天会话的标注：历史只报数量，不带内容。"""
    if not session.history_turns:
        return ""
    return (
        f"（会话始于 {_stamp(session.origin_at, '%m-%d %H:%M')}，"
        f"此前 {session.history_turns} 轮历史未纳入）"
    )


def _outline(sessions: list[Session]) -> str:
    blocks: list[str] = []
    for session in sessions:
        header = (
            f"### [{session.source}{'@' + session.host if session.host else ''}] "
            f"{session.project} ({session.cwd}) "
            f"今日 {_stamp(session.started_at)}–{_stamp(session.ended_at)}"
            f"{_window_note(session)}"
        )
        lines = [header]
        for turn in session.turns:
            if not turn.text:
                continue
            prefix = "用户" if turn.role == "user" else "助手"
            lines.append(f"- {prefix}：{_clip(turn.text, 500)}")
        if session.tools:
            lines.append(
                "- 工具："
                + ", ".join(f"{name}×{count}" for name, count in session.tools.most_common(8))
            )
        blocks.append("\n".join(lines))
        if sum(len(block) for block in blocks) > MAX_OUTLINE_CHARS:
            blocks.append("（已截断，后续会话从略）")
            break
    return "\n\n".join(blocks)


def _deterministic(sessions: list[Session]) -> dict:
    projects: dict[str, dict] = {}
    for session in sessions:
        project = projects.setdefault(
            session.project, {"name": session.project, "did": [], "decisions": [], "artifacts": []}
        )
        for turn in session.user_turns[:5]:
            item = _clip(turn.text, 120)
            if item not in project["did"]:
                project["did"].append(item)
        if session.assistant_turns:
            last = _clip(session.assistant_turns[-1].text, 240)
            if last and last not in project["decisions"]:
                project["decisions"].append(last)
        if session.tools:
            tools = ", ".join(f"{name}×{count}" for name, count in session.tools.most_common(5))
            project["artifacts"].append(
                f"{session.source} 会话（{_stamp(session.started_at)}）：{tools}"
            )
    summary = "；".join(
        f"{item['name']}：{_clip(item['did'][0], 30)}" for item in projects.values() if item["did"]
    )
    return {
        "summary": summary,
        "projects": list(projects.values()),
        "corrections": [],
        "highlights": [],
        "followups": [],
    }


def _normalize(payload: dict, fallback: dict) -> dict:
    def clean(value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()][:6]

    projects = []
    for item in payload.get("projects") or []:
        if not isinstance(item, dict):
            continue
        projects.append(
            {
                "name": str(item.get("name") or "未命名项目"),
                "did": clean(item.get("did")),
                "decisions": clean(item.get("decisions")),
                "artifacts": clean(item.get("artifacts")),
            }
        )
    learned = []
    for item in payload.get("corrections") or []:
        if not isinstance(item, dict):
            continue
        learned.append(
            {
                "problem": str(item.get("problem") or ""),
                "correction": str(item.get("correction") or ""),
                "rule": str(item.get("rule") or ""),
            }
        )
    return {
        "summary": str(payload.get("summary") or "").strip(),
        "projects": projects or fallback["projects"],
        "corrections": learned,
        "highlights": clean(payload.get("highlights")),
        "followups": clean(payload.get("followups")),
    }


def _topics_by_project(sessions: list[Session]) -> dict[str, list[str]]:
    topics: dict[str, list[str]] = {}
    for session in sessions:
        topics.setdefault(session.project, []).extend(
            _clip(turn.text, 60) for turn in session.user_turns[:3]
        )
    return topics


def render(
    day: date,
    data: dict,
    sessions: list[Session],
    detected: list[dict[str, str]],
    *,
    used_llm: bool,
    warnings: list[str],
) -> str:
    sources = (
        ", ".join(
            f"{key} {sum(1 for s in sessions if s.source == key)} 个"
            for key in sorted({session.source for session in sessions})
        )
        or "无"
    )
    lines = [
        f"# {day.isoformat()} 每日复盘",
        "",
        f"> 生成于 {datetime.now():%Y-%m-%d %H:%M} · 会话来源：{sources} · "
        f"归纳方式：{'LLM' if used_llm else '本地确定性摘要'}",
        "",
    ]
    if data.get("summary"):
        lines += [f"**一句话**：{data['summary']}", ""]
    if not sessions:
        lines += ["今天没有读到任何 agent 会话。", ""]

    lines += ["## 项目", ""]
    for index, project in enumerate(data.get("projects") or [], start=1):
        lines.append(f"### {index}. {project['name']}")
        for label, key in (("做了什么", "did"), ("决定/讨论", "decisions"), ("产出", "artifacts")):
            items = project.get(key) or []
            if not items:
                continue
            lines.append(f"**{label}**")
            lines.extend(f"- {item}" for item in items)
        lines.append("")

    lines += ["## 我纠正了什么", ""]
    if data.get("corrections"):
        for item in data["corrections"]:
            lines.append(f"- **问题**：{_clip(item['problem'], 160)}")
            if item.get("correction"):
                lines.append(f"  **纠正**：{_clip(item['correction'], 200)}")
            if item.get("rule"):
                lines.append(f"  **可写成规则**：{_clip(item['rule'], 160)}")
    elif detected:
        for item in detected[:5]:
            lines.append(f"- [{item['project']}] 用户说：{_clip(item['user_said'], 160)}")
            lines.append(f"  （上一轮 agent：{_clip(item['agent_said'], 120)}）")
    else:
        lines.append("- 今天没有检测到明确的纠正。")
    lines.append("")

    if data.get("highlights"):
        lines += ["## 今日高亮", ""] + [f"- {item}" for item in data["highlights"]] + [""]
    if data.get("followups"):
        lines += ["## 待跟进", ""] + [f"- {item}" for item in data["followups"]] + [""]

    lines += ["## 数据与限制", ""]
    for session in sessions:
        line = (
            f"- `{session.source}{'@' + session.host if session.host else ''}` "
            f"{session.project}（{session.cwd or '未知目录'}）"
            f"今日 {_stamp(session.started_at)}–{_stamp(session.ended_at)}"
        )
        if session.history_turns:
            line += (
                f"，会话始于 {_stamp(session.origin_at, '%m-%d %H:%M')}，"
                f"忽略 {session.history_turns} 轮历史"
            )
        lines.append(line)
    for warning in warnings:
        lines.append(f"- ⚠ {warning}")
    lines.append("")
    return "\n".join(lines)


def render_cases(day: date, detected: list[dict[str, str]], data: dict) -> str:
    lines = [f"# {day.isoformat()} 纠错案例", ""]
    lines.append("> 由每日复盘自动整理；规则建议需要你确认后才写进 `rules/lessons.md`。")
    lines.append("")
    suggestions = [
        item.get("rule", "") for item in data.get("corrections") or [] if item.get("rule")
    ]
    for index, item in enumerate(detected, start=1):
        lines += [
            f"## 案例 {index} · {item['project']}",
            "",
            f"- **agent 原来的说法**：{_clip(item['agent_said'], 300) or '（无）'}",
            f"- **我的纠正**：{_clip(item['user_said'], 300)}",
        ]
        rule = suggestions[index - 1] if index - 1 < len(suggestions) else ""
        lines.append(f"- **可复用规则**：{_clip(rule, 200) if rule else '待补充'}")
        lines.append("")
    if data.get("corrections"):
        lines += ["## 可以加进 lessons.md 的候选", ""]
        for item in data["corrections"]:
            if item.get("rule"):
                lines.append(f"- {_clip(item['rule'], 160)}")
        lines.append("")
    return "\n".join(lines)


def run_digest(settings: Settings, day: date, *, use_llm: bool = True, progress=None) -> Digest:
    notify = progress or (lambda _step: None)
    notify("读取会话")
    tz = ZoneInfo(settings.timezone)
    sessions = collect_local(settings.agent_log_root, settings.codex_log_root, day, tz)
    warnings: list[str] = []

    for host in settings.remote_agent_hosts:
        cache = settings.data_dir / "remote" / host
        remote_sessions, error = fetch_remote_sessions(host, cache)
        if error:
            warnings.append(f"远程 {error}")
            continue
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        for session in remote_sessions:
            clipped = clip_to_window(session, start, end)
            if clipped:
                sessions.append(clipped)

    detected = corrections(sessions)
    fallback = _deterministic(sessions)
    data: dict = fallback
    used_llm = False

    lessons = ""
    if settings.rules_file.exists():
        lessons = settings.rules_file.read_text(encoding="utf-8")[:12_000]

    llm = build_llm(settings.llm) if use_llm else None
    notify("归纳中" if llm else "整理摘要")
    if llm and sessions:
        payload = llm.complete_json(
            "daily_review",
            PROMPT.format(lessons=lessons or "（空）", outline=_outline(sessions)),
        )
        if payload:
            data = _normalize(payload, fallback)
            used_llm = True
            # LLM 漏掉的纠正用启发式结果补上，避免"学会了但没记录"
            if not data["corrections"] and detected:
                data["corrections"] = [
                    {
                        "problem": _clip(item["agent_said"], 160),
                        "correction": _clip(item["user_said"], 200),
                        "rule": "",
                    }
                    for item in detected[:5]
                ]
        else:
            warnings.append("LLM 调用失败，已退回确定性摘要")

    notify("写入文件")
    markdown = render(day, data, sessions, detected, used_llm=used_llm, warnings=warnings)
    return Digest(
        day=day,
        markdown=markdown,
        sessions=sessions,
        warnings=warnings,
        used_llm=used_llm,
        cases_path=None,
    )


def write_digest(settings: Settings, digest: Digest) -> Digest:
    reviews = settings.vault_dir / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    review_path = reviews / f"{digest.day.isoformat()}.md"
    review_path.write_text(digest.markdown, encoding="utf-8")
    digest.review_path = review_path

    cases = settings.memory_dir / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    detected = corrections(digest.sessions)
    if detected:
        cases_path = cases / f"{digest.day.isoformat()}-cases.md"
        data = _deterministic(digest.sessions)
        cases_path.write_text(render_cases(digest.day, detected, data), encoding="utf-8")
        digest.cases_path = cases_path
    return digest


def dump_digest_json(digest: Digest) -> str:
    return json.dumps(
        {
            "day": digest.day.isoformat(),
            "used_llm": digest.used_llm,
            "sessions": len(digest.sessions),
            "warnings": digest.warnings,
        },
        ensure_ascii=False,
    )
