"""Tool runtime: the Tool contract and a registry. Carried from the reference
repo's section 2, with one addition: `narrate`.

A Tool carries the Anthropic input_schema it advertises plus the handler that
runs it; the Registry emits the tools list for the model. `narrate(input)` is
the one human sentence the CLI prints before the call runs, so the user reads
"在读第 3 章" instead of a raw tool_use block (UX gate item 2). Returning ""
means "say nothing" (ask_user prints its own question).

    reading-coach-test      (offline checks for every module)
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 json；无包内依赖。
# - [OUTPUT]: 对外提供 Tool、Registry、run_tool、NO_INPUT。
# - [POS]: 工具运行时。Registry 产出 schema，run_tool 统一把异常转成模型可读文本；narrate 让每次调用先说一句人话。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import json
from dataclasses import dataclass, field
from typing import Any, Callable

NO_INPUT = {"type": "object", "properties": {}}   # schema for a tool that takes no arguments


@dataclass
class Tool:
    name: str
    run: Callable[[dict], Any]
    description: str = ""
    input_schema: dict = field(default_factory=lambda: dict(NO_INPUT))
    narrate: Callable[[dict], str] | None = None   # human sentence shown before the call
    is_read_only: bool = False

    def say(self, tool_input: dict) -> str:
        """The narration line; a broken narrator never blocks the call."""
        if self.narrate is None:
            return f"正在执行 {self.name}"
        try:
            return self.narrate(tool_input or {})
        except Exception:  # ponytail: narration is cosmetic, the call must still run
            return f"正在执行 {self.name}"


class Registry:
    """Name to Tool. Adding a tool is registering one handler."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """The tools list advertised to the model (Anthropic format)."""
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in self._tools.values()]


def run_tool(tool: Tool | None, tool_input: dict, name: str = "") -> str:
    """Execute one tool, turning any failure into a string result (never raises)."""
    if tool is None:
        return f"error: no tool {name!r}"
    try:
        out = tool.run(tool_input or {})
    except Exception as e:  # ponytail: failure is a result fed back, not a crashed loop
        return f"error: {type(e).__name__}: {e}"
    if isinstance(out, (dict, list)):
        return json.dumps(out, ensure_ascii=False)   # structured results stay readable Chinese
    return str(out)
