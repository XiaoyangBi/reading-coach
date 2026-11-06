"""Highlights: what you marked while reading, from WeChat Reading or a local file.

Highlights are a preference signal, not the question source (spec 2): they tell
the coach what you already flagged as important, so it can aim at the core
ideas you did NOT mark. Each highlight is mapped back to a paragraph anchor by
substring search, so the coach sees "3.12 已划线" rather than loose quotes.

Two sources, tried in order:
  weread   the WeChat Reading agent gateway (needs WEREAD_API_KEY)
  local    data/highlights/<书名>.txt, one highlight per non-empty line

A missing key, a network error, or an API error never fails the session; the
result says which source was used and why the other was skipped, so the user
knows what the coach could and could not see (UX gate item 3).
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 urllib；可选 WEREAD_API_KEY；调用方注入 call 以便离线测试。
# - [OUTPUT]: 对外提供 weread_call、from_weread、from_local、get_highlights。
# - [POS]: 划线来源适配层。先试微信读书网关，再退到数据目录里的本地 txt；任何失败都返回来源标记而不中断陪练；shelf 复用同一个 weread_call。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
import os
import urllib.request
from pathlib import Path

GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.4"     # the weread skill's version; the gateway requires it on every call
MATCH_CHARS = 18            # leading characters of a highlight used to locate its paragraph


def weread_call(api_name: str, key: str, **params) -> dict:
    """POST one gateway call; business params flat at the top level (the skill's rule)."""
    body = json.dumps({"api_name": api_name, "skill_version": SKILL_VERSION, **params}).encode()
    req = urllib.request.Request(GATEWAY, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    if data.get("errcode"):
        raise RuntimeError(f"微信读书接口返回错误 {data.get('errcode')}：{data.get('errmsg', '')}")
    return data


def from_weread(title: str, call=weread_call) -> list[dict]:
    key = os.environ.get("WEREAD_API_KEY")
    if not key:
        raise RuntimeError("没有设置 WEREAD_API_KEY")
    found = call("/store/search", key, keyword=title, scope=10)
    books = [b["bookInfo"] for g in found.get("results", []) for b in g.get("books", []) if "bookInfo" in b]
    match = next((b for b in books if b.get("title") == title), books[0] if books else None)
    if match is None:
        raise RuntimeError(f"微信读书里没搜到《{title}》")
    marks = call("/book/bookmarklist", key, bookId=match["bookId"])
    chapters = {c.get("chapterUid"): c.get("title", "") for c in marks.get("chapters", [])}
    return [{"text": m["markText"], "chapter": chapters.get(m.get("chapterUid"), "")}
            for m in marks.get("updated", []) if m.get("markText")]


def from_local(title: str, root: Path) -> list[dict]:
    path = root / f"{title}.txt"
    if not path.exists():
        raise RuntimeError(f"本地没有 {path.name}")
    return [{"text": l.strip(), "chapter": ""} for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def get_highlights(library, book_id: int, root: Path, call=weread_call) -> dict:
    """{"source": weread|local|none, "skipped": [...], "items": [{"anchor", "text"}]}"""
    title, skipped, items, source = library.title(book_id), [], [], "none"
    for name, fetch in (("weread", lambda: from_weread(title, call)), ("local", lambda: from_local(title, root))):
        try:
            items, source = fetch(), name
            break
        except Exception as e:  # ponytail: a missing source is reported, never fatal
            skipped.append(f"{name}: {e}")
    for item in items:
        hits = library.search(book_id, item["text"][:MATCH_CHARS], limit=1)
        item["anchor"] = hits[0]["anchor"] if hits else ""
        item["text"] = item["text"][:120]
    return {"source": source, "skipped": skipped, "count": len(items), "items": items[:60]}
