# -*- coding: utf-8 -*-
"""BYOK 凭证契约：模型能力由使用者自带，本项目不提供、不代管、不转发密钥。

这是全项目唯一读取模型密钥的位置。其它模块只接收 Provider 对象，不读环境变量。
凭证只存在于进程内存：不写盘、不进 trace、不进日志、不进页面。
没有凭证时软件仍可用——书架、账本、台账、离线检查都照常工作，只是不能开陪练。

支持两类端点：任意 OpenAI 兼容端点、Anthropic Messages API。
新增端点类型在此扩展，loop / session / 门禁一行不动。
"""
from __future__ import annotations

# - [INPUT]: 依赖标准库 os / dataclasses；不依赖包内模块与任何第三方 SDK。
# - [OUTPUT]: 对外提供 Provider、CredentialsError、resolve、weread_key、available、describe。
# - [POS]: BYOK 信任边界。唯一把环境变量翻译成模型与数据源凭证的位置；凭证以不可变对象向下游传递，api_key 不参与 repr。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md

import os
from dataclasses import dataclass, field

DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
PROVIDER_CHOICES = ("auto", "openai", "anthropic")


class CredentialsError(ValueError):
    """凭证配置不完整或互相矛盾。消息可以安全显示给用户，不含密钥值。"""


@dataclass(frozen=True)
class Provider:
    """一份自带凭证。api_key 不参与 repr，异常堆栈与日志都不会带出它。"""

    kind: str                          # openai | anthropic
    api_key: str = field(repr=False)
    base_url: str | None
    model: str
    label: str
    supports_thinking_toggle: bool = False   # 只有部分端点接受 extra_body.thinking

    def describe(self) -> dict:
        """给页面与 API 的非敏感描述。刻意不含 api_key。"""
        return {"configured": True, "byok": True, "kind": self.kind,
                "model": self.model, "label": self.label, "base_url": self.base_url}


def _first(*names: str) -> str | None:
    """按优先级取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve() -> Provider | None:
    """把环境变量解析成唯一凭证。没有任何密钥时返回 None。

    auto 的优先级：通用 BYOK 密钥 > 兼容别名 DEEPSEEK_API_KEY > ANTHROPIC_API_KEY。
    通用密钥必须同时给端点与模型名，否则拒绝启动，避免把密钥发往默认厂商。
    """
    choice = (os.environ.get("READING_COACH_PROVIDER") or "auto").strip().lower()
    if choice not in PROVIDER_CHOICES:
        raise CredentialsError(
            f"READING_COACH_PROVIDER 只接受 {'/'.join(PROVIDER_CHOICES)}，收到 {choice!r}")

    generic_key = _first("READING_COACH_API_KEY")
    legacy_key = _first("DEEPSEEK_API_KEY")
    anthropic_key = _first("ANTHROPIC_API_KEY")
    openai_key = generic_key or legacy_key

    kind = choice
    if kind == "auto":
        kind = "openai" if openai_key else "anthropic" if anthropic_key else "auto"
    if kind == "auto":
        return None

    if kind == "anthropic":
        if not anthropic_key:
            raise CredentialsError("指定了 anthropic，但没有设置 ANTHROPIC_API_KEY")
        return Provider(kind="anthropic", api_key=anthropic_key,
                        base_url=_first("ANTHROPIC_BASE_URL"),
                        model=_first("ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL,
                        label="Anthropic")

    if not openai_key:
        raise CredentialsError("指定了 openai，但没有设置 READING_COACH_API_KEY 或 DEEPSEEK_API_KEY")

    base_url = _first("READING_COACH_BASE_URL", "DEEPSEEK_BASE_URL")
    model = _first("READING_COACH_MODEL", "DEEPSEEK_MODEL")
    if generic_key and not (base_url and model):
        raise CredentialsError(
            "BYOK 通用密钥必须同时设置 READING_COACH_BASE_URL 与 READING_COACH_MODEL，"
            "否则会把你的密钥发往默认端点")
    if not generic_key:                            # 兼容别名保留原来的厂商默认值
        base_url = base_url or DEEPSEEK_DEFAULT_URL
        model = model or DEEPSEEK_DEFAULT_MODEL

    toggle = _first("READING_COACH_THINKING")
    is_deepseek = "deepseek" in (base_url or "").lower()
    supports_toggle = toggle == "1" if toggle in ("0", "1") else is_deepseek
    return Provider(kind="openai", api_key=openai_key, base_url=base_url, model=model,
                    label="DeepSeek" if is_deepseek else "OpenAI 兼容端点",
                    supports_thinking_toggle=supports_toggle)


def weread_key() -> str | None:
    """微信读书数据源凭证。它同样由使用者自带，缺失时调用方降级到本地划线。"""
    return _first("WEREAD_API_KEY")


def available() -> bool:
    """是否有可用凭证。配置错误不算可用，原因由 describe() 给出。"""
    try:
        return resolve() is not None
    except CredentialsError:
        return False


def describe() -> dict:
    """给页面与 API 的非敏感描述。无凭证或配置错误都不泄露密钥。"""
    try:
        provider = resolve()
    except CredentialsError as e:
        return {"configured": False, "byok": True, "error": str(e)}
    if provider is None:
        return {"configured": False, "byok": True,
                "error": "还没有配置模型密钥。在 .env 里设置 BYOK 凭证，参考 .env.example"}
    return provider.describe()
