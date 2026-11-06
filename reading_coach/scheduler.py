"""Review scheduler: the background agent that makes the agent come to you.

launchd wakes this process once a day (20:00). It scans the ledger for due
concepts, picks one by rule, pushes one macOS notification, and exits. No
model call, no resident process: the decision that matters (what to push) is
a deterministic rule because getting it wrong costs almost nothing, while the
scheduler's first duty is to be alive every day. This is the reference repo's
section 15 pattern (cron + state) with the ledger as the mailbox between the
foreground coach and this background agent (section 16's inbox, persisted).

Push rules:
  - nothing due -> exit silently (a notification that nags without cause
    teaches the user to ignore notifications)
  - at most one push per calendar day, even if run by hand or twice at once:
    the day is claimed with INSERT OR IGNORE before notifying, and released
    if the notification fails (claim first, then act; never check-then-act)
  - a push nobody answers is a signal, not a reason to push harder (v3): after
    IGNORE_LIMIT unanswered pushes of one concept it backs off (2, 4, then at
    most once every 7 days) and the next due concept gets the slot instead
  - the user's feedback outranks the rule: 别再提这个 mutes a concept, 今天先不
    silences today; both are undoable rows in `feedback`
  - every wake leaves a decision in the trace, silence included, so a missed
    reminder can be told apart from a scheduler that never ran
  - the notification names the book and concept, never the question: spoiling
    the question would let you answer it in your head without opening the app

plan_today() is the whole decision as data; run() only claims, notifies and
records. The web workbench (web.py) shows the same plan on its today card, so
what you see there is exactly what the scheduler would do.

    reading-coach-scheduler          one wake cycle (what launchd runs)

notify and clock are injectable so the offline tests never touch the system.
"""
from __future__ import annotations

# - [INPUT]: 依赖 config 与 book.Library、ledger.Ledger、tracelog.Trace；macOS 通知依赖 osascript / terminal-notifier。
# - [OUTPUT]: 对外提供 plan_today、run、push_history、add_feedback、undo_feedback、ensure_schema、main、entry、FEEDBACK。
# - [POS]: 每日复习推送与反馈台账。跑完即退不常驻；账本即信箱，选概念走规则不调模型；web 复用它的查询与反馈接口，两者共写同一个 coach.db。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .book import Library
from .ledger import Ledger
from .tracelog import Trace

PORT = config.port()
DAY = 86400
IGNORE_LIMIT = 3           # unanswered pushes of one concept before backing off
MAX_WAIT_DAYS = 7          # backoff ceiling: a muted-by-silence concept still surfaces weekly
FEEDBACK = {"useful": "有用", "useless": "没用", "mute": "别再提这个", "not_today": "今天先不"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pushes (pushed_on TEXT PRIMARY KEY, payload TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, target TEXT, concept_id INTEGER,
  value TEXT, ts REAL, undone_at REAL);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(pushes)")}
    if "concept_id" not in cols:                    # v2 rows carry only "book|concept" in payload
        db.execute("ALTER TABLE pushes ADD COLUMN concept_id INTEGER")
    db.commit()


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# ---- what has happened: pushes, answers, feedback ---------------------------

def _pushes_of(db, concept: dict, book: str) -> list[tuple[str, float]]:
    """(day, ts) of every push of this concept, newest first. v2 rows are matched by payload."""
    return db.execute(
        "SELECT pushed_on, ts FROM pushes WHERE concept_id=? OR (concept_id IS NULL AND payload=?) "
        "ORDER BY ts DESC", (concept["id"], f"{book}|{concept['name']}")).fetchall()


def _answered_since(db, concept_id: int, ts: float) -> bool:
    return bool(db.execute("SELECT 1 FROM answers WHERE concept_id=? AND created_at>=? LIMIT 1",
                           (concept_id, ts)).fetchone())


def ignored_streak(db, concept: dict, book: str) -> tuple[int, int, float | None]:
    """(unanswered pushes in a row, total pushes, last push ts) for one concept."""
    pushes = _pushes_of(db, concept, book)
    streak = 0
    for _, ts in pushes:
        if _answered_since(db, concept["id"], ts):
            break
        streak += 1
    return streak, len(pushes), (pushes[0][1] if pushes else None)


def backoff_days(streak: int) -> int:
    """Wait after the last push before pushing this concept again: 0 until the
    limit, then 2, 4, and at most MAX_WAIT_DAYS."""
    if streak < IGNORE_LIMIT:
        return 0
    return min(MAX_WAIT_DAYS, 2 ** (streak - IGNORE_LIMIT + 1))


def active_feedback(db, value: str | None = None) -> list[dict]:
    q = "SELECT id, target, concept_id, value, ts FROM feedback WHERE undone_at IS NULL"
    args: tuple = ()
    if value:
        q, args = q + " AND value=?", (value,)
    return [dict(zip(("id", "target", "concept_id", "value", "ts"), r)) for r in db.execute(q + " ORDER BY id", args)]


def add_feedback(db, target: str, value: str, concept_id: int | None = None, clock=time.time) -> int:
    if value not in FEEDBACK:
        raise ValueError(f"没有这种反馈：{value}")
    cur = db.execute("INSERT INTO feedback (target, concept_id, value, ts) VALUES (?,?,?,?)",
                     (target, concept_id, value, clock()))
    db.commit()
    return cur.lastrowid


def undo_feedback(db, feedback_id: int, clock=time.time) -> bool:
    """Feedback is never deleted, only marked undone, so the history stays auditable."""
    cur = db.execute("UPDATE feedback SET undone_at=? WHERE id=? AND undone_at IS NULL", (clock(), feedback_id))
    db.commit()
    return cur.rowcount == 1


# ---- the decision -------------------------------------------------------------

def plan_today(db, clock=time.time) -> dict:
    """What the scheduler would do right now, with every reason spelled out.

    status: push (choice is set) / already (today's slot is used) /
            silent (nothing due, or every due concept is held back)
    focus:  the concept the today card shows; it is set even when silent,
            because backoff limits pushes, never your own practice."""
    ensure_schema(db)
    library, ledger = Library(db), Ledger(db, clock=clock)
    now = clock()
    today = _day(now)
    due = ledger.due()
    plan = {"status": "silent", "why": "", "choice": None, "focus": None, "held": [], "due": len(due),
            "day": today}
    if not due:
        plan["why"] = "没有到期的概念，今天不打扰"
        return plan

    muted = {f["concept_id"] for f in active_feedback(db, "mute")}
    not_today = any(_day(f["ts"]) == today for f in active_feedback(db, "not_today"))
    candidates = []
    for row in due:
        book = library.title(row["book_id"])
        streak, total, last = ignored_streak(db, row, book)
        item = {"concept_id": row["id"], "book_id": row["book_id"], "book": book, "concept": row["name"],
                "anchor": row["anchor"], "status": row["status"],
                "overdue_days": int((now - row["due_at"]) // DAY), "pushes": total, "ignored": streak}
        wait = backoff_days(streak)
        if row["id"] in muted:
            item["held"] = "你点过「别再提这个」"
        elif wait and last is not None and now - last < wait * DAY:
            item["held"] = f"连续 {streak} 次推送没回应，{wait} 天内不再推它"
        candidates.append(item)

    eligible = [c for c in candidates if "held" not in c]
    plan["held"] = [c for c in candidates if "held" in c]
    plan["focus"] = (eligible or candidates)[0]
    pushed = db.execute("SELECT payload FROM pushes WHERE pushed_on=?", (today,)).fetchone()
    if pushed:
        plan.update(status="already", why=f"今天已经推过一条（{pushed[0].replace('|', ' · ')}）")
    elif not_today:
        plan["why"] = "你说了今天先不，今天不再推送"
    elif not eligible:
        plan["why"] = "到期的概念都在暂停中：" + "；".join(f"「{c['concept']}」{c['held']}" for c in plan["held"])
    else:
        top = eligible[0]
        why = f"逾期最久（{top['overdue_days']} 天），共 {len(due)} 个到期概念"
        if plan["held"]:
            why = "；".join(f"「{c['concept']}」暂停（{c['held']}）" for c in plan["held"]) + f"，改推「{top['concept']}」"
        plan.update(status="push", why=why, choice={**top, "others": len(due) - 1})
    return plan


def message(choice: dict, url: str | None = None) -> tuple[str, str]:
    title = f"《{choice['book']}》该复习了"
    body = f"「{choice['concept']}」到复习窗口了"
    if choice["others"]:
        body += f"，另有 {choice['others']} 个概念到期"
    return title, body + ("。点开直接回一题" if url else "。打开 reading-coach 回一题就续上")


# ---- the notification ---------------------------------------------------------

def notifier() -> str | None:
    """terminal-notifier can open a URL on click; osascript cannot."""
    for path in (os.environ.get("READING_COACH_NOTIFIER"), shutil.which("terminal-notifier"),
                 str(Path.home() / ".local/opt/terminal-notifier.app/Contents/MacOS/terminal-notifier")):
        if path and Path(path).exists():
            return path
    return None


def notify_macos(title: str, body: str, url: str | None = None) -> None:
    """One native notification. Raises on failure so the caller can log it."""
    tn = notifier()
    if tn:
        cmd = [tn, "-title", title, "-message", body, "-sound", "Glass", "-group", "reading-coach"]
        subprocess.run(cmd + (["-open", url] if url else []), check=True, capture_output=True)
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    text = body + (f"（{url}）" if url else "")      # no click-through without terminal-notifier
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'],
                   check=True, capture_output=True)


def run(db, notify=notify_macos, clock=time.time, url: str | None = None) -> str:
    """One wake cycle. Returns silent / skipped / pushed / failed for the log."""
    plan = plan_today(db, clock=clock)
    trace = Trace(db, "sched-" + time.strftime("%Y%m%d", time.localtime(clock())))
    held = [{"concept": c["concept"], "why": c["held"]} for c in plan["held"]]
    if plan["status"] == "already":
        trace.emit("decide", what="skip-push", why=plan["why"])
        return "skipped"
    if plan["status"] == "silent":
        trace.emit("decide", what="silent", why=plan["why"], held=held)
        return "silent"

    choice, now = plan["choice"], clock()
    claim = db.execute("INSERT OR IGNORE INTO pushes (pushed_on, payload, ts, concept_id) VALUES (?,?,?,?)",
                       (plan["day"], f"{choice['book']}|{choice['concept']}", now, choice["concept_id"]))
    db.commit()
    if claim.rowcount == 0:                         # a concurrent run claimed today first
        trace.emit("decide", what="skip-push", why="另一次运行先推了今天这条")
        return "skipped"
    link = url if url is not None else f"http://127.0.0.1:{PORT}/?push={plan['day']}"
    title, body = message(choice, link)
    try:
        notify(title, body, link)
    except Exception as e:
        db.execute("DELETE FROM pushes WHERE pushed_on=? AND ts=?", (plan["day"], now))   # release the claim
        db.commit()
        trace.emit("notify_failed", book=choice["book"], concept=choice["concept"], error=str(e))
        return f"failed: {e}"
    trace.emit("notify", book=choice["book"], concept=choice["concept"], others=choice["others"],
               why=plan["why"], ignored=choice["ignored"], held=held)
    return "pushed"


def push_history(db, limit: int = 30, clock=time.time) -> list[dict]:
    """Every push, newest first, with whether you answered that concept before
    the next push of it (an answer today must not mark sixteen old pushes as
    answered) and your feedback on it."""
    ensure_schema(db)
    fb: dict[str, list] = {}
    for f in active_feedback(db):
        fb.setdefault(f["target"], []).append({"id": f["id"], "value": f["value"], "label": FEEDBACK[f["value"]]})
    out, next_ts = [], {}                           # concept -> ts of the push after this one
    for day, payload, ts, cid in db.execute(
            "SELECT pushed_on, payload, ts, concept_id FROM pushes ORDER BY ts DESC LIMIT ?", (limit,)):
        book, _, name = (payload or "").partition("|")
        if cid is None:                             # v2 row: resolve the concept by book title + name
            row = db.execute("SELECT c.id FROM concepts c JOIN books b ON b.id=c.book_id "
                             "WHERE b.title=? AND c.name=?", (book, name)).fetchone()
            cid = row[0] if row else None
        until = next_ts.get(cid, clock() + DAY)
        answered = bool(cid and db.execute(
            "SELECT 1 FROM answers WHERE concept_id=? AND created_at>=? AND created_at<? LIMIT 1",
            (cid, ts, until)).fetchone())
        next_ts[cid] = ts
        out.append({"day": day, "time": time.strftime("%H:%M", time.localtime(ts)), "book": book,
                    "concept": name, "concept_id": cid, "answered": answered,
                    "feedback": fb.get(f"push:{day}", [])})
    return out


def main() -> int:
    config.load_env()
    config.setup_logging()
    home = config.data_home()
    config.init_db(home)
    db = config.connect(home)
    result = run(db)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {result}"
    print(line)
    with open(home / "scheduler.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0 if not result.startswith("failed") else 1


def entry() -> int:
    """console_scripts 入口。setuptools 负责 sys.exit。"""
    return main()


if __name__ == "__main__":
    sys.exit(main())
