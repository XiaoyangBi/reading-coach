"""Trace log: what the agent did and why, one row per event, in SQLite.
The local counterpart of the reference repo's section 20 telemetry. (Named
tracelog, not trace, so it never shadows the stdlib `trace` module.)

Three things are recorded, because they are what v3's eval will grade:
  tool    every tool call, with its input (which carries the model's `why`)
  decide  every mode / plan / follow-up / stop decision and its reason
  verdict every judge result

emit() never raises: a logging fault must not crash a coaching session.
BYOK means the key's shape is chosen by the user, so scrub() reads every
environment variable whose name looks like a credential, not a fixed list of
vendor variable names, and replaces those values before a row is written.
"""
from __future__ import annotations

# - [INPUT]: 依赖标准库 sqlite3 / re / json / os / time；无包内依赖。
# - [OUTPUT]: 对外提供 Trace、scrub、SECRET_ENV、SECRET_RE。
# - [POS]: 事件日志与脱敏。写 trace 表；按进程里全部凭证环境变量的实际值脱敏，覆盖任意形状的 BYOK 密钥，web 推流前再脱敏一次。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
import os
import re
import time

# 历史清单，保留供外部引用。scrub 不再依赖它，因为 BYOK 下变量名由使用者决定。
SECRET_ENV = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "WEREAD_API_KEY")
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|wrk-[A-Za-z0-9_\-]{6,})")
SECRET_NAME_TAGS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

SCHEMA = """CREATE TABLE IF NOT EXISTS trace (
  id INTEGER PRIMARY KEY, session TEXT, ts REAL, kind TEXT, payload TEXT)"""


def _secret_values() -> list[str]:
    """收集进程里所有看起来像凭证的环境变量值。只取值，不取名字。"""
    return [v for k, v in os.environ.items()
            if len(v) >= 8 and any(tag in k.upper() for tag in SECRET_NAME_TAGS)]


def scrub(text: str) -> str:
    for value in _secret_values():
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
