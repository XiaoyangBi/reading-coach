"""Notes: your passing answers, in your own words, as markdown on disk.

The coach never writes note content. When the judge passes an answer, the
session appends that answer verbatim to the concept's note (spec 8: the AI does
not write your reading notes for you). Format is the interface (spec 4.5):
plain markdown with a fixed frontmatter, readable by any other tool.

Writes only append, never overwrite, and every write returns a snapshot of the
file's previous content so the session can undo it (UX gate item 4).
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 pathlib / re；无包内依赖。
# - [OUTPUT]: 对外提供 Notes、slug、frontmatter。
# - [POS]: 用户原话笔记写入器。按书名分文件、只追加不覆盖；session 在撤销时按路径回滚它。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import re
import time
from pathlib import Path


def slug(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")[:60] or "untitled"


def frontmatter(book: str, chapter: str, anchor: str, concept: str, status: str) -> str:
    return (f"---\nbook: {book}\nchapter: {chapter}\nanchor: {anchor}\nconcept: {concept}\n"
            f"status: {status}\nupdated: {time.strftime('%Y-%m-%d')}\n---\n")


class Notes:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, book: str, concept: str) -> Path:
        return self.root / slug(book) / f"{slug(concept)}.md"

    def show(self, path) -> str:
        """A path the user can read: 笔记/书名/概念.md, not a temp-dir absolute path."""
        try:
            return "笔记/" + str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    def append(self, *, book, chapter, anchor, concept, status, question, answer) -> dict:
        """Append one passing answer. Returns {"path", "before", "added"}; before is None for a new file."""
        p = self.path(book, concept)
        before = p.read_text(encoding="utf-8") if p.exists() else None
        body = before.split("---\n", 2)[2] if before and before.startswith("---\n") else (before or "")
        added = f"\n## {time.strftime('%Y-%m-%d')} {question}\n\n{answer.strip()}\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(frontmatter(book, chapter, anchor, concept, status) + body + added, encoding="utf-8")
        return {"path": str(p), "before": before, "added": added}

    def undo(self, snapshot: dict) -> None:
        p = Path(snapshot["path"])
        if snapshot["before"] is None:
            p.unlink(missing_ok=True)                   # the note did not exist before this session's write
        else:
            p.write_text(snapshot["before"], encoding="utf-8")
