"""消息消费工具:consume → 处理 → ack 的批量封装。"""

import logging

from travel_agent.queue.base import MessageQueue

logger = logging.getLogger(__name__)


def consume_batch(
    queue: MessageQueue,
    stream: str,
    group: str,
    consumer: str,
    handler,
    count: int = 10,
    block_ms: int = 1000,
) -> int:
    """消费一批消息,逐条交给 handler,处理成功的 ack;返回处理条数。

    handler(msg: AgentMessage) -> bool:
        返回 True 表示处理成功(将 ack);
        返回 False 表示处理失败(不 ack,由崩溃恢复 XAUTOCLAIM 稍后重派)。

    幂等去重由 queue.consume 内部完成,handler 永远只看到"新"消息。
    """
    handled = 0
    for msg_id, msg in queue.consume(stream, group, consumer,
                                     count=count, block_ms=block_ms):
        try:
            ok = bool(handler(msg))
        except Exception:
            logger.exception("处理消息失败(msg_id=%s, topic=%s),不 ack 等待重派",
                             msg_id, msg.topic)
            continue
        if ok:
            queue.ack(stream, group, msg_id)
        handled += 1
    return handled
