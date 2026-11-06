"""Context management: keep a long coaching session inside the window.
Adapted from the reference repo's section 8 (budget -> micro), without the
lossy summary pass.

Book text is the bulk of the context. A coaching session reads a chapter,
asks several questions about it, then may move on, so old chapter text is the
first thing to go. Only results of BULKY tools are stubbed; ask_user results
(the actual Q&A) are never touched, because the coach needs the dialogue
history to decide whether to follow up or stop.
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库；不依赖包内模块。
# - [OUTPUT]: 对外提供 manage、size 与 BULKY / MAX_RESULT_CHARS / KEEP_RECENT / STUB 常量。
# - [POS]: 上下文预算层。只压缩书籍原文类工具结果，问答记录不动；用打桩替代有损摘要，session 在每轮调用模型前使用它。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


BULKY = {"read_chapter", "search_book", "get_highlights"}   # re-readable, safe to stub
MAX_RESULT_CHARS = 6000    # one result over this keeps only a head preview
KEEP_RECENT = 6            # tail messages always kept verbatim
STUB = "<已压缩：这段原文已移出上下文，需要时重新调用工具读取>"


def manage(messages):
    """Run the cheap passes every turn: budget huge results, stub old bulky ones."""
    bulky_ids = _bulky_ids(messages)
    _budget(messages)
    _micro(messages, bulky_ids, KEEP_RECENT)
    return messages


def _blocks(message):
    content = message.get("content")
    return content if isinstance(content, list) else []


def _get(block, key):
    """Blocks are dicts (ours) or SDK objects (the model's); read either."""
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _bulky_ids(messages):
    """tool_use ids of BULKY calls; a tool_result only carries the id, not the name."""
    return {_get(b, "id") for m in messages if m.get("role") == "assistant"
            for b in _blocks(m) if _get(b, "type") == "tool_use" and _get(b, "name") in BULKY}


def _results(message):
    if message.get("role") != "user":
        return []
    return [b for b in _blocks(message) if isinstance(b, dict) and b.get("type") == "tool_result"]


def _budget(messages):
    for m in messages:
        for block in _results(m):
            c = block.get("content")
            if isinstance(c, str) and len(c) > MAX_RESULT_CHARS:
                block["content"] = c[:MAX_RESULT_CHARS] + " ...<超长已截断，可缩小段落范围重读>"


def _micro(messages, bulky_ids, keep_recent):
    for m in messages[:max(0, len(messages) - keep_recent)]:
        for block in _results(m):
            if block.get("tool_use_id") in bulky_ids and block.get("content") != STUB:
                block["content"] = STUB


def size(messages) -> int:
    """Rough context size in characters (Chinese runs ~1.5 chars per token)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
            continue
        for b in c or []:
            total += len(str(_get(b, "content") or _get(b, "text") or _get(b, "input") or ""))
    return total
