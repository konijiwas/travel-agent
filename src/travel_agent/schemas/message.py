"""消息模型:AgentMessage / 消息类型 / 流名称 / 幂等 key。

AgentMessage 是 Agent 间通信的唯一载体:任务派发、结果回传、事件通知
三类消息都装进这个统一信封,经消息队列(Redis Streams / 内存)传输。
"""

import hashlib
import json
from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """三类消息:任务派发 / 结果回传 / 事件通知。"""

    TASK_DISPATCH = "task_dispatch"   # Supervisor → Agent:派任务
    RESULT_RETURN = "result_return"   # Agent → Supervisor:交结果
    EVENT_NOTIFY = "event_notify"     # 事件广播:预算超支 / 天气变更


class StreamName(str, Enum):
    """三个消息流(Redis Streams 的 stream key,内存实现对应三个分桶)。"""

    TASKS = "tasks"
    RESULTS = "results"
    EVENTS = "events"


# 事件主题:events 流中 topic 的取值(触发重规划闭环)
EVENT_BUDGET_OVERRUN = "budget_overrun"
EVENT_WEATHER_CHANGED = "weather_changed"


class AgentMessage(BaseModel):
    """统一结构化消息(JSON 可序列化)。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: MessageType
    topic: str                 # 细分主题,如 "task.analyze" / "budget_overrun"
    from_agent: str            # "supervisor" | "analyst" | "budget" | "executor"
    to_agent: str
    payload: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)
    idempotency_key: str       # 幂等 key:重发/重启后同 key 只处理一次


def make_idempotency_key(topic: str, session_id: str, round_no: int,
                         payload: dict | None = None) -> str:
    """生成幂等 key:同一主题+会话+轮次+载荷 → 同一个 key。

    载荷先 md5 压缩(长 payload 不直接进 key),再整体 sha256。
    发布方重试/重启重发时 key 不变,消费方据此去重。
    """
    payload_hash = hashlib.md5(
        json.dumps(payload or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    raw = f"{topic}:{session_id}:{round_no}:{payload_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
