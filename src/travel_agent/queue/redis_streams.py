"""Redis Streams 队列实现:消费者组 + SETNX 幂等 + XAUTOCLAIM 崩溃恢复。"""

import redis

from travel_agent.queue.base import QueueUnavailableError
from travel_agent.schemas.message import AgentMessage, StreamName

_IDEM_PREFIX = "idem:"          # 幂等 key 在 Redis 中的键前缀
_IDEM_TTL_S = 3600              # 幂等记录过期时间(与内存实现一致)
_PENDING_RECLAIM_MS = 30_000    # 超过 30s 未确认视为崩溃遗留,重新认领
_MAXLEN = 10_000                # 每流最多保留条数(防无限增长)


class RedisStreamsQueue:
    """MessageQueue 协议的 Redis Streams 实现。

    语义要点:
    - publish:XADD 追加,消息整体 JSON 存单个字段,幂等 key 单独存;
    - consume:XREADGROUP 消费者组消费,内部先 XAUTOCLAIM 认领崩溃遗留,
      再 SETNX 幂等去重(重复消息直接 XACK 跳过);
    - ack:XACK 确认,消息进入已确认状态;
    - 所有 Redis 异常统一转 QueueUnavailableError(熔断/降级的上游信号)。
    """

    def __init__(self, url: str = "redis://127.0.0.1:6379/0"):
        self._client = redis.Redis.from_url(url, decode_responses=True)

    # ── MessageQueue 协议实现 ──

    def publish(self, msg: AgentMessage, stream: str = StreamName.TASKS.value) -> str:
        try:
            entry_id = self._client.xadd(
                stream,
                {"json": msg.model_dump_json(), "idem": msg.idempotency_key},
                id="*",
                maxlen=_MAXLEN,
                approximate=True,
            )
        except redis.RedisError as e:
            raise QueueUnavailableError(f"发布失败(stream={stream}):{e}") from e
        return entry_id

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[tuple[str, AgentMessage]]:
        try:
            self._ensure_group(stream, group)
            self._reclaim_pending(stream, group, consumer)
            rows = self._client.xreadgroup(
                group, consumer, {stream: ">"}, count=count, block=block_ms)
        except redis.RedisError as e:
            raise QueueUnavailableError(f"消费失败(stream={stream}):{e}") from e
        if not rows:
            return []                              # 阻塞超时:无新消息
        out: list[tuple[str, AgentMessage]] = []
        for _stream, entries in rows:
            for entry_id, fields in entries:
                key = fields.get("idem", "")
                # SETNX:只有第一次 set 返回 True;重复 key → 已被处理过
                if key and not self._client.set(
                        _IDEM_PREFIX + key, entry_id, nx=True, ex=_IDEM_TTL_S):
                    self._client.xack(stream, group, entry_id)
                    continue                       # 幂等去重:跳过重复消息
                try:
                    msg = AgentMessage.model_validate_json(fields["json"])
                except Exception:
                    self._client.xack(stream, group, entry_id)
                    continue                       # 坏消息:确认丢弃,不阻塞队列
                out.append((entry_id, msg))
        return out

    def ack(self, stream: str, group: str, msg_id: str) -> None:
        try:
            self._client.xack(stream, group, msg_id)
        except redis.RedisError as e:
            raise QueueUnavailableError(f"确认失败(stream={stream}):{e}") from e

    def healthcheck(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except redis.RedisError:
            pass

    # ── 内部 ──

    def _ensure_group(self, stream: str, group: str) -> None:
        """创建消费者组(已存在则忽略)。组从最新消息开始消费($)。"""
        try:
            self._client.xgroup_create(stream, group, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):          # 组已存在是正常情况
                raise

    def _reclaim_pending(self, stream: str, group: str, consumer: str) -> None:
        """崩溃恢复:认领本组超过 30s 未确认的消息,保证"至少一次"投递。"""
        self._client.xautoclaim(
            stream, group, consumer,
            min_idle_time=_PENDING_RECLAIM_MS, start_id="0-0",
        )
