"""可靠性六件套 ⑥ 可观测性(Tracer 打点器)。

全链路打点:llm / tool / node / queue / gate / cli 六个环节,
每个环节开始到结束记一条 TraceEvent(耗时/状态/出入参摘要),
写入 data/traces/session_{trace_id}.jsonl,并镜像进 SQLite traces 表。

用法(同步与异步通用,with / async with 皆可):
    with tracer.span("tool", "weather.get_forecast",
                     input={"city": "杭州"}) as span:
        result = await tool_call()          # 正常结束 → 记 ok
        span.set_output({"days": 3})        # 可选:补输出摘要
    # 块内抛异常 → 自动按异常类型记 timeout/open/error 后原样再抛

trace_id = 会话 id:同一会话的所有事件串成一条轨迹,
`replay --trace-id` 按它回放。

当前 Tracer 用 ContextVar 传递(与 cost_guard 同套路):
轨迹开始 set_tracer() 换新实例,各处 get_tracer() 拿到自己的,
多轨迹并发互不串扰。
"""

import contextvars
import time
from typing import Any

from travel_agent.config import settings
from travel_agent.memory import jsonl_writer
from travel_agent.memory.store import get_store
from travel_agent.reliability.circuit_breaker import CircuitOpenError
from travel_agent.reliability.timeout import ToolTimeoutError
from travel_agent.schemas.trace import TraceEvent, new_trace_id


class Span:
    """一个已开始的 span 句柄:块结束时落盘,支持同步/异步 with。"""

    def __init__(self, tracer: "Tracer", event: TraceEvent, parent: str | None):
        self._tracer = tracer
        self._event = event
        self._start = time.perf_counter()   # 单调时钟:计时不受系统改时间影响
        self._output: dict[str, Any] | None = None
        self._status_override: str | None = None

    @property
    def span_id(self) -> str:
        return self._event.span_id

    def set_output(self, output: dict[str, Any]) -> None:
        """块结束前补充输出摘要(可选)。"""
        self._output = output

    def mark_retry(self) -> None:
        """标记本次执行发生过重试(状态记 retry)。"""
        self._status_override = "retry"

    def _finish(self, exc: BaseException | None) -> None:
        event = self._event
        event.duration_ms = (time.perf_counter() - self._start) * 1000
        if exc is None:
            event.status = self._status_override or "ok"
        else:
            # 异常按类型映射状态:超时/熔断是"预期内故障",有专属状态便于统计
            if isinstance(exc, ToolTimeoutError):
                event.status = "timeout"
            elif isinstance(exc, CircuitOpenError):
                event.status = "open"
            else:
                event.status = "error"
            event.error = f"{type(exc).__name__}: {exc}"
        event.output = self._output
        self._tracer._write(event)
        self._tracer._pop(self)

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._finish(exc)
        return False                       # 不吞异常:原样向外传播

    async def __aenter__(self) -> "Span":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._finish(exc)
        return False


class Tracer:
    """打点器:span 开始/落盘、父子链、双写 JSONL+SQLite。"""

    def __init__(self, trace_id: str | None = None, write_db: bool = True):
        self.trace_id = trace_id or new_trace_id()
        self.write_db = write_db
        self._parent_stack: list[str] = []  # 栈顶 = 当前父 span(支持嵌套)

    # ── span 生命周期 ──

    def span(self, kind, name: str, input: dict[str, Any] | None = None,
             extra: dict[str, Any] | None = None) -> Span:
        """开始一个 span:返回句柄,用 with 包裹;父 = 当前栈顶。"""
        parent = self._parent_stack[-1] if self._parent_stack else None
        event = TraceEvent(
            trace_id=self.trace_id, kind=kind, name=name,
            parent_span_id=parent, input=input, extra=extra,
        )
        self._parent_stack.append(event.span_id)
        return Span(self, event, parent)

    def event(self, kind, name: str, input: dict[str, Any] | None = None,
              extra: dict[str, Any] | None = None,
              status: str = "ok") -> None:
        """一次性事件(不嵌套):队列收发、质量门结论等,立即落盘。"""
        event = TraceEvent(trace_id=self.trace_id, kind=kind, name=name,
                           input=input, extra=extra, status=status)
        self._write(event)

    # ── 内部 ──

    def _write(self, event: TraceEvent) -> None:
        jsonl_writer.record_event(event)                    # 回放用副本
        if self.write_db:
            try:
                get_store().save_trace(event)               # 查询用主库
            except Exception:                               # DB 故障不影响主流程
                pass

    def _pop(self, span: Span) -> None:
        if self._parent_stack and self._parent_stack[-1] == span.span_id:
            self._parent_stack.pop()


# ── 当前轨迹 Tracer(ContextVar,与 cost_guard 同套路)──

_current_tracer: contextvars.ContextVar[Tracer] = contextvars.ContextVar(
    "current_tracer", default=Tracer(write_db=False)   # 默认不落库,轨迹开始时显式换
)


def set_tracer(tracer: Tracer) -> None:
    _current_tracer.set(tracer)


def get_tracer() -> Tracer:
    return _current_tracer.get()
