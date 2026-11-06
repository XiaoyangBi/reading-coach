"""Judge: grade one answer against the original text, in a fresh context.

A subagent in the section 6 sense: every call starts from an empty messages[]
holding only the question, the anchored paragraphs, and your answer. It never
sees the coach's plan, the dialogue so far, or your ledger, so it cannot be
talked into leniency by context (spec 4.2).

The verdict is forced through one tool, submit_verdict, so the answer shape is
checked by code, not parsed out of prose. A malformed verdict raises
JudgeError; the session reports it and offers a retry, and nothing is written.
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 json；无包内依赖。
# - [OUTPUT]: 对外提供 JudgeError、build_prompt、judge、SYSTEM、VERDICT_TOOL。
# - [POS]: 裁判子代理。每次构造全新 messages，用 tool_choice 强制结构化判分；教练看不到历史答案、历史等级与裁判依据。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


LEVELS = ("pass", "partial", "fail")

SYSTEM = """你是阅读理解的裁判。你只做一件事：对照原文，判断用户的回答是否真正理解了题目问的东西。

判分标准：
- pass：抓住了原文的核心意思，用自己的话说对了，允许不完整的细节
- partial：方向对，但漏了关键一环，或者有一处明显误解
- fail：答非所问、和原文矛盾，或者只是复述字面没有理解

规则：
- 只依据给你的原文判断，不用你自己对这本书的印象
- 不因为回答长就给高分，不因为回答短就给低分
- gap 写一句话：缺了什么或错在哪；pass 时写"无"
- feedback 写给用户看，一到两句大白话，先说对在哪，再说差在哪；不给标准答案全文，不夸张表扬
- 必须调用 submit_verdict 提交结果"""

VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "提交判分结果。",
    "input_schema": {
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": list(LEVELS)},
            "gap": {"type": "string", "description": "一句话：缺了什么或错在哪"},
            "feedback": {"type": "string", "description": "给用户看的一到两句反馈"},
            "rationale": {"type": "string", "description": "判分依据，只进账本，不给教练看"},
        },
        "required": ["level", "gap", "feedback", "rationale"],
    },
}


class JudgeError(Exception):
    pass


def build_prompt(question: str, source: str, answer: str) -> str:
    return f"【题目】\n{question}\n\n【原文】\n{source or '（没有找到对应原文）'}\n\n【用户的回答】\n{answer}"


def judge(model, question: str, source: str, answer: str) -> dict:
    """One fresh-context grading call. `model(messages)` must be configured with
    SYSTEM, [VERDICT_TOOL], and tool_choice forcing submit_verdict."""
    messages = [{"role": "user", "content": build_prompt(question, source, answer)}]   # fresh context
    response = model(messages)
    call = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_verdict"), None)
    if call is None:
        raise JudgeError("裁判没有给出结构化结果")
    verdict = dict(call.input)
    if verdict.get("level") not in LEVELS:
        raise JudgeError(f"裁判给了无效的等级：{verdict.get('level')!r}")
    return {k: str(verdict.get(k, "")) for k in ("level", "gap", "feedback", "rationale")}
