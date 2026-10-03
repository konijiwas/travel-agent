"""队列工厂:按配置/探测结果创建队列实例(Redis 优先,auto 模式自动降级内存)。"""

import logging

from travel_agent.config import settings
from travel_agent.queue.base import MessageQueue, QueueUnavailableError
from travel_agent.queue.memory import InMemoryQueue
from travel_agent.queue.redis_streams import RedisStreamsQueue

logger = logging.getLogger(__name__)


def get_queue(backend: str | None = None) -> MessageQueue:
    """创建队列实例。

    backend 取值(默认取配置 TA_QUEUE_BACKEND):
    - "memory":直接内存队列(开发/测试)
    - "redis":强制 Redis,不可用则抛 QueueUnavailableError(需要可靠性的场景)
    - "auto":探测 Redis,可用用 Redis,不可用降级内存并告警(默认,业务无感知)
    """
    backend = (backend or settings.queue_backend).lower()
    if backend == "memory":
        return InMemoryQueue()

    q = RedisStreamsQueue(url=settings.redis_url)
    if backend == "redis":
        if not q.healthcheck():
            q.close()
            raise QueueUnavailableError("TA_QUEUE_BACKEND=redis 但 Redis 不可用")
        return q

    # auto:探测失败自动降级
    if q.healthcheck():
        return q
    q.close()
    logger.warning(
        "Redis 不可用(%s),降级为 InMemoryQueue:功能不变,仅丢失跨进程与持久化能力",
        settings.redis_url,
    )
    return InMemoryQueue()
