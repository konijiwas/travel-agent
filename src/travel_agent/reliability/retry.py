"""可靠性六件套 ③ 重试(指数退避 + jitter + 错误分类)。

一次失败不一定是真故障(网络抖动、服务瞬时过载很常见),
重试一次往往就成功。但重试有两条纪律:

1. **指数退避 + jitter**:第 n 次重试等待 base*2^(n-1) 秒(0.5→1→2),
   避免失败后立刻重打把故障源打得更死;再叠加随机抖动,
   防止多客户端"同一时刻齐射"(同步重试风暴)。
2. **错误分类,该重试才重试**:
   - 超时/连接/进程类错误 → 重试(瞬时故障);
   - 参数校验、鉴权、余额不足类错误(400/401/402/422)
     → 不重试,立即抛出 —— 重试一百次也是同样的错,
     立即暴露反而让用户/评测最快发现问题。

Retry-After:服务端返回 429(限流)时常带 Retry-After 头,
指明"请 X 秒后再来" —— 尊重它,优先用它,不再自己猜。
"""

import asyncio
import random
from typing import Any, Awaitable, Callable

from travel_agent.config import settings

# HTTP 语义:这些状态码重试无意义(参数错/没权限/没余额/校验不过)
NO_RETRY_STATUS = {400, 401, 402, 422}
# 这些状态码是瞬时/服务端故障,可重试
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def extract_retry_after(exc: BaseException) -> float | None:
    """从异常上取服务端建议的重试间隔(秒);没有则 None。"""
    value = getattr(exc, "retry_after", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def default_is_retryable(exc: BaseException) -> bool:
    """错误分类:瞬时故障可重试,业务/参数错误不可重试。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True                       # 超时/连接断/子进程退出:瞬时类
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status not in NO_RETRY_STATUS
    return False                          # 其余(ValueError 等):重试无意义


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """第 attempt 次尝试(≥2)前的等待秒数:指数退避 + jitter,封顶。

    retry_after 存在时直接采用(服务端明示,不叠加指数)。
    """
    if retry_after is not None:
        return min(retry_after, settings.retry_max_delay_s)
    delay = settings.retry_base_delay_s * (2 ** (attempt - 2))
    delay = min(delay, settings.retry_max_delay_s)
    jitter = random.uniform(0, delay * 0.5)   # 抖动幅度 = 等待的一半
    return delay + jitter


async def call_with_retry(
    name: str,
    fn: Callable[..., Awaitable[Any]],
    *args,
    max_attempts: int | None = None,
    is_retryable: Callable[[BaseException], bool] | None = None,
    **kwargs,
) -> Any:
    """重试执行协程 fn(*args, **kwargs):失败且可重试 → 退避等待后重来。

    返回 fn 的结果;重试耗尽或遇不可重试错误 → 原样抛出最后一次异常。
    总尝试次数 = max_attempts(含首次),默认 settings.tool_retry_max。
    """
    attempts = settings.tool_retry_max if max_attempts is None else max_attempts
    checker = is_retryable or default_is_retryable
    for attempt in range(1, attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise                            # 取消信号必须原样传递,不吞
        except Exception as exc:
            if attempt >= attempts or not checker(exc):
                raise                        # 用尽次数 / 不可重试:直接抛
            await asyncio.sleep(backoff_delay(attempt, extract_retry_after(exc)))
    raise AssertionError("不可达:循环必然在上方 return 或 raise")
