"""Agent loop: the core while-loop, carried from the reference repo's section 1.

One call of run_turn is one inner loop: call the model, and while it asks for
tools, run them, feed each result back as a `tool_result`, and call again.
Three seams are added, each a plain callable so test.py can fake it:

  before_call(messages)        context management (section 8) before each model call
  pre_tool / post_tool         hook points (section 4): narration, trace, gates
  should_stop()                a tool can end the session (end_session) without
                               waiting for the model to stop on its own

The loop speaks the Anthropic Messages format directly, and `model` is any
callable messages -> Message, so the live client and the scripted fake in
test.py are interchangeable.
"""
from __future__ import annotations

# - [INPUT]: 依赖 tools.run_tool。
# - [OUTPUT]: 对外提供 run_turn、final_text。
# - [POS]: Agent 内层循环。用 before_call / pre_tool / post_tool / should_stop 四个接缝把门禁留给 session，自己只负责工具调用与消息推进。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


from .tools import run_tool


def run_turn(messages, model, registry, *, before_call=None, pre_tool=None,
             post_tool=None, should_stop=None, max_steps=40):
    """Run one turn to completion over `messages` (mutated in place).

    Returns the model's final text. Stops when the model stops asking for tools,
    or right after a batch of tool results if should_stop() says the session is
    over (the stop decision itself was a tool call, so it is in the trace).
    """
    for _ in range(max_steps):                   # ponytail: max_steps is the no-infinite-loop backstop
        if before_call:
            before_call(messages)
        response = model(messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":   # the model finished on its own
            return final_text(response)

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool = registry.get(block.name)
            if pre_tool:
                pre_tool(block.name, block.input, tool)
            out = run_tool(tool, block.input, block.name)
            if post_tool:
                post_tool(block.name, block.input, out)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})

        if should_stop and should_stop():         # end_session ran in this batch
            return final_text(response)

    raise RuntimeError("hit max_steps without end_turn")


def final_text(response):
    """The model's last words: concatenate its text blocks."""
    return "".join(b.text for b in response.content if b.type == "text")
