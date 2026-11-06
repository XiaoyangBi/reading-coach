"""Model adapters: any OpenAI-compatible provider supplied by the user (BYOK),
wrapped to speak the Anthropic Messages shape the loop expects.

The loop, gates, and tests never change: `make_openai_compatible` returns
callables messages -> Message with the same content-block / stop_reason contract
as the Anthropic SDK response. The translation is two pure functions, so test.py
can verify the mapping offline without any network or key. The live factory
receives a credentials.Provider; it never reads environment variables.
"""
from __future__ import annotations

# - [INPUT]: 依赖标准库；接收调用方传入的 BYOK 凭证对象；惰性依赖 openai SDK 与 recovery.with_retry。
# - [OUTPUT]: 对外提供 to_openai_messages、to_openai_tools、from_openai_response、make_openai_compatible。
# - [POS]: OpenAI 兼容适配层。翻译函数是纯函数，离线可测；厂商私有参数由凭证对象的能力位决定，不写死任何厂商名。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
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

def make_openai_compatible(provider, coach_system, judge_system, verdict_tool, registry_schemas):
    """按 BYOK 凭证构建 live 教练/裁判。凭证来自 credentials.Provider，不读环境变量。"""
    from openai import OpenAI
    from .recovery import with_retry
    client = OpenAI(api_key=provider.api_key, base_url=provider.base_url)
    model = provider.model

    def call(system, messages, schemas, force_tool=None, max_tokens=2048):
        kwargs = {}
        if schemas:
            kwargs["tools"] = to_openai_tools(schemas)
        if force_tool:
            # 裁判必须经 verdict 工具作答。部分思考型端点拒绝被强制的 tool_choice，
            # 需要额外关闭思考；该私有参数只在凭证声明支持时下发，BYOK 端点默认不发。
            kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
            if provider.supports_thinking_toggle:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return with_retry(lambda: from_openai_response(client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}] + to_openai_messages(messages),
            max_tokens=max_tokens, **kwargs)))

    def coach_model(messages):
        return call(coach_system, messages, registry_schemas, max_tokens=4096)

    def judge_model(messages):
        return call(judge_system, messages, [verdict_tool], force_tool="submit_verdict", max_tokens=1024)

    return coach_model, judge_model


__all__ = ["to_openai_messages", "to_openai_tools", "from_openai_response", "make_openai_compatible"]
