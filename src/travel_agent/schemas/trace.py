"""Trace 事件模型:全链路打点的数据单元(M7 可观测性)。

一个 TraceEvent 记录"一次操作"的完整留痕(业界叫 span):
谁(trace_id/span_id)、在做什么(kind/name)、用了多久、结果如何。
trace_id 相同的事件串起来就是一次会话的完整执行轨迹,
写入 data/traces/session_{trace_id}.jsonl,供 `replay` 回放定位。

字段说明:
- trace_id / span_id:追踪标识。trace_id = 会话 id,串起整条链路;
  span_id 每条事件唯一,parent_span_id 表示父子关系(如"预算节点"是"取价工具"的父)。
- kind:打点来源类别 —— llm(模型调用)/ tool(MCP 工具)/ node(状态机节点)/
  queue(消息收发)/ gate(质量门)/ cli(命令入口)。
- status:ok 正常 / error 失败 / timeout 超时 / retry 已重试 / open 熔断开闸跳过。
- input/output:出入参摘要(dict)。只记录必要字段,不落敏感信息;
  过长的内容由调用方截断后传入。
- attempts:本次操作尝试次数(重试后 >1),用于衡量重试成本。
- duration_ms:耗时毫秒,评测 M8 统计延迟的直接来源。
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

TraceKind = Literal["llm", "tool", "node", "queue", "gate", "cli"]
TraceStatus = Literal["ok", "error", "timeout", "retry", "open"]


def new_trace_id() -> str:
    """生成新的 trace_id(会话开始时生成一次,全链路共用)。"""
    return uuid.uuid4().hex[:16]


def new_span_id() -> str:
    """生成新的 span_id(每条事件一个,全局唯一)。"""
    return uuid.uuid4().hex[:8]


class TraceEvent(BaseModel):
    """一次打点的完整记录。"""

    trace_id: str
    span_id: str = Field(default_factory=new_span_id)
    parent_span_id: str | None = None
    kind: TraceKind
    name: str                      # 如 "weather.get_forecast"、"node.plan_budget"
    status: TraceStatus = "ok"
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )
    duration_ms: float = 0.0
    attempts: int = 1
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    extra: dict[str, Any] | None = None

    def to_jsonl_line(self) -> str:
        """序列化为一行 JSON(JSONL 文件格式:一行一个事件)。"""
        return self.model_dump_json()
