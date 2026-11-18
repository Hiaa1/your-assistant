"""个人博客：`vault/blog/*.md`，文件名即 slug，第一行 `# 标题`。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXCERPT_CHARS = 90


@dataclass
class Post:
    slug: str
    title: str
    excerpt: str
    body: str
    updated: float


def _title_and_excerpt(text: str, fallback: str) -> tuple[str, str]:
    title = ""
    summary = ""
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("> ") and not summary:
            summary = stripped[2:].strip()
            continue
        if stripped.startswith(("#", ">", "-", "*", "|", "```", "<!--")):
            continue
        lines.append(stripped)
        if len(lines) >= 2:
            break
    excerpt = summary or " ".join(lines)
    if len(excerpt) > EXCERPT_CHARS:
        excerpt = excerpt[: EXCERPT_CHARS - 1].rstrip() + "…"
    return title or fallback, excerpt


def read_post(root: Path, slug: str) -> Post | None:
    if not slug or slug.startswith(".") or "/" in slug or "\\" in slug:
        return None
    path = root / f"{slug}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    title, excerpt = _title_and_excerpt(text, slug)
    return Post(
        slug=slug,
        title=title,
        excerpt=excerpt,
        body=text,
        updated=path.stat().st_mtime,
    )


def list_posts(root: Path, limit: int = 20) -> list[Post]:
    if not root.is_dir():
        return []
    posts: list[Post] = []
    for path in root.glob("*.md"):
        if path.name == "README.md":
            continue
        post = read_post(root, path.stem)
        if post:
            posts.append(post)
    posts.sort(key=lambda item: item.updated, reverse=True)
    return posts[:limit]
