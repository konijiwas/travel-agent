"""内存队列实现:每流一个 deque + 锁 + 条件变量;幂等去重带 TTL。

进程内使用(开发/测试/无 Redis 自动降级);重启后消息丢失。
接口与 Redis 实现完全对齐(tests/test_queue.py 同一套用例对拍)。
"""

import threading
import time
from collections import deque

from travel_agent.queue.base import QueueUnavailableError
from travel_agent.schemas.message import AgentMessage, StreamName


class InMemoryQueue:
    """MessageQueue 协议的进程内实现。

    - 消费即删:ack 为 no-op;
    - 幂等去重:consume 时按 idempotency_key 查 _seen(带 TTL),已处理的直接丢弃;
    - 阻塞等待:用 Condition 实现 block_ms 语义,超时返回空列表。
    """

    def __init__(self, idempotency_ttl_s: float = 3600.0):
        self._streams: dict[str, deque[tuple[str, AgentMessage]]] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seen: dict[str, float] = {}          # idempotency_key -> 过期时间戳
        self._idempotency_ttl_s = idempotency_ttl_s
        self._closed = False

    # ── MessageQueue 协议实现 ──

    def publish(self, msg: AgentMessage, stream: str = StreamName.TASKS.value) -> str:
        with self._lock:
            self._streams.setdefault(stream, deque()).append((msg.id, msg))
            self._cond.notify_all()                # 唤醒所有等待中的消费者
        return msg.id

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[tuple[str, AgentMessage]]:
        # group/consumer 为接口兼容参数:进程内单队列无消费者组概念
        deadline = time.monotonic() + block_ms / 1000.0
        with self._cond:
            q = self._streams.setdefault(stream, deque())
            while not q and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []                      # 超时:正常返回空列表
                self._cond.wait(timeout=remaining)
            if not q:
                return []
            out: list[tuple[str, AgentMessage]] = []
            now = time.time()
            while q and len(out) < count:
                msg_id, msg = q.popleft()
                key = msg.idempotency_key
                seen_at = self._seen.get(key)
                if seen_at is not None and seen_at > now:
                    continue                       # 幂等去重:已处理过,丢弃
                self._seen[key] = now + self._idempotency_ttl_s
                out.append((msg_id, msg))
            self._purge_seen(now)
            return out

    def ack(self, stream: str, group: str, msg_id: str) -> None:
        return None                                # 消费即删,无确认概念

    def healthcheck(self) -> bool:
        return not self._closed

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # ── 内部 ──

    def _purge_seen(self, now: float) -> None:
        """清理过期的幂等记录,防止 _seen 无限增长。"""
        expired = [k for k, ts in self._seen.items() if ts <= now]
        for k in expired:
            del self._seen[k]
