"""Error recovery: wrap a model call so transient failures retry and only real
ones surface. A trimmed copy of the reference repo's section 11 (no fallback
model: one person, one model, a failed call is reported, not rerouted).

  transient (408 / 409 / 429 / 5xx) : exponential backoff + jitter, honor retry-after
  fatal     (other 4xx, unknown)    : raise at once, the session layer reports it

The session layer, not this file, decides what the user is told after a final
failure (which steps already happened, what can be retried).
"""
from __future__ import annotations

# - [INPUT]: 依赖 标准库 time；无包内依赖。
# - [OUTPUT]: 对外提供 with_retry、should_retry、retry_delay。
# - [POS]: 瞬时错误重试。只处理可重试状态码，不切换模型、不吞掉业务异常，因此失败状态能被 session 如实报告。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import time
from random import random

BASE_DELAY = 0.5
MAX_DELAY = 16.0
MAX_RETRIES = 3
RETRY_STATUS = {408, 409, 429}


def should_retry(status) -> bool:
    return status in RETRY_STATUS or (status is not None and 500 <= status < 600)


def retry_delay(attempt, retry_after=None) -> float:
    if retry_after is not None:
        return float(retry_after)
    base = min(BASE_DELAY * 2 ** (attempt - 1), MAX_DELAY)
    return base + base * 0.25 * random()


def with_retry(call, max_retries=MAX_RETRIES, sleep=time.sleep):
    """Call `call()`; retry transient API errors, raise everything else."""
    for attempt in range(1, max_retries + 2):
        try:
            return call()
        except Exception as e:
            status = getattr(e, "status_code", None)   # the anthropic SDK's attribute
            if attempt > max_retries or not should_retry(status):
                raise
            sleep(retry_delay(attempt, getattr(e, "retry_after", None)))
    raise RuntimeError("with_retry exhausted")         # unreachable
