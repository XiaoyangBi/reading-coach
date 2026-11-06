"""The one entry point. Everything the user does starts here.

    reading-coach 金字塔原理    shelf mode: coach on your weread highlights of that book
    reading-coach 书.epub      file mode: import (or reuse) a local epub / txt / md / pdf
    reading-coach              pick a book, due reviews first

Inside a session there is nothing to learn but four words, typed at any answer
prompt: 跳过 / 深挖 / 撤销 / 结束. This is the only file that imports the
anthropic SDK and dotenv (the reference repo keeps them in demo.py); the API
key is read from .env by the SDK and never printed, logged, or traced.

Data lives under $READING_COACH_HOME (default: ./data): coach.db (books,
ledger, trace), notes/, highlights/.
"""
from __future__ import annotations

# - [INPUT]: 依赖 config 与 book/coach/judge/ledger/notes/recovery/session/tracelog；惰性依赖 models、shelf、anthropic、python-dotenv。
# - [OUTPUT]: 对外提供 main、entry、load_env、has_model_key、make_models、confirm、pick_book、SHELF_NOTE。
# - [POS]: 终端入口，也是唯一按 key 选择 provider 的位置。web 复用它的 make_models 与 SHELF_NOTE；它只装配 Session 并驱动一轮，不实现教练逻辑。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import os
import sys
import time
from pathlib import Path

from . import config
from .book import Library
from .coach import SYSTEM as COACH_SYSTEM
from .coach import opening
from .judge import SYSTEM as JUDGE_SYSTEM
from .judge import VERDICT_TOOL
from .ledger import Ledger
from .notes import Notes
from .recovery import with_retry
from .session import Session
from .tracelog import Trace

SHELF_NOTE = "本书内容来自用户在微信读书上的划线和想法（书架模式），每个段落都是用户读过并标记的句子"


def load_env() -> None:
    """把 .env 读进进程。web 入口复用此名字，实现委托给 config。"""
    config.load_env()


def has_model_key() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def make_models(registry_schemas):
    """Build the live coach and judge callables. Provider is picked by which key
    exists: DEEPSEEK_API_KEY (or any OpenAI-compatible endpoint via
    DEEPSEEK_BASE_URL) wins, else ANTHROPIC_API_KEY. SDKs import lazily so
    offline tests never need them."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        from .models import make_deepseek
        return make_deepseek(COACH_SYSTEM, JUDGE_SYSTEM, VERDICT_TOOL, registry_schemas)

    from anthropic import Anthropic
    client = Anthropic(base_url=os.environ.get("ANTHROPIC_BASE_URL") or None)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def coach_model(messages):
        return with_retry(lambda: client.messages.create(
            model=model, system=COACH_SYSTEM, messages=messages, tools=registry_schemas, max_tokens=2048))

    def judge_model(messages):
        return with_retry(lambda: client.messages.create(
            model=model, system=JUDGE_SYSTEM, messages=messages, tools=[VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "submit_verdict"}, max_tokens=1024))

    return coach_model, judge_model


def confirm(text: str) -> bool:
    print(text)
    return input("确定要重新导入吗？输入 y 确认，其他任意键保留原来的版本> ").strip().lower() == "y"


def pick_book(library: Library, ledger: Ledger) -> int | None:
    books = library.books()
    if not books:
        print("还没有导入过书。用法：reading-coach 你的书.epub（支持 epub / txt / md / pdf）")
        return None
    due = {b["id"]: len(ledger.due(b["id"])) for b in books}
    books.sort(key=lambda b: -due[b["id"]])
    for i, b in enumerate(books, 1):
        print(f"  {i}. 《{b['title']}》" + (f"  {due[b['id']]} 个概念到期" if due[b["id"]] else ""))
    choice = input("选一本（回车选第 1 本）> ").strip() or "1"
    try:
        return books[int(choice) - 1]["id"]
    except (ValueError, IndexError):
        print("没有这个编号")
        return None


def main(argv):
    load_env()
    config.setup_logging()
    if not has_model_key():
        print("需要模型 key 才能开始：在 reading-coach/.env 里写 DEEPSEEK_API_KEY=... "
              "或 ANTHROPIC_API_KEY=...（参考 .env.example）")
        return 1

    home = config.data_home()
    config.init_db(home)
    db = config.connect(home)
    library, ledger = Library(db), Ledger(db)

    shelf_mode = False
    if len(argv) > 1:
        arg = argv[1]
        if Path(arg).expanduser().exists():
            try:
                book_id, status = library.import_book(arg, confirm=confirm)
            except Exception as e:
                print(f"导入失败：{e}")
                return 1
            print({"new": "已导入", "same": "这本书导入过，直接继续", "replaced": "已重新导入",
                   "kept": "保留了原来的版本"}[status] + f"：《{library.title(book_id)}》")
        else:                                   # not a file: treat it as a book title on your shelf
            from .shelf import build_book
            try:
                book, meta = build_book(arg)
            except Exception as e:
                print(f"书架模式启动失败：{e}")
                return 1
            book_id, status = library.import_parsed(book, f"weread:{meta['bookId']}", confirm=confirm)
            shelf_mode = True
            print(f"书架模式：《{book.title}》全书 {meta['chapters_total']} 章，"
                  f"你在 {meta['chapters_kept']} 章留下 {meta['highlights']} 条划线"
                  + (f"、{meta['notes']} 条想法" if meta["notes"] else "")
                  + ("（划线有更新，已同步）" if status == "replaced" else ""))
    else:
        book_id = pick_book(library, ledger)
        if book_id is None:
            return 1

    summary = ledger.summary(book_id)
    request = "有到期的概念，先复习" if summary["到期"] else "开始今天的陪练"
    session = Session(library=library, ledger=ledger, notes=Notes(home / "notes"),
                      trace=Trace(db, time.strftime("%Y%m%d-%H%M%S")), book_id=book_id,
                      coach_model=None, judge_model=None, highlights_root=home / "highlights",
                      content_from_highlights=shelf_mode)
    session.coach_model, session.judge_model = make_models(session.registry.schemas())
    print(f"\n《{session.title}》：学习中 {summary['学习中']}，卡壳 {summary['卡壳']}，"
          f"掌握 {summary['掌握']}，到期 {summary['到期']}")
    content_note = SHELF_NOTE if shelf_mode else ""
    session.run(opening(session.title, len(library.chapters(book_id)), summary, request,
                        content_note=content_note))
    return 0


def entry() -> int:
    """console_scripts 入口。setuptools 负责 sys.exit。"""
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
