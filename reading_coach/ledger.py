"""Mastery ledger: one row per concept, every answer kept, reviews scheduled.

This is the memory layer (section 9) for the one thing a reading coach must
remember across runs: what you got right, where you got stuck, and when to ask
again. Two views exist on purpose:

  coach_view()  status, anchor, due date: enough to pick what to ask next
  answers       the judge's levels and rationales, never shown to the coach,
                so the coach cannot soften a question because it knows your
                record (the coach / judge split in the spec, 4.2)

Spacing: pass doubles-and-a-half the interval (1 -> 3 -> 8 -> 20 days), partial
resets it to 1 day, fail resets it and counts a fail streak. Two fails in a row
on one concept is the trigger for a Feynman retell.

record() returns a snapshot so the session can undo the write (UX gate item 4).
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 sqlite3 / time；无包内依赖。
# - [OUTPUT]: 对外提供 Ledger、next_state。
# - [POS]: 掌握度账本与间隔重复状态机。教练视图与裁判记录分离；scheduler 只读它挑选到期概念。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import math
import time

DAY = 86400
MASTERED_DAYS = 7          # an interval this long means the concept is "掌握"
FEYNMAN_STREAK = 2         # consecutive fails before a retell is required
STATUS = ("学习中", "卡壳", "掌握")

SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (id INTEGER PRIMARY KEY, book_id INTEGER, name TEXT, anchor TEXT,
  status TEXT DEFAULT '学习中', interval_days INTEGER DEFAULT 0, fail_streak INTEGER DEFAULT 0,
  reviews INTEGER DEFAULT 0, due_at REAL, updated_at REAL, UNIQUE(book_id, name));
CREATE TABLE IF NOT EXISTS answers (id INTEGER PRIMARY KEY, concept_id INTEGER, question TEXT,
  answer TEXT, level TEXT, gap TEXT, rationale TEXT, created_at REAL);
"""
COLS = ("id", "book_id", "name", "anchor", "status", "interval_days", "fail_streak",
        "reviews", "due_at", "updated_at")


def next_state(row: dict, level: str, now: float) -> dict:
    """Pure scheduling rule: the concept row after one verdict."""
    interval, streak = row["interval_days"], row["fail_streak"]
    if level == "pass":
        interval, streak = (1 if interval == 0 else math.ceil(interval * 2.5)), 0
        status = "掌握" if interval >= MASTERED_DAYS else "学习中"
    elif level == "partial":
        interval, status = 1, "学习中"
    else:                                           # fail, or anything unrecognized
        interval, streak, status = 1, streak + 1, "卡壳"
    return {**row, "status": status, "interval_days": interval, "fail_streak": streak,
            "reviews": row["reviews"] + 1, "due_at": now + interval * DAY, "updated_at": now}


class Ledger:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        self.db.executescript(SCHEMA)

    def concept(self, book_id: int, name: str, anchor: str = "") -> dict:
        """Get a concept by name, creating it on first ask; a new anchor overwrites an empty one."""
        row = self._row("SELECT * FROM concepts WHERE book_id=? AND name=?", (book_id, name))
        if row is None:
            self.db.execute("INSERT INTO concepts (book_id, name, anchor, updated_at) VALUES (?,?,?,?)",
                            (book_id, name, anchor, self.clock()))
            self.db.commit()
            return self._row("SELECT * FROM concepts WHERE book_id=? AND name=?", (book_id, name))
        if anchor and not row["anchor"]:
            self.db.execute("UPDATE concepts SET anchor=? WHERE id=?", (anchor, row["id"]))
            row["anchor"] = anchor
        return row

    def record(self, concept_id: int, question: str, answer: str, verdict: dict) -> dict:
        """Apply one verdict. Returns {"before": row, "after": row, "answer_id": id} for undo."""
        before = self._row("SELECT * FROM concepts WHERE id=?", (concept_id,))
        after = next_state(before, verdict.get("level", "fail"), self.clock())
        self.db.execute(
            "UPDATE concepts SET status=?, interval_days=?, fail_streak=?, reviews=?, due_at=?, updated_at=? "
            "WHERE id=?", (after["status"], after["interval_days"], after["fail_streak"], after["reviews"],
                           after["due_at"], after["updated_at"], concept_id))
        cur = self.db.execute(
            "INSERT INTO answers (concept_id, question, answer, level, gap, rationale, created_at) "
            "VALUES (?,?,?,?,?,?,?)", (concept_id, question, answer, verdict.get("level"),
                                       verdict.get("gap", ""), verdict.get("rationale", ""), self.clock()))
        self.db.commit()
        return {"before": before, "after": after, "answer_id": cur.lastrowid}

    def undo(self, snapshot: dict) -> None:
        """Put a concept back exactly as it was before record()."""
        b = snapshot["before"]
        self.db.execute(
            "UPDATE concepts SET status=?, interval_days=?, fail_streak=?, reviews=?, due_at=?, updated_at=? "
            "WHERE id=?", (b["status"], b["interval_days"], b["fail_streak"], b["reviews"],
                           b["due_at"], b["updated_at"], b["id"]))
        self.db.execute("DELETE FROM answers WHERE id=?", (snapshot["answer_id"],))
        self.db.commit()

    def needs_feynman(self, concept_id: int) -> bool:
        row = self._row("SELECT * FROM concepts WHERE id=?", (concept_id,))
        return bool(row) and row["fail_streak"] >= FEYNMAN_STREAK

    def coach_view(self, book_id: int) -> list[dict]:
        """What the coach may see: no levels, no rationales, no answer history."""
        now = self.clock()
        return [{"concept": r["name"], "anchor": r["anchor"], "status": r["status"],
                 "due": bool(r["due_at"] and r["due_at"] <= now)}
                for r in self._rows("SELECT * FROM concepts WHERE book_id=? ORDER BY id", (book_id,))]

    def due(self, book_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM concepts WHERE due_at IS NOT NULL AND due_at<=?", [self.clock()]
        if book_id is not None:
            q, args = q + " AND book_id=?", args + [book_id]
        return self._rows(q + " ORDER BY due_at", tuple(args))

    def upcoming(self, book_id: int, n: int = 3) -> list[dict]:
        """The next reviews not yet due, soonest first (for the end-of-session report)."""
        return self._rows("SELECT * FROM concepts WHERE book_id=? AND due_at>? ORDER BY due_at LIMIT ?",
                          (book_id, self.clock(), n))

    def summary(self, book_id: int) -> dict:
        counts = {s: 0 for s in STATUS}
        for (s, n) in self.db.execute("SELECT status, COUNT(*) FROM concepts WHERE book_id=? GROUP BY status",
                                      (book_id,)):
            counts[s] = n
        return {**counts, "到期": len(self.due(book_id))}

    def _rows(self, q, args) -> list[dict]:
        return [dict(zip(COLS, r)) for r in self.db.execute(q.replace("*", ", ".join(COLS), 1), args)]

    def _row(self, q, args) -> dict | None:
        rows = self._rows(q, args)
        return rows[0] if rows else None
