"""事件发布 / 消费:预算超支、天气变化事件 → 重规划闭环的触发器。"""

from travel_agent.queue.base import MessageQueue
from travel_agent.queue.consumer import consume_batch
from travel_agent.schemas.message import (
    EVENT_BUDGET_OVERRUN,
    EVENT_WEATHER_CHANGED,
    AgentMessage,
    MessageType,
    StreamName,
    make_idempotency_key,
)

# 需要触发重规划的事件主题
REPLAN_EVENTS = {EVENT_BUDGET_OVERRUN, EVENT_WEATHER_CHANGED}


def publish_event(
    queue: MessageQueue,
    topic: str,
    from_agent: str,
    payload: dict,
    session_id: str,
    round_no: int,
) -> AgentMessage:
    """发布一条事件通知到 events 流(幂等 key 已生成),返回消息。"""
    msg = AgentMessage(
        type=MessageType.EVENT_NOTIFY,
        topic=topic,
        from_agent=from_agent,
        to_agent="supervisor",
        payload=payload,
        idempotency_key=make_idempotency_key(topic, session_id, round_no, payload),
    )
    queue.publish(msg, stream=StreamName.EVENTS.value)
    return msg


def collect_events(queue: MessageQueue) -> list[AgentMessage]:
    """消费 events 流当前可用的全部事件(幂等去重在队列层完成)。"""
    collected: list[AgentMessage] = []

    def _collect(msg: AgentMessage) -> bool:
        collected.append(msg)
        return True                 # 收集成功 → ack

    consume_batch(queue, StreamName.EVENTS.value, "supervisor", "supervisor",
                  handler=_collect, count=50, block_ms=200)
    return collected


def has_replan_event(events: list[AgentMessage]) -> bool:
    """是否存在需要触发重规划的事件(预算超支 / 天气变化)。"""
    return any(
        e.type == MessageType.EVENT_NOTIFY and e.topic in REPLAN_EVENTS
        for e in events
    )
