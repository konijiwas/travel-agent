"""消息队列抽象:MessageQueue 协议 + 队列异常。

业务代码只依赖本文件定义的协议(AGENTS.md 规则 3):
Redis Streams 与内存两个实现可无缝互换,Redis 不可用时自动降级。
"""

from typing import Protocol

from travel_agent.schemas.message import AgentMessage, StreamName


class QueueUnavailableError(Exception):
    """队列后端不可用(如 Redis 连接失败)。"""


class MessageQueue(Protocol):
    """队列统一接口(结构协议:任何类具备这些方法签名即满足,无需继承)。

    consume 内部完成幂等去重:同一 idempotency_key 的消息只被返回一次。
    """

    def publish(self, msg: AgentMessage, stream: str = StreamName.TASKS.value) -> str:
        """发布一条消息,返回队列内的消息 id。"""
        ...

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[tuple[str, AgentMessage]]:
        """消费消息,返回 [(queue_msg_id, AgentMessage)];等待超时返回空列表。"""
        ...

    def ack(self, stream: str, group: str, msg_id: str) -> None:
        """确认消息已处理(Redis:XACK 需要组名;内存实现:no-op)。"""
        ...

    def healthcheck(self) -> bool:
        """探测队列后端是否可用。"""
        ...

    def close(self) -> None:
        """释放连接资源。"""
        ...
