"""Trace log: what the agent did and why, one row per event, in SQLite.
The local counterpart of the reference repo's section 20 telemetry. (Named
tracelog, not trace, so it never shadows the stdlib `trace` module.)

Three things are recorded, because they are what v3's eval will grade:
  tool    every tool call, with its input (which carries the model's `why`)
  decide  every mode / plan / follow-up / stop decision and its reason
  verdict every judge result

emit() never raises: a logging fault must not crash a coaching session.
Secrets are scrubbed before a row is written: the values of the model and
weread keys in SECRET_ENV, and anything shaped like an API key, become "<redacted>".
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 sqlite3 / re / json / os；无包内依赖。
# - [OUTPUT]: 对外提供 Trace、scrub、SECRET_ENV、SECRET_RE。
# - [POS]: 事件日志与脱敏。写入 SQLite 的 trace 表；按环境变量值与密钥形状脱敏，web 推流前对文本再 scrub 一次。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
import os
import re
import time

SECRET_ENV = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "WEREAD_API_KEY")
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|wrk-[A-Za-z0-9_\-]{6,})")

SCHEMA = """CREATE TABLE IF NOT EXISTS trace (
  id INTEGER PRIMARY KEY, session TEXT, ts REAL, kind TEXT, payload TEXT)"""


def scrub(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            text = text.replace(value, "<redacted>")
    return SECRET_RE.sub("<redacted>", text)


class Trace:
    def __init__(self, db, session: str):
        self.db, self.session = db, session
        self.db.execute(SCHEMA)

    def emit(self, kind: str, **payload) -> None:
        try:
            body = scrub(json.dumps(payload, ensure_ascii=False, default=str))
            self.db.execute("INSERT INTO trace (session, ts, kind, payload) VALUES (?,?,?,?)",
                            (self.session, time.time(), kind, body))
            self.db.commit()
        except Exception:  # ponytail: tracing is best-effort, the session is not
            pass

    def rows(self, kind: str | None = None) -> list[dict]:
        q = "SELECT kind, payload FROM trace WHERE session=?"
        args = [self.session]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [{"kind": k, **json.loads(p)} for k, p in self.db.execute(q + " ORDER BY id", args)]
