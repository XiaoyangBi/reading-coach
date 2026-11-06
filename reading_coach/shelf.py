"""Shelf mode: the input is a book TITLE, the material is your own WeChat
Reading traces (highlights + notes) on that book.

Design pivot (2026-10-14): the primary workflow is "name a book you have read",
not "upload a file". The weread gateway has no full-text endpoint (copyright),
so a shelf book's "content" is the sentences YOU marked plus the thoughts YOU
wrote — the coach quizzes you on your own reading traces, and the judge grades
against the sentence you highlighted. File mode stays for books not on the
shelf.

A shelf book becomes a normal Book: chapters = TOC entries that have at least
one highlight (chapters with no traces have no anchors, so they are dropped and
the coach is told), paragraphs = the marked sentences, with your notes appended
as 「我的想法：…」 paragraphs. Everything downstream (loop, ledger, judge,
notes, undo) is reused unchanged.
"""
from __future__ import annotations

# - [INPUT]: 依赖 book.Book、highlights.weread_call 与 credentials.weread_key。
# - [OUTPUT]: 对外提供 resolve、build_book。
# - [POS]: 书架模式适配层。按书名解析 bookId，把用户划线折成 Book；cli 与 web 由此进入以划线为原文的陪练。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


from . import credentials
from .book import Book
from .highlights import weread_call


def resolve(title: str, call=weread_call) -> tuple[str, str, int]:
    """Title -> (bookId, canonical title, highlight count of the chosen edition).

    Same title often has several editions (《金字塔原理》vs《金字塔原理大全集》);
    the right one is the edition the USER actually marked up, so candidates are
    ranked by highlight count first, exact title match second.
    """
    key = credentials.weread_key()
    if not key:
        raise RuntimeError("书架模式需要 WEREAD_API_KEY（写在 .env 里）")
    found = call("/store/search", key, keyword=title, scope=10)
    books = [b["bookInfo"] for g in found.get("results", []) for b in g.get("books", []) if "bookInfo" in b]
    if not books:
        raise RuntimeError(f"微信读书里没搜到《{title}》")
    scored = []
    for b in books[:5]:
        marks = call("/book/bookmarklist", key, bookId=str(b["bookId"]))
        n = sum(1 for m in marks.get("updated", []) if m.get("markText"))
        scored.append((n, b.get("title") != title, str(b["bookId"]), b["title"]))
    scored.sort(key=lambda s: (-s[0], s[1]))               # most marks, then exact title
    n, _, book_id, canonical = scored[0]
    return book_id, canonical, n


def build_book(title: str, call=weread_call) -> tuple[Book, dict]:
    """Pull TOC + highlights + notes for `title`, return (Book, meta).

    meta: {"bookId", "chapters_total", "chapters_kept", "highlights", "notes"}
    Raises an honest RuntimeError when there is nothing to coach on.
    """
    book_id, canonical, _ = resolve(title, call)
    key = credentials.weread_key()
    if not key:
        raise RuntimeError("书架模式需要 WEREAD_API_KEY（写在 .env 里）")

    toc = call("/book/chapterinfo", key, bookId=book_id).get("chapters", [])
    marks = call("/book/bookmarklist", key, bookId=book_id)
    reviews = call("/review/list/mine", key, bookid=book_id)

    per_chapter: dict[int, list[str]] = {}
    for m in marks.get("updated", []):
        if m.get("markText"):
            per_chapter.setdefault(m.get("chapterUid"), []).append(m["markText"].strip())
    note_count = 0
    for r in reviews.get("reviews", []):
        content = (r.get("content") or "").strip()
        if not content:
            continue
        target = r.get("chapterUid")
        abstract = (r.get("abstract") or "").strip()
        note = f"我的想法：{content}" + (f"（针对：{abstract[:40]}）" if abstract else "")
        per_chapter.setdefault(target, []).append(note)
        note_count += 1

    if not per_chapter:
        raise RuntimeError(f"《{canonical}》在微信读书上没有你的划线或想法，书架模式出不了题；"
                           f"先去读并划几句，或者把文件传给我走文件模式")

    toc_titles = {c.get("chapterUid"): (c.get("title") or "").strip() for c in toc}
    toc_order = {c.get("chapterUid"): i for i, c in enumerate(toc)}
    book = Book(canonical)
    for uid in sorted(per_chapter, key=lambda u: toc_order.get(u, 1 << 30)):
        name = toc_titles.get(uid) or f"第{uid}节"
        book.chapters.append((name, per_chapter[uid]))

    meta = {"bookId": book_id, "chapters_total": len(toc), "chapters_kept": len(book.chapters),
            "highlights": sum(len(v) for v in per_chapter.values()) - note_count, "notes": note_count}
    return book, meta


__all__ = ["resolve", "build_book"]
