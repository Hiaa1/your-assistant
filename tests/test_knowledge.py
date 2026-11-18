from __future__ import annotations

from pathlib import Path

from your_assistant.knowledge import KnowledgeIndex, display_path, split_blocks, tokenize


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _index(conn, root: Path) -> KnowledgeIndex:
    return KnowledgeIndex(conn, (root,))


def test_index_follows_edits_deletions_and_cites_lines(conn, settings):
    notes = settings.vault_dir / "notes"
    note = _write(
        notes / "course.md",
        "# 课程通知\n\n"
        "报名截止时间为 11 月 20 日，请在教务系统提交材料。\n"
        "这次报名的材料包括成绩单和推荐信，请提前准备，逾期不候。\n\n"
        "## 活动安排\n\n"
        "活动开始时间为 11 月 25 日下午两点，地点在教学中心 302。\n",
    )
    index = _index(conn, notes)

    written, deleted = index.sync()
    assert written > 0
    assert deleted == 0

    hits = index.search("报名 截止")
    assert hits, "中文分词检索应当命中"
    hit = next(item for item in hits if item.path.endswith("course.md"))
    assert hit.path == str(note.resolve())
    assert hit.start_line <= 3 <= hit.end_line
    assert "报名" in hit.snippet
    assert hit.citation.startswith(str(note.resolve()) + ":")

    # 文件修改：旧内容消失，新内容可见
    _write(note, "# 课程通知\n\n活动改期为 12 月 1 日，地点不变。\n")
    written, deleted = index.sync()
    assert written > 0
    assert index.search("报名") == []
    assert index.search("改期")

    # 文件删除：块和文档一起消失
    note.unlink()
    _, deleted = index.sync()
    assert deleted == 1
    assert index.stats() == (0, 0)


def test_long_markdown_lines_remain_searchable(conn, settings):
    notes = settings.vault_dir / "notes"
    long_line = "前缀" + "填充内容" * 900 + "关键结论在长行末尾"
    _write(notes / "long.md", long_line + "\n")
    index = _index(conn, notes)
    index.sync()

    hits = index.search("关键结论")
    assert hits
    assert hits[0].start_line == 1
    assert hits[0].end_line == 1


def test_unchanged_files_are_not_reindexed(conn, settings):
    notes = settings.vault_dir / "notes"
    _write(notes / "a.md", "论文投稿截止日期是十一月二十日。\n")
    index = _index(conn, notes)
    assert index.sync()[0] > 0
    assert index.sync() == (0, 0)


def test_overlapping_roots_index_each_file_once(conn, settings):
    notes = settings.vault_dir / "notes"
    _write(notes / "a.md", "唯一内容：自动化实验记录。\n")
    index = KnowledgeIndex(conn, (notes, notes.parent))
    index.sync()
    assert index.stats() == (1, 1)


def test_split_blocks_keeps_line_ranges():
    text = "# 标题\n\n" + "正文" * 100 + "\n\n尾段\n"
    blocks = split_blocks(text)
    assert blocks[0].start_line == 1
    assert blocks[0].heading == "标题"
    assert blocks[-1].end_line >= blocks[-1].start_line


def test_tokenize_keeps_chinese_and_english():
    tokens = tokenize("截止DEADLINE 11月20日").split()
    assert "截止" in tokens
    assert "deadline" in tokens
    assert "11" in tokens


def test_display_path_shortens_home():
    assert display_path(str(Path.home() / "notes" / "a.md")) == "~/notes/a.md"
    assert display_path("/tmp/a.md") == "/tmp/a.md"
