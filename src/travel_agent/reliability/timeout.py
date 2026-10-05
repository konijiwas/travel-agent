"""可靠性六件套 ① 超时控制。

对"可能无限挂起"的外部调用(MCP 工具、远端服务)加时间上限:
超过 settings.tool_timeout_s 秒未返回 → 取消调用并抛 ToolTimeoutError,
由上层(重试/熔断)决定下一步。没有这一层,单个工具卡死会拖垮
整次行程规划(所有下游节点都在等它)。

实现要点:
- 用 asyncio.wait_for 等待协程:超时会**取消**底层任务(不只是
  放弃等待),不残留半死的调用占资源、占子进程;
- 超时时间可全局配置(settings.tool_timeout_s,默认 5s),也可
  调用时单独指定 —— 快工具和慢工具可区别对待。
"""

import asyncio
from typing import Any, Awaitable, Callable

from travel_agent.config import settings


class ToolTimeoutError(TimeoutError):
    """工具调用超时(继承内置 TimeoutError,上层可用 except TimeoutError 统一捕获)。"""

    def __init__(self, name: str, timeout_s: float):
        self.name = name
        self.timeout_s = timeout_s
        super().__init__(f"工具 {name} 超过 {timeout_s}s 未返回")


async def await_with_timeout(
    name: str,
    fn: Callable[..., Awaitable[Any]],
    *args,
    timeout_s: float | None = None,
    **kwargs,
) -> Any:
    """在超时限制内等待协程 fn(*args, **kwargs) 完成并返回其结果。

    参数:
    - name:工具名(异常信息与 trace 里标识是谁超时了)
    - fn:要执行的协程函数(不用先 await,这里统一管理)
    - timeout_s:None 时用全局配置 settings.tool_timeout_s
    """
    limit = settings.tool_timeout_s if timeout_s is None else timeout_s
    try:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=limit)
    except TimeoutError:
        # wait_for 超时:底层任务已被取消;转成业务异常,附工具名便于定位
        raise ToolTimeoutError(name, limit) from None
