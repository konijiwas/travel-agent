"""可靠性六件套统一入口(AGENTS.md 约定:工具调用必须经 wrap_tool_call)。

六件套:
① 超时(timeout)        ② 熔断(circuit_breaker)  ③ 重试(retry)
④ 权限分级(permissions) ⑤ 成本护栏(cost_guard)   ⑥ 可观测(observability)

包装顺序(由外向内):
    权限检查 → 熔断检查 → span 打点 → [每次尝试:超时] → 重试
- 权限不过(deny / confirm 未批准):直接抛,**不打点不计数** —— 这不是故障;
- 熔断开闸:记一条 open 事件后立即抛 CircuitOpenError,请求根本不发;
- span 内:重试循环,每次尝试都套超时(每次尝试独立限时);
  发生过重试则状态记 retry;成功/失败回报熔断器。

另提供 wrap_sync_call:给同步调用(LLM)用同一套 熔断+重试+打点
(LLM 无工具超时概念,超时由 ChatOpenAI 自身 timeout 管)。
"""

import threading
import time
from typing import Any, Awaitable, Callable

from travel_agent.config import settings
from travel_agent.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
)
from travel_agent.reliability.cost_guard import (
    CostGuard,
    CostLimitExceededError,
    estimate_tokens,
    get_guard,
    set_guard,
)
from travel_agent.reliability.observability import (
    Span,
    Tracer,
    get_tracer,
    set_tracer,
)
from travel_agent.reliability.permissions import (
    AUTO,
    CONFIRM,
    DENY,
    ConfirmationRequiredError,
    PermissionDeniedError,
    assert_allowed,
    classify_tool,
)
from travel_agent.reliability.retry import (
    backoff_delay,
    call_with_retry,
    default_is_retryable,
    extract_retry_after,
)
from travel_agent.reliability.timeout import ToolTimeoutError, await_with_timeout

__all__ = [
    "AUTO", "CONFIRM", "DENY",
    "CircuitBreaker", "CircuitOpenError",
    "ConfirmationRequiredError", "PermissionDeniedError",
    "CostGuard", "CostLimitExceededError", "estimate_tokens",
    "Span", "Tracer", "ToolTimeoutError",
    "assert_allowed", "await_with_timeout", "backoff_delay", "call_with_retry",
    "classify_tool", "default_is_retryable", "extract_retry_after",
    "get_breaker", "get_guard", "get_tracer", "reset_breakers",
    "set_guard", "set_tracer", "wrap_sync_call", "wrap_tool_call",
]


# ── 熔断器注册表:按粒度各一个(weather/flight/hotel/llm),单源故障互不影响 ──

_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    """取(或建)指定粒度的熔断器,进程内共享同一实例。"""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name)
        return _breakers[name]


def reset_breakers() -> None:
    """清空全部熔断器状态(测试/评测每个 case 干净环境时用)。"""
    with _breakers_lock:
        _breakers.clear()


# ── 工具调用包装(异步)──

async def wrap_tool_call(
    name: str,
    fn: Callable[[], Awaitable[Any]],
    *,
    tool_args: dict[str, Any] | None = None,
    breaker: str | None = None,
    pre_approved: bool = False,
    output: dict[str, Any] | None = None,
) -> Any:
    """以 权限→熔断→打点(超时+重试) 包装一次工具调用,返回工具结果。

    - fn:零参协程函数,执行"真正调用工具"的动作
    - tool_args:参数 dict(写 trace 与权限摘要用)
    - breaker:熔断粒度名(默认用工具名;同 server 的工具建议传 server 名)
    - pre_approved:confirm 类工具是否已经用户同意
    """
    tracer = get_tracer()

    # 1) 权限:deny / confirm 未批准 → 直接抛(不算故障,不打点)
    assert_allowed(name, tool_args, pre_approved=pre_approved)

    # 2) 熔断:开闸 → 记 open 事件,立即失败,请求不发
    brk = get_breaker(breaker or name)
    if not brk.allow():
        tracer.event("tool", name, input=tool_args,
                     extra={"breaker": brk.name}, status="open")
        raise CircuitOpenError(brk.name, brk.recovery)

    # 3) 打点 + 每次尝试独立超时 + 重试
    calls = [0]

    async def once() -> Any:
        calls[0] += 1
        return await await_with_timeout(name, fn)

    with tracer.span("tool", name, input=tool_args,
                     extra={"breaker": brk.name}) as span:
        try:
            result = await call_with_retry(name, once)
        except Exception:
            brk.record_failure()          # 失败(重试耗尽/不可重试)→ 记熔断一败
            raise                         # span 自动按异常类型记状态后原样抛出
        if calls[0] > 1:
            span.mark_retry()             # 发生过重试:状态记 retry
        if output is not None:
            span.set_output(output)
        brk.record_success()
        return result


# ── 同步调用包装(LLM 用)──

def wrap_sync_call(
    name: str,
    fn: Callable[..., Any],
    *args,
    breaker: str | None = None,
    input_summary: dict[str, Any] | None = None,
    max_attempts: int | None = None,
    **kwargs,
) -> Any:
    """以 熔断→打点→重试 包装一次同步调用(LLM):无权限/超时层。

    LLM 的"权限"= 提示词层规则,"超时"由 ChatOpenAI 的 timeout
    参数管理,这里只管 熔断 + 重试 + 打点。
    """
    tracer = get_tracer()
    brk = get_breaker(breaker or name)
    if not brk.allow():
        tracer.event("llm", name, input=input_summary,
                     extra={"breaker": brk.name}, status="open")
        raise CircuitOpenError(brk.name, brk.recovery)

    attempts = settings.tool_retry_max if max_attempts is None else max_attempts
    with tracer.span("llm", name, input=input_summary,
                     extra={"breaker": brk.name}) as span:
        for attempt in range(1, attempts + 1):
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if attempt >= attempts or not default_is_retryable(exc):
                    brk.record_failure()
                    raise
                time.sleep(backoff_delay(attempt, extract_retry_after(exc)))
                continue
            if attempt > 1:
                span.mark_retry()
            brk.record_success()
            return result
    raise AssertionError("不可达:循环内必然 return 或 raise")
