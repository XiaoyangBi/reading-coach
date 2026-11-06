"""Coach: the system prompt and the tool set the coaching agent works with.

The coach owns the decisions (which concept, which tier, follow up or stop);
the harness owns the edges that must not depend on the model's judgment
(section 22's "control flow moves to code" in miniature):

  ask_user refuses until set_mode and propose_plan ran   the user sees the plan first
  ask_user refuses past the tier's question budget       fatigue is bounded by code
  ask_user refuses a non-Feynman question after 2 fails  the retell rule is not optional
  set_mode refuses "deep" unless the user said 深挖       the user opens deep mode, never the model

Every tool carries a `narrate` line, so the user reads what the agent is
doing in plain Chinese instead of tool_use blocks (UX gate item 2). Tools take
a `why` argument; it lands in the trace with every call.
"""
from __future__ import annotations

# - [INPUT]: 依赖 book.anchor_label 与 tools.Registry / Tool。
# - [OUTPUT]: 对外提供 SYSTEM、WHY、build_registry、opening。
# - [POS]: 教练的 prompt、工具 schema 与代码边。把计划先行、题量预算、费曼规则、深挖授权写成门禁，让规则由代码裁决而不是靠模型自觉。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


from .book import anchor_label
from .tools import Registry, Tool

SYSTEM = """你是一位阅读陪练教练。书是用户自己选的，理解要用户自己完成，你只负责提问、追问和决定什么时候收。

工作顺序：
1. 先用工具看清楚情况：目录、账本（哪些到期、哪些卡壳）、用户的划线、相关原文
2. set_mode 选档位并说明理由，propose_plan 列出这次要问的概念和原因
3. ask_user 一次问一题；根据回答决定追问、换概念还是 end_session

出题规则：
- 每题必须挂原文锚点（章.段，例如 3.12），锚点要用 read_chapter 或 search_book 亲眼看过
- 优先问用户没有划线、但原文里是核心论点的地方；用户划过线的地方说明他已经注意到了
- 如果输入说明本书内容来自用户自己的划线（书架模式）：没有「没划线的部分」，每个段落都是用户划过或写过的句子。围绕划线本身出题：请他解释这句为什么成立、怎么用、和另一句划线什么关系
- 问理解、应用、对比、举反例，不问人名、年份、页码这类记忆细节
- 题干不超过 60 个字，一题只问一件事
- 账本里到期的概念优先复习，intent 写 review

档位规则：
- 默认闪卡档（flash），通常问一题就收
- 用户回答流畅、主动展开，可以升到陪练档（drill），理由写清楚
- 深挖档（deep）只在用户说了「深挖」之后才能开
- 回答变短、敷衍、连续跳过，说明累了，降档或直接 end_session

收束规则：
- 什么时候结束由你判断，用 end_session 结束，reason 写真实理由
- ask_user 返回 feynman_required=true 时，下一题必须 intent=feynman：请用户用自己的话把这个概念讲给一个外行听
- 返回 user_signal=stop 时立即 end_session；user_signal=deep 时先 set_mode deep 再继续

边界：
- 不替用户总结全书，不给标准答案式的长篇讲解，判分和反馈由裁判负责
- 每个工具的 why 写一句真实理由，它会进入日志
- 全程用中文"""

WHY = {"why": {"type": "string", "description": "一句话：为什么现在调用这个工具"}}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {**props, **WHY}, "required": required + ["why"]}


def build_registry(session) -> Registry:
    """The coach's tools, each bound to one Session handler."""
    reg = Registry()
    reg.register(Tool(
        "list_chapters", session.t_list_chapters, "列出这本书的章节、每章段落数。",
        _schema({}, []), narrate=lambda a: f"在看《{session.title}》的目录", is_read_only=True))
    reg.register(Tool(
        "read_chapter", session.t_read_chapter, "读某一章的原文段落，每段前面带锚点 [章.段]。一次最多约 4000 字。",
        _schema({"chapter": {"type": "integer"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["chapter"]),
        narrate=lambda a: f"在读第{a['chapter']}章" + (f"第{a.get('start', 1)}-{a['end']}段" if a.get("end") else ""),
        is_read_only=True))
    reg.register(Tool(
        "search_book", session.t_search_book, "在全书原文里按关键词找段落，返回锚点和片段。",
        _schema({"query": {"type": "string"}}, ["query"]),
        narrate=lambda a: f"在书里找「{a['query']}」", is_read_only=True))
    reg.register(Tool(
        "get_highlights", session.t_get_highlights, "查用户在这本书上的划线（微信读书或本地导出），带对应锚点。",
        _schema({}, []), narrate=lambda a: "在查你的划线", is_read_only=True))
    reg.register(Tool(
        "get_ledger", session.t_get_ledger, "查掌握度账本：每个概念的状态（学习中/卡壳/掌握）、锚点、是否到期复习。",
        _schema({}, []), narrate=lambda a: "在翻你的掌握度账本", is_read_only=True))
    reg.register(Tool(
        "set_mode", session.t_set_mode, "设定档位：flash 闪卡 / drill 陪练 / deep 深挖。切换时题量预算重新计算。",
        {"type": "object", "properties": {"mode": {"type": "string", "enum": ["flash", "drill", "deep"]},
                                          "reason": {"type": "string"}}, "required": ["mode", "reason"]},
        narrate=lambda a: ""))
    reg.register(Tool(
        "propose_plan", session.t_propose_plan, "在提问前把这次要问的概念列给用户看。",
        {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "concept": {"type": "string"}, "anchor": {"type": "string"}, "why": {"type": "string"}},
                "required": ["concept", "anchor", "why"]}},
            "reason": {"type": "string"}}, "required": ["items", "reason"]},
        narrate=lambda a: ""))
    reg.register(Tool(
        "ask_user", session.t_ask_user,
        "向用户提一道题并等待回答，回答由独立裁判判分。返回用户回答、等级、缺口、是否需要费曼复述、用户信号。",
        _schema({"concept": {"type": "string", "description": "概念名，账本按它记录"},
                 "anchor": {"type": "string", "description": "原文锚点，如 3.12 或 3.12-3.15"},
                 "question": {"type": "string"},
                 "intent": {"type": "string", "enum": ["new", "follow_up", "review", "feynman"]}},
                ["concept", "anchor", "question", "intent"]),
        narrate=lambda a: ""))
    reg.register(Tool(
        "end_session", session.t_end_session, "结束这次陪练。",
        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        narrate=lambda a: ""))
    return reg


def opening(title: str, chapters: int, summary: dict, request: str, content_note: str = "") -> str:
    """The first user message: the situation in one paragraph, then the request."""
    note = f"内容说明：{content_note}\n" if content_note else ""
    return (f"书：《{title}》，共 {chapters} 章。{note}账本：学习中 {summary['学习中']}，卡壳 {summary['卡壳']}，"
            f"掌握 {summary['掌握']}，到期待复习 {summary['到期']}。\n用户：{request}")
