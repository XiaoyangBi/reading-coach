"""Workbench: the same coach in a browser tab, on your own machine only.

The CLI is still the reference entry point; this file is a second front door
onto the very same Session. Nothing about coaching is reimplemented here:

  say(text)   -> one typed event appended to the live session's event list
  ask(prompt) -> blocks the session thread until the page POSTs a reply

so the coach, judge, ledger, notes, undo and failure report behave exactly as
they do in the terminal (and stay covered by the same offline tests). The page
streams events over SSE and posts replies; the four control words (跳过 /
深挖 / 撤销 / 结束) are buttons that send those words.

Boundaries:
  - binds 127.0.0.1 only, and rejects any Host / Origin that is not this
    server, so a web page you visit cannot drive your coach (DNS rebinding)
  - model and weread keys stay in this process; the page only learns whether
    a key exists (key_ready), never its value
  - one live session at a time; a page that goes silent for ASK_TIMEOUT is
    treated as 结束, so an abandoned tab never holds the session thread
  - the proactive runtime's shadow decisions are shown read-only; this server
    never writes into that project's store

    reading-coach-web             start (or reuse) the server and open the page
    reading-coach-web --no-open   start without opening a browser (launchd)
"""
from __future__ import annotations

# - [INPUT]: 依赖 config、credentials、scheduler、book/coach/ledger/notes/session/tracelog，函数内复用 cli 的 make_models 与 shelf.build_book；标准库 http.server / threading。
# - [OUTPUT]: 对外提供 make_server、main、entry、App、Handler、Live、classify、books_view、concepts_view、runtime_view。
# - [POS]: 浏览器工作台入口。把同一个 Session 的 say/ask 映射成 SSE 事件与等待回复；Host/Origin 白名单加可选 Basic 令牌构成访问边界；页面只收到 BYOK 模型的非敏感描述，永远拿不到 key。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import base64
import binascii
import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, config, credentials, scheduler
from .book import Library
from .coach import opening
from .ledger import Ledger
from .notes import Notes
from .session import LEVEL_LABEL, Session
from .tracelog import Trace, scrub

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
PORT = config.port()
LOGGER = logging.getLogger("reading_coach.web")
ASK_TIMEOUT = 30 * 60      # seconds without a reply before the session is treated as 结束
HEARTBEAT = 15             # seconds between SSE keep-alive comments
REPLY_KIND = {"答> ": "answer", "r/s> ": "retry", "确认> ": "confirm"}
QUESTION_RE = re.compile(r"^\[(?P<tier>[^\]]+)\]\s*(?P<where>[^\n]*)\n问：(?P<question>.*)$", re.S)


# ---- events: what the session says, typed for the page ------------------------

def classify(text: str) -> dict:
    """Turn one say() line into a typed event. The CLI prints the same text;
    the page only needs to know which block it belongs to."""
    t = text.strip("\n")
    if t.startswith("· ") or t.startswith("（划线来源"):
        return {"kind": "narrate", "text": t.lstrip("· ").strip()}
    if t.startswith("档位："):
        return {"kind": "mode", "text": t.removeprefix("档位：")}
    if t.startswith("计划："):
        lines = [ln.strip() for ln in t.splitlines()[1:]]
        hint = [ln for ln in lines if ln.startswith("（随时")]
        return {"kind": "plan", "items": [ln for ln in lines if ln and ln not in hint]}
    m = QUESTION_RE.match(t)
    if m:
        return {"kind": "question", "tier": m["tier"], "where": m["where"].strip(), "question": m["question"].strip()}
    if t.startswith("判定："):
        head, _, ledger_line = t.partition("\n")
        level = next((lv for lv, lb in LEVEL_LABEL.items() if head.startswith(f"判定：{lb}")), "")
        return {"kind": "verdict", "level": level, "text": head.removeprefix("判定："),
                "ledger": ledger_line.removeprefix("账本：")}
    if t.startswith("笔记"):
        return {"kind": "note", "text": t}
    if t.startswith("判题失败"):
        return {"kind": "error", "text": t}
    if t.startswith(("已撤销", "撤销失败", "这次会话还没有可以撤销")):
        return {"kind": "undo", "text": t}
    if t.startswith("（判错了或者想重答"):              # the CLI says "type 撤销"; here it is a button
        return {"kind": "info", "text": "判错了或者想重答：在下一题作答前点「撤销上一题」"}
    if t.startswith("==== 这次陪练"):
        return {"kind": "report", "lines": t.splitlines()[1:]}
    return {"kind": "info", "text": t}


class Live:
    """One coaching session running in a background thread, seen from the web."""

    def __init__(self, sid: str):
        self.id, self.events, self.done = sid, [], False
        self.waiting: str | None = None
        self.replies: queue.Queue = queue.Queue()
        self.cond = threading.Condition()

    def emit(self, kind: str, **data) -> None:
        with self.cond:
            self.events.append({"seq": len(self.events), "kind": kind, **data})
            self.cond.notify_all()

    def say(self, text) -> None:
        ev = classify(scrub(str(text)))
        self.emit(ev.pop("kind"), **ev)

    def ask(self, prompt: str = "") -> str:
        self.waiting = REPLY_KIND.get(prompt, "answer")
        self.emit("ask", want=self.waiting)
        try:
            text = self.replies.get(timeout=ASK_TIMEOUT)
        except queue.Empty:
            self.emit("info", text="30 分钟没有回应，按「结束」处理")
            raise EOFError
        finally:
            self.waiting = None
        self.emit("you", text=text)
        return text

    def confirm(self, text: str) -> bool:
        self.emit("confirm", text=text)
        return self.ask("确认> ").strip().lower() in ("y", "是", "确认")

    def reply(self, text: str) -> str | None:
        """Hand a reply to the session; returns an error string when nobody is asking."""
        if self.done:
            return "这局已经结束了"
        if self.waiting is None:
            return "教练还在想，等它问完再回答"
        self.replies.put(text)
        return None

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def abort(self) -> None:
        """User gave up: unlock the workbench now, then let the thread wind down."""
        self.replies.put("结束")        # wakes ask(), so the thread reaches its report
        self.finish()                  # active() stops seeing it; buttons unlock at once

    def since(self, seq: int, timeout: float) -> list[dict]:
        with self.cond:
            self.cond.wait_for(lambda: len(self.events) > seq or self.done, timeout)
            return self.events[seq:]


def run_live(live: Live, home: Path, make_models, *, book_id=None, title=None, focus=None, shelf_build=None):
    """The CLI's main() without the terminal: pick or sync the book, then Session.run."""
    from .cli import SHELF_NOTE
    db = config.connect(home)
    try:
        library, ledger = Library(db), Ledger(db)
        if shelf_build is None:
            from .shelf import build_book as shelf_build
        if book_id is not None:
            row = db.execute("SELECT title, path FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                raise ValueError("书架上没有这本书")
            title = row[0] if (row[1] or "").startswith("weread:") else None
        shelf_mode = False
        if title:                                   # shelf mode: resync your highlights first, like the CLI
            live.say(f"· 在同步你在微信读书上的《{title}》划线")
            try:
                book, meta = shelf_build(title)
                book_id, status = library.import_parsed(book, f"weread:{meta['bookId']}", confirm=live.confirm)
                live.say(f"· 你在 {meta['chapters_kept']} 章留下 {meta['highlights']} 条划线"
                         + (f"、{meta['notes']} 条想法" if meta.get("notes") else "")
                         + ("（有更新，已同步）" if status == "replaced" else ""))
            except Exception as e:
                if book_id is None:
                    raise
                live.say(f"划线同步失败（{e}），用上次同步的内容继续")
            shelf_mode = True
        summary = ledger.summary(book_id)
        request = (f"先复习「{focus}」" if focus else
                   "有到期的概念，先复习" if summary["到期"] else "开始今天的陪练")
        session = Session(library=library, ledger=ledger, notes=Notes(home / "notes"),
                          trace=Trace(db, "web-" + time.strftime("%Y%m%d-%H%M%S")), book_id=book_id,
                          coach_model=None, judge_model=None, ask=live.ask, say=live.say,
                          highlights_root=home / "highlights", content_from_highlights=shelf_mode)
        live.say("· 在连接模型")
        session.coach_model, session.judge_model = make_models(session.registry.schemas())
        live.emit("book", book_id=book_id, title=session.title, summary=summary)
        session.run(opening(session.title, len(library.chapters(book_id)), summary, request,
                            content_note=SHELF_NOTE if shelf_mode else ""))
    except Exception as e:
        live.emit("error", text=scrub(f"没能开始：{e}。账本和笔记没有改动"))
    finally:
        db.close()
        live.finish()


# ---- read models for the page ---------------------------------------------------

def books_view(db) -> list[dict]:
    library, ledger = Library(db), Ledger(db)
    paths = dict(db.execute("SELECT id, path FROM books"))
    return [{**b, "shelf": (paths.get(b["id"]) or "").startswith("weread:"), "summary": ledger.summary(b["id"])}
            for b in library.books()]


def concepts_view(db, book_id: int) -> list[dict]:
    Ledger(db)
    now = time.time()
    out = []
    for cid, name, anchor, status, interval, reviews, due_at in db.execute(
            "SELECT id, name, anchor, status, interval_days, reviews, due_at FROM concepts "
            "WHERE book_id=? ORDER BY (due_at IS NULL), due_at", (book_id,)):
        last = db.execute("SELECT level FROM answers WHERE concept_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        out.append({"id": cid, "name": name, "anchor": anchor, "status": status, "interval_days": interval,
                    "reviews": reviews, "due": time.strftime("%m-%d", time.localtime(due_at)) if due_at else "",
                    "overdue": bool(due_at and due_at <= now),
                    "last": LEVEL_LABEL.get(last[0], "") if last else ""})
    return out


def runtime_view(store: str | None, limit: int = 20) -> dict:
    """Shadow decisions from the proactive runtime, read-only."""
    if not store:
        return {"configured": False, "items": [], "note": "没有配置 PROACTIVE_RUNTIME_STORE"}
    path = Path(store) / "decisions.jsonl"
    if not path.exists():
        return {"configured": True, "items": [], "note": "影子运行还没有产生决策"}
    items = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        chosen = next((c for c in d.get("candidates", []) if c.get("candidate_id") == d.get("chosen_candidate_id")), {})
        items.append({"at": d.get("occurred_at", "")[:16].replace("T", " "), "status": d.get("status"),
                      "reason": d.get("reason", ""), "summary": chosen.get("summary", ""),
                      "notification": bool(d.get("notification"))})
    return {"configured": True, "items": items[::-1], "note": ""}


# ---- http ---------------------------------------------------------------------

class App:
    def __init__(self, home: Path, port: int, make_models, shelf_build=None, key_ready=None, runtime_store=None):
        self.home, self.port, self.make_models, self.shelf_build = home, port, make_models, shelf_build
        self.key_ready = key_ready or (lambda: True)
        self.runtime_store = runtime_store
        self.lives: dict[str, Live] = {}
        self.lock = threading.Lock()

    def db(self):
        return config.connect(self.home)

    def prune(self, keep: int = 20) -> None:
        """只保留最近 keep 个已结束会话，避免长驻进程内存无界增长。"""
        done = [lv for lv in list(self.lives.values()) if lv.done]
        for lv in done[:-keep]:
            self.lives.pop(lv.id, None)

    def active(self) -> Live | None:
        return next((lv for lv in self.lives.values() if not lv.done), None)

    def start(self, body: dict) -> tuple[int, dict]:
        if not self.key_ready():
            return 400, {"error": credentials.describe().get("error") or "还没有配置 BYOK 模型密钥"}
        self.prune()
        with self.lock:
            if self.active():
                return 409, {"error": "已经有一局在进行", "id": self.active().id}
            live = Live(time.strftime("s%Y%m%d%H%M%S") + f"{len(self.lives)}")
            self.lives[live.id] = live
        kwargs = {"focus": body.get("focus"), "shelf_build": self.shelf_build}
        if body.get("book_id") is not None:
            kwargs["book_id"] = int(body["book_id"])
        elif (body.get("title") or "").strip():
            kwargs["title"] = body["title"].strip()
        else:
            live.finish()
            return 400, {"error": "要么选一本书，要么输入书名"}
        threading.Thread(target=run_live, args=(live, self.home, self.make_models), kwargs=kwargs,
                         daemon=True).start()
        return 200, {"id": live.id}


class Handler(BaseHTTPRequestHandler):
    app: App = None                                  # set by make_server

    def log_message(self, fmt, *args):
        LOGGER.debug("%s %s", self.address_string(), fmt % args)

    def _allowed(self) -> bool:
        ok = config.allowed_hosts(self.server.server_address[1])
        if self.headers.get("Host") not in ok:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.removeprefix("http://") in ok

    def _auth(self) -> bool:
        """配置令牌时要求 HTTP Basic。浏览器为该源缓存凭据，fetch 与 SSE 自动携带，页面无需改动。

        /api/health 只返回常量，豁免认证，供容器 healthcheck 与 launchd 探活。
        """
        token = config.access_token()
        if token is None or self.path.partition("?")[0] == "/api/health":
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return False
        return hmac.compare_digest(raw.partition(":")[2], token)

    def _guard(self) -> bool:
        if not self._allowed():
            self._send(403, {"error": "只接受本机访问"})
            return False
        if not self._auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="reading-coach"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = b'{"error":"unauthorized"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if not self._guard():
            return
        path, _, query = self.path.partition("?")
        app = self.app
        if path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")   # a page refresh must pick up UI edits
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/health":
            return self._send(200, {"ok": True, "app": "reading-coach", "version": __version__,
                                    "db": (app.home / "coach.db").exists(),
                                    "live": sum(1 for lv in app.lives.values() if not lv.done)})
        db = app.db()
        try:
            if path == "/api/today":
                plan = scheduler.plan_today(db)
                live = app.active()
                info = None
                if live:
                    book = next((e.get("title") for e in live.events if e["kind"] == "book"), None)
                    info = {"id": live.id, "waiting": live.waiting, "book": book}
                return self._send(200, {**plan, "key_ready": app.key_ready(),
                                        "model_info": credentials.describe(),
                                        "live": live.id if live else None, "live_info": info})
            if path == "/api/books":
                return self._send(200, books_view(db))
            m = re.fullmatch(r"/api/books/(\d+)/concepts", path)
            if m:
                return self._send(200, concepts_view(db, int(m[1])))
            if path == "/api/pushes":
                return self._send(200, {"pushes": scheduler.push_history(db),
                                        "runtime": runtime_view(app.runtime_store)})
        finally:
            db.close()
        m = re.fullmatch(r"/api/session/(\w+)/events", path)
        if m and m[1] in app.lives:
            return self._stream(app.lives[m[1]], int(dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                                                      .get("since", 0)))
        self._send(404, {"error": "没有这个地址"})

    def _stream(self, live: Live, seq: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                fresh = live.since(seq, HEARTBEAT)
                for ev in fresh:
                    self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                seq += len(fresh)
                if not fresh:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                if live.done and seq >= len(live.events):
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return                                   # the tab closed; the session keeps its own timeout

    def do_POST(self):
        if not self._guard():
            return
        app, body = self.app, self._body()
        if self.path == "/api/session":
            return self._send(*app.start(body))
        m = re.fullmatch(r"/api/session/(\w+)/reply", self.path)
        if m:
            live = app.lives.get(m[1])
            if live is None:
                return self._send(404, {"error": "没有这局"})
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send(400, {"error": "回答是空的"})
            err = live.reply(text)
            return self._send(409, {"error": err}) if err else self._send(200, {"ok": True})
        m = re.fullmatch(r"/api/session/(\w+)/abort", self.path)
        if m:
            live = app.lives.get(m[1])
            if live is None:
                return self._send(404, {"error": "没有这局"})
            live.abort()
            return self._send(200, {"ok": True})
        db = app.db()
        try:
            if self.path == "/api/feedback":
                try:
                    fid = scheduler.add_feedback(db, str(body.get("target", "")), str(body.get("value", "")),
                                                 body.get("concept_id"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"id": fid, "label": scheduler.FEEDBACK[body["value"]]})
            if self.path == "/api/feedback/undo":
                ok = scheduler.undo_feedback(db, int(body.get("id", 0)))
                return self._send(200 if ok else 404, {"ok": ok})
        finally:
            db.close()
        self._send(404, {"error": "没有这个地址"})


def make_server(home: Path, port: int, make_models, **kwargs) -> ThreadingHTTPServer:
    config.require_token()
    config.init_db(home)
    handler = type("BoundHandler", (Handler,), {"app": App(home, port, make_models, **kwargs)})
    server = ThreadingHTTPServer((config.bind_host(), port), handler)
    server.daemon_threads = True
    handler.app.port = server.server_address[1]
    return server


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
            return json.load(r).get("app") == "reading-coach"
    except Exception:
        return False


def warm_sdk() -> None:
    """Import the model SDK in the background at startup. On this machine a cold
    `import openai` has taken over a minute; paying it before the first click
    keeps 开始一题 from looking frozen."""
    import importlib
    import importlib.util
    try:
        provider = credentials.resolve()
    except credentials.CredentialsError:
        return
    name = "openai" if provider and provider.kind == "openai" else "anthropic"
    threading.Thread(target=lambda: importlib.util.find_spec(name) and importlib.import_module(name),
                     daemon=True).start()


def main(argv) -> int:
    from .cli import has_model_key, load_env, make_models
    load_env()
    config.setup_logging()
    try:
        config.require_token()
    except RuntimeError as e:
        print(f"启动被拒绝：{e}", file=sys.stderr)
        return 2
    host = config.bind_host()
    url = f"http://{'127.0.0.1' if config.is_loopback(host) else host}:{PORT}/"
    if already_running(PORT):
        print(f"工作台已经在运行：{url}")
        if "--no-open" not in argv:
            webbrowser.open(url)
        return 0
    server = make_server(config.data_home(), PORT, make_models, key_ready=has_model_key,
                         runtime_store=os.environ.get("PROACTIVE_RUNTIME_STORE"))
    if has_model_key():
        warm_sdk()
    print(f"工作台：{url}（Ctrl-C 退出）")
    if not has_model_key():
        print("提醒：还没有配置 BYOK 模型密钥，可以看账本和台账，但开不了陪练")
    if "--no-open" not in argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("收到 Ctrl-C，正在停止")
    finally:
        server.server_close()
    return 0


def entry() -> int:
    """console_scripts 入口。setuptools 负责 sys.exit。"""
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
