"""Model adapters: any OpenAI-compatible provider (DeepSeek, Qwen, ...) wrapped
to speak the Anthropic Messages shape the loop expects.

The loop, gates, and tests never change: `make_deepseek` returns callables
messages -> Message with the same content-block / stop_reason contract as the
Anthropic SDK response. The translation is two pure functions, so test.py can
verify the mapping offline without any network or key.
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库；惰性依赖 openai SDK 与 recovery.with_retry。
# - [OUTPUT]: 对外提供 to_openai_messages、to_openai_tools、from_openai_response、make_deepseek。
# - [POS]: OpenAI 兼容适配层。翻译函数是纯函数，离线可测；cli 按 key 决定是否启用它，loop 与门禁一行不改。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
import os
from types import SimpleNamespace as NS


# ---------- pure translation, offline-testable ----------

def _field(block, name, default=None):
    """Content blocks arrive as dicts (tests, scripted fakes) or as objects
    (SDK responses, our own from_openai_response). Read either."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def to_openai_messages(messages):
    """Anthropic-shaped messages -> OpenAI chat messages."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(_field(b, "text", "") for b in content if _field(b, "type") == "text")
            calls = [{"id": _field(b, "id"), "type": "function",
                      "function": {"name": _field(b, "name"),
                                   "arguments": json.dumps(_field(b, "input", {}), ensure_ascii=False)}}
                     for b in content if _field(b, "type") == "tool_use"]
            out.append({"role": "assistant", "content": text or None,
                        **({"tool_calls": calls} if calls else {})})
        else:                                        # user turn carrying tool results
            for b in content:
                kind = _field(b, "type")
                if kind == "tool_result":
                    out.append({"role": "tool", "tool_call_id": _field(b, "tool_use_id"),
                                "content": _field(b, "content", "")})
                elif kind == "text":
                    out.append({"role": "user", "content": _field(b, "text", "")})
    return out


def to_openai_tools(schemas):
    """Anthropic tool schemas -> OpenAI function tools."""
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in schemas]


def from_openai_response(resp):
    """OpenAI ChatCompletion -> Anthropic-shaped Message."""
    msg = resp.choices[0].message
    content = []
    if msg.content:
        content.append(NS(type="text", text=msg.content))
    for c in msg.tool_calls or []:
        try:
            args = json.loads(c.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_raw": c.function.arguments}    # let the tool's schema check complain in words
        content.append(NS(type="tool_use", id=c.id, name=c.function.name, input=args))
    if not content:
        content.append(NS(type="text", text=""))
    stop = "tool_use" if resp.choices[0].finish_reason == "tool_calls" else "end_turn"
    return NS(content=content, stop_reason=stop)


# ---------- live factory ----------

def make_deepseek(coach_system, judge_system, verdict_tool, registry_schemas):
    """Build live coach/judge callables on DeepSeek's OpenAI-compatible API."""
    from openai import OpenAI
    from .recovery import with_retry
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    def call(system, messages, schemas, force_tool=None, max_tokens=2048):
        kwargs = {}
        if schemas:
            kwargs["tools"] = to_openai_tools(schemas)
        if force_tool:
            # the judge must answer through its verdict tool. Thinking-mode models
            # (deepseek-flash etc.) reject a forced tool_choice, so thinking is
            # turned off for this one call; judging is extraction, not reasoning.
            kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return with_retry(lambda: from_openai_response(client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}] + to_openai_messages(messages),
            max_tokens=max_tokens, **kwargs)))

    def coach_model(messages):
        return call(coach_system, messages, registry_schemas, max_tokens=4096)  # thinking eats tokens

    def judge_model(messages):
        return call(judge_system, messages, [verdict_tool], force_tool="submit_verdict", max_tokens=1024)

    return coach_model, judge_model


__all__ = ["to_openai_messages", "to_openai_tools", "from_openai_response", "make_deepseek"]
