"""Book library: turn a local epub / txt / md / pdf into chapters of numbered
paragraphs, store them, and serve them back by anchor.

Every question the coach asks must point at the original text, so the unit of
storage is the paragraph and its address is an anchor "章.段" (3.12 = chapter 3,
paragraph 12), shown to the user as "第3章第12段". The judge receives the
anchored paragraphs, so a verdict is always checked against the book, not
against the model's memory of the book.

Re-importing a changed file renumbers paragraphs and can orphan anchors already
in the ledger, so import_book() previews the change and asks before replacing
(a medium-risk action under the UX gate). Parsing is stdlib only; pdf needs
the optional pypdf package.
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 zipfile / html.parser / re / sqlite3 / pathlib，可选 pypdf；不依赖包内模块。
# - [OUTPUT]: 对外提供 Book、Library、parse_book、parse_epub、parse_text、parse_pdf、anchor_label。
# - [POS]: 领域层唯一书籍解析器。把 epub/txt/md/pdf 折成章节与段落锚点并写入 books/paragraphs；重导入会重排锚点，因此先预览差异，再把替换决定交给 cli 传入的 confirm 回调，自己不询问用户。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

READ_CHARS = 4000          # one read_chapter call returns at most this much text
MIN_CHAPTER_CHARS = 80     # epub spine items shorter than this are covers / toc pages
TXT_CHUNK = 40             # a txt with no headings is cut into chapters of this many paragraphs
PDF_PAGES = 10             # pdf chapters are page ranges of this size
HEADING_RE = re.compile(r"^(#{1,3}\s+.+|第[一二三四五六七八九十百零〇两\d]+[章节回篇部讲].*)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY, title TEXT UNIQUE, path TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS paragraphs (book_id INTEGER, chapter INTEGER, chapter_title TEXT,
  para INTEGER, text TEXT);
"""


@dataclass
class Book:
    title: str
    chapters: list = field(default_factory=list)   # [(chapter_title, [paragraph, ...]), ...]

    def digest(self) -> str:
        h = hashlib.sha1()
        for t, paras in self.chapters:
            h.update(t.encode())
            for p in paras:
                h.update(p.encode())
        return h.hexdigest()

    def counts(self) -> tuple[int, int]:
        return len(self.chapters), sum(len(p) for _, p in self.chapters)


def anchor_label(anchor: str) -> str:
    """'3.12' -> '第3章第12段'; '3.12-3.15' -> '第3章第12-15段'."""
    m = re.fullmatch(r"(\d+)\.(\d+)(?:-(?:\d+\.)?(\d+))?", anchor.strip())
    if not m:
        return anchor
    c, p, q = m.groups()
    return f"第{c}章第{p}-{q}段" if q else f"第{c}章第{p}段"


# ---- parsing ---------------------------------------------------------------

class _Blocks(HTMLParser):
    """Collect text per block element; headings remembered for the chapter title."""
    BLOCK = {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "br", "tr"}

    def __init__(self):
        super().__init__()
        self.paras, self.heading, self._buf, self._skip = [], "", [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "title"):
            self._skip += 1
        if tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOCK:
            text = self._flush()
            if tag in ("h1", "h2", "h3") and text and not self.heading:
                self.heading = text                       # first heading names the chapter

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self) -> str:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            self.paras.append(text)
        return text


def parse_epub(path: Path) -> Book:
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(opf_path))
        base = PurePosixPath(opf_path).parent
        title = next((e.text for e in opf.iter() if e.tag.endswith("title") and e.text), path.stem)
        manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item")}
        spine = [e.get("idref") for e in opf.iter() if e.tag.endswith("itemref")]
        book = Book(title.strip())
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            parser = _Blocks()
            parser.feed(z.read(str(base / href)).decode("utf-8", errors="ignore"))
            parser._flush()
            if sum(len(p) for p in parser.paras) < MIN_CHAPTER_CHARS:
                continue                                  # cover, copyright, toc
            heading = parser.heading or f"第{len(book.chapters) + 1}部分"
            paras = [p for p in parser.paras if p != heading] or parser.paras
            book.chapters.append((heading, paras))
    return book


def parse_text(path: Path) -> Book:
    lines = [l.strip() for l in path.read_text(encoding="utf-8", errors="ignore").splitlines()]
    book, title, paras = Book(path.stem), None, []
    for line in lines:
        if not line:
            continue
        if HEADING_RE.match(line):
            if paras:
                book.chapters.append((title or f"第{len(book.chapters) + 1}部分", paras))
            title, paras = line.lstrip("#").strip(), []
        else:
            paras.append(line)                            # one non-empty line = one paragraph
    if paras:
        book.chapters.append((title or f"第{len(book.chapters) + 1}部分", paras))
    if len(book.chapters) == 1 and len(book.chapters[0][1]) > TXT_CHUNK * 1.5:
        _, all_paras = book.chapters[0]                   # no headings at all: cut into chunks
        book.chapters = [(f"第{i // TXT_CHUNK + 1}部分", all_paras[i:i + TXT_CHUNK])
                         for i in range(0, len(all_paras), TXT_CHUNK)]
    return book


def parse_pdf(path: Path) -> Book:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("读 pdf 需要先安装 pypdf：pip install pypdf") from e
    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception:                                # one bad page must not kill the book
            pages.append("")
        if (i + 1) % 50 == 0:
            print(f"  解析 pdf 中… {i + 1}/{len(reader.pages)} 页", flush=True)
    book = Book(path.stem)
    for i in range(0, len(pages), PDF_PAGES):
        text = "\n".join(pages[i:i + PDF_PAGES])
        paras = [re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n\s*\n|(?<=[。！？])\n", text)]
        paras = [p for p in paras if len(p) > 1]
        if paras:
            book.chapters.append((f"第{i + 1}-{min(i + PDF_PAGES, len(pages))}页", paras))
    return book


def parse_book(path) -> Book:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")
    suffix = path.suffix.lower()
    parser = {".epub": parse_epub, ".txt": parse_text, ".md": parse_text, ".pdf": parse_pdf}.get(suffix)
    if parser is None:
        raise ValueError(f"暂不支持 {suffix} 格式，支持 epub / txt / md / pdf")
    book = parser(path)
    if not book.chapters:
        raise ValueError(f"《{book.title}》没解析出正文，可能是扫描版或加密文件")
    return book


# ---- storage and access ----------------------------------------------------

class Library:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def import_book(self, path, confirm=lambda text: True) -> tuple[int, str]:
        """Parse and store a local file; returns (book_id, status): new / same / replaced / kept."""
        return self.import_parsed(parse_book(path), str(path), confirm=confirm)

    def import_parsed(self, book: Book, origin: str, confirm=lambda text: True) -> tuple[int, str]:
        """Store an already-parsed Book (a local file parse, or a shelf book built
        from weread highlights). `confirm(preview)` is asked only when a book
        with the same title exists with different content."""
        row = self.db.execute("SELECT id, digest FROM books WHERE title=?", (book.title,)).fetchone()
        if row and row[1] == book.digest():
            return row[0], "same"
        if row:
            old_c, old_p = self.db.execute(
                "SELECT COUNT(DISTINCT chapter), COUNT(*) FROM paragraphs WHERE book_id=?", (row[0],)).fetchone()
            new_c, new_p = book.counts()
            preview = (f"《{book.title}》之前导入过，这次文件内容不同：\n"
                       f"  原来 {old_c} 章 {old_p} 段 -> 现在 {new_c} 章 {new_p} 段\n"
                       f"  重新导入后段落编号会变，账本里已有概念的原文锚点可能对不上。")
            if not confirm(preview):
                return row[0], "kept"
            self.db.execute("DELETE FROM paragraphs WHERE book_id=?", (row[0],))
            self.db.execute("UPDATE books SET path=?, digest=? WHERE id=?", (origin, book.digest(), row[0]))
            book_id, status = row[0], "replaced"
        else:
            cur = self.db.execute("INSERT INTO books (title, path, digest) VALUES (?,?,?)",
                                  (book.title, origin, book.digest()))
            book_id, status = cur.lastrowid, "new"
        self.db.executemany(
            "INSERT INTO paragraphs VALUES (?,?,?,?,?)",
            [(book_id, ci, t, pi, p) for ci, (t, paras) in enumerate(book.chapters, 1)
             for pi, p in enumerate(paras, 1)])
        self.db.commit()
        return book_id, status

    def books(self) -> list[dict]:
        return [{"id": i, "title": t} for i, t in self.db.execute("SELECT id, title FROM books ORDER BY id")]

    def title(self, book_id: int) -> str:
        row = self.db.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
        return row[0] if row else ""

    def chapters(self, book_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT chapter, chapter_title, COUNT(*), SUM(LENGTH(text)) FROM paragraphs "
            "WHERE book_id=? GROUP BY chapter ORDER BY chapter", (book_id,))
        return [{"chapter": c, "title": t, "paragraphs": n, "chars": s} for c, t, n, s in rows]

    def chapter_title(self, book_id: int, chapter: int) -> str:
        row = self.db.execute("SELECT chapter_title FROM paragraphs WHERE book_id=? AND chapter=? LIMIT 1",
                              (book_id, chapter)).fetchone()
        return row[0] if row else ""

    def read(self, book_id: int, chapter: int, start: int = 1, end: int | None = None) -> str:
        """Paragraphs [start, end] of a chapter, each prefixed with its anchor, capped at READ_CHARS."""
        rows = self.db.execute(
            "SELECT para, text FROM paragraphs WHERE book_id=? AND chapter=? AND para>=? "
            "AND (? IS NULL OR para<=?) ORDER BY para", (book_id, chapter, start, end, end)).fetchall()
        if not rows:
            return f"第{chapter}章没有第{start}段之后的内容"
        out, used = [], 0
        for para, text in rows:
            line = f"[{chapter}.{para}] {text}"
            if used + len(line) > READ_CHARS and out:
                out.append(f"...（到此为止，下一次从第{para}段读起）")
                break
            out.append(line)
            used += len(line)
        return "\n".join(out)

    def search(self, book_id: int, query: str, limit: int = 8) -> list[dict]:
        rows = self.db.execute(
            "SELECT chapter, para, text FROM paragraphs WHERE book_id=? AND text LIKE ? "
            "ORDER BY chapter, para LIMIT ?", (book_id, f"%{query}%", limit))
        return [{"anchor": f"{c}.{p}", "text": t[:160]} for c, p, t in rows]

    def anchor_text(self, book_id: int, anchor: str) -> str:
        """The original paragraphs an anchor points to ('3.12' or '3.12-3.15')."""
        m = re.fullmatch(r"(\d+)\.(\d+)(?:-(?:\d+\.)?(\d+))?", anchor.strip())
        if not m:
            return ""
        c, p, q = int(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(2))
        rows = self.db.execute("SELECT text FROM paragraphs WHERE book_id=? AND chapter=? AND para BETWEEN ? AND ? "
                               "ORDER BY para", (book_id, c, p, min(q, p + 9)))
        return "\n".join(t for (t,) in rows)
