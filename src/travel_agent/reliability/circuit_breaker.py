"""可靠性六件套 ② 熔断器(Circuit Breaker)。

问题:某个数据源(如 flight-server)连续故障时,如果每次都照常
调用,每次都要等超时 + 重试才失败 —— 一次行程规划里多个节点
反复踩同一个坑,全流程被拖慢,还加重故障源的负担(雪崩)。

解法(电路开关三态):
- closed(闭合,正常):请求直接放行;
- open(开闸,故障):连续 failure_threshold 次失败后开闸,后续
  请求**立即失败**不真发(recovery_s 秒内),快速失败反而保护全链路;
- half_open(半开,试探):开闸时间到后,放行**一个**探测请求 ——
  成功则闭合恢复,失败则重新开闸计时。

按 server 粒度各建一个(weather/flight/hotel/llm):
单源故障只熔断它自己,其他数据源照常 —— 这就是
"单源故障不拖垮全局"。
"""

import threading
import time

from travel_agent.config import settings


class CircuitOpenError(Exception):
    """熔断开闸:请求被拒绝,未真正发出。"""

    def __init__(self, name: str, retry_after_s: float):
        self.name = name
        self.retry_after_s = retry_after_s
        super().__init__(f"熔断开闸:{name} 连续失败,约 {retry_after_s:.0f}s 后恢复")


class CircuitBreaker:
    """单数据源的熔断状态机(线程安全)。"""

    def __init__(self, name: str, failure_threshold: int | None = None,
                 recovery_s: float | None = None):
        self.name = name
        self.threshold = failure_threshold or settings.breaker_failure_threshold
        self.recovery = recovery_s if recovery_s is not None else settings.breaker_recovery_s
        self._failures = 0            # 连续失败计数(成功后清零)
        self._opened_at: float | None = None   # 开闸时刻(epoch 秒)
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """当前状态:closed / open / half_open(按时间实时推导,无需定时器)。"""
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.recovery:
            return "half_open"        # 恢复窗口已过:进入试探期
        return "open"

    def allow(self) -> bool:
        """调用前询问:本次请求是否放行。

        open → 拒绝;half_open → 只放行一个探测请求(放行后即视为
        重新开闸计时,探测结果由 record_success/failure 决定走向)。
        """
        with self._lock:
            st = self.state
            if st == "open":
                return False
            if st == "half_open":
                self._opened_at = time.monotonic()   # 探测期重新计时
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None    # 成功即恢复闭合

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()   # 达到阈值:开闸
