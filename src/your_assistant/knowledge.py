"""个人资料库：Markdown 索引、增量更新、带行号的检索。

设计要点：
- 索引只做"块"级别（段落），每块记录 `path:start_line-end_line`，检索结果直接可回溯原文。
- 中文用 jieba 预分词后再交给 FTS5；英文/数字原样保留。
- 文件哈希没变就不重写块，删除的文件连带删除块（外键 CASCADE）。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import jieba

from .db import now_iso
from .models import Hit

TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
SKIP_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
MIN_BLOCK_CHARS = 120
MAX_BLOCK_CHARS = 1800
_WORDLIKE = re.compile(r"[\w\u4e00-\u9fff]")
_jieba_lock = threading.Lock()
_jieba_ready = False


def _ensure_jieba() -> None:
    global _jieba_ready
    if _jieba_ready:
        return
    with _jieba_lock:
        if not _jieba_ready:
            jieba.initialize()
            _jieba_ready = True


def tokenize(text: str) -> str:
    """把文本切成以空格分隔的词序列；FTS5 的 unicode61 会按空格建 token。"""
    _ensure_jieba()
    words = (word.strip().lower() for word in jieba.lcut(text))
    return " ".join(word for word in words if word and _WORDLIKE.search(word))


def query_terms(query: str) -> list[str]:
    return tokenize(query).split()


def _snippet(text: str, terms: Sequence[str], width: int = 70) -> str:
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return text[: width * 2].replace("\n", " ") + ("…" if len(text) > width * 2 else "")
    start = max(0, min(positions) - width)
    end = min(len(text), min(positions) + width * 2)
    body = text[start:end].replace("\n", " ")
    for term in sorted({term for term in terms if term}, key=len, reverse=True):
        if term:
            body = re.sub(re.escape(term), lambda m: f"«{m.group(0)}»", body, flags=re.I)
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


@dataclass(frozen=True)
class Block:
    start_line: int
    end_line: int
    text: str
    heading: str = ""


def split_blocks(text: str) -> list[Block]:
    """按空行切段；遇到超长段落主动截断，单行超长文本则保持整块。"""
    lines = text.splitlines()
    blocks: list[Block] = []
    buffer: list[str] = []
    start = 0
    heading = ""

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            blocks.append(
                Block(
                    start_line=start + 1,
                    end_line=start + len(buffer),
                    text=body,
                    heading=heading,
                )
            )
        buffer = []

    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if not line.strip():
            if len("\n".join(buffer)) >= MIN_BLOCK_CHARS:
                flush()
            continue
        if not buffer:
            start = index
        stripped = line.lstrip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
        buffer.append(line)
        if len("\n".join(buffer)) >= MAX_BLOCK_CHARS:
            flush()
    flush()
    return blocks


def iter_text_files(roots: Sequence[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in TEXT_SUFFIXES:
                yield root
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def display_path(path: str) -> str:
    """把绝对路径缩成 ~/...，引用更短且不泄露机器目录结构。"""
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


class KnowledgeIndex:
    def __init__(self, conn: sqlite3.Connection, roots: Sequence[Path]) -> None:
        self.conn = conn
        self.roots = tuple(roots)

    # ---- 索引 ----
    def sync(self) -> tuple[int, int]:
        """返回 (写入/更新的块数, 删除的文档数)。"""
        known = {
            row["path"]: (row["id"], row["sha"])
            for row in self.conn.execute("SELECT id, path, sha FROM documents")
        }
        seen: set[str] = set()
        written = 0

        for file_path in iter_text_files(self.roots):
            absolute = str(file_path.resolve())
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if absolute in seen:  # 多个根目录重叠时只索引一次
                continue
            seen.add(absolute)
            digest = _sha(text)
            previous = known.get(absolute)
            if previous and previous[1] == digest:
                continue

            if previous:
                doc_id = previous[0]
                self.conn.execute(
                    "DELETE FROM blocks_fts WHERE rowid IN "
                    "(SELECT id FROM blocks WHERE doc_id = ?)",
                    (doc_id,),
                )
                self.conn.execute("DELETE FROM blocks WHERE doc_id = ?", (doc_id,))
                self.conn.execute(
                    "UPDATE documents SET mtime = ?, size = ?, sha = ?, indexed_at = ? "
                    "WHERE id = ?",
                    (
                        file_path.stat().st_mtime,
                        file_path.stat().st_size,
                        digest,
                        now_iso(),
                        doc_id,
                    ),
                )
            else:
                cursor = self.conn.execute(
                    "INSERT INTO documents (path, mtime, size, sha, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        absolute,
                        file_path.stat().st_mtime,
                        file_path.stat().st_size,
                        digest,
                        now_iso(),
                    ),
                )
                doc_id = int(cursor.lastrowid)

            for block in split_blocks(text):
                tokens = tokenize(f"{block.heading}\n{block.text}" if block.heading else block.text)
                cursor = self.conn.execute(
                    "INSERT INTO blocks (doc_id, start_line, end_line, text, tokens, heading) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, block.start_line, block.end_line, block.text, tokens, block.heading),
                )
                self.conn.execute(
                    "INSERT INTO blocks_fts (rowid, tokens) VALUES (?, ?)",
                    (cursor.lastrowid, tokens),
                )
                written += 1

        deleted = 0
        for path, (doc_id, _) in known.items():
            if path in seen:
                continue
            self.conn.execute(
                "DELETE FROM blocks_fts WHERE rowid IN (SELECT id FROM blocks WHERE doc_id = ?)",
                (doc_id,),
            )
            self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            deleted += 1

        self.conn.commit()
        return written, deleted

    def rebuild(self) -> tuple[int, int]:
        self.conn.execute("DELETE FROM blocks_fts")
        self.conn.execute("DELETE FROM blocks")
        self.conn.execute("DELETE FROM documents")
        self.conn.commit()
        return self.sync()

    # ---- 检索 ----
    def search(self, query: str, limit: int = 8, *, match_all: bool = True) -> list[Hit]:
        terms = query_terms(query)
        if not terms:
            return []
        joiner = " AND " if match_all else " OR "
        expression = joiner.join(f'"{term}"' for term in terms)
        rows = self.conn.execute(
            """
            SELECT d.path, b.start_line, b.end_line, b.text, bm25(blocks_fts) AS rank
            FROM blocks_fts
            JOIN blocks b ON b.id = blocks_fts.rowid
            JOIN documents d ON d.id = b.doc_id
            WHERE blocks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()
        if not rows:
            rows = self._like_search(query, limit)
        return [
            Hit(
                path=row["path"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                score=round(1.0 / (1.0 + abs(float(row["rank"]))), 4),
                snippet=_snippet(row["text"], terms),
                text=row["text"],
            )
            for row in rows
        ]

    def _like_search(self, query: str, limit: int) -> list[sqlite3.Row]:
        pattern = f"%{query.strip()}%"
        if len(pattern) <= 2:
            return []
        return self.conn.execute(
            """
            SELECT d.path, b.start_line, b.end_line, b.text, 0.0 AS rank
            FROM blocks b JOIN documents d ON d.id = b.doc_id
            WHERE b.text LIKE ?
            ORDER BY b.start_line
            LIMIT ?
            """,
            (pattern, limit),
        ).fetchall()

    def stats(self) -> tuple[int, int]:
        docs = int(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        blocks = int(self.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0])
        return docs, blocks
