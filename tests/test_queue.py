"""M6 队列对拍测试:内存与 Redis 两个实现跑同一套用例,保证接口语义一致。

Redis 未运行时自动跳过 Redis 组(装 Memurai 后同一套用例自动生效)。
注意两个 Redis 语义点(测试据此设计):
1. 消费者组在**创建时刻**起算,建组前发布的消息不会被 ">" 读到 → 先 _prime 建组再发布;
2. Redis 跨测试/跨进程持久 → 幂等 key 必须带随机后缀,避免 SETNX 残留误伤。
"""

import uuid

import pytest

from travel_agent.queue.consumer import consume_batch
from travel_agent.queue.memory import InMemoryQueue
from travel_agent.queue.redis_streams import RedisStreamsQueue
from travel_agent.schemas.message import (
    AgentMessage,
    MessageType,
    StreamName,
    make_idempotency_key,
)

GROUP, CONSUMER = "g1", "c1"
# 测试专用 Redis 数据库(与生产 db 0 隔离,防止 CLI 运行的消息污染测试)
TEST_REDIS_URL = "redis://127.0.0.1:6379/1"


@pytest.fixture(scope="session", autouse=True)
def clean_redis_test_db():
    """测试会话开始前清空 db 1:隔离上次运行/生产消息残留。"""
    import redis as redis_lib

    try:
        r = redis_lib.Redis.from_url(TEST_REDIS_URL)
        r.flushdb()
        r.close()
    except Exception:
        pass    # Redis 未运行则什么都不做(后续用例会 skip)


@pytest.fixture(params=["memory", "redis"])
def queue(request):
    """两个实现对拍:同一套测试各跑一遍。"""
    if request.param == "memory":
        q = InMemoryQueue()
    else:
        q = RedisStreamsQueue(url=TEST_REDIS_URL)
        if not q.healthcheck():
            q.close()
            pytest.skip("Redis 未运行,跳过 Redis 对拍(安装 Memurai 后自动生效)")
    yield q
    q.close()


def _prime(queue, stream: str) -> None:
    """先消费一次(空轮)以创建消费者组:组从当前末尾 $ 起算,只消费此后的消息。"""
    queue.consume(stream, GROUP, CONSUMER, count=1, block_ms=10)


def _msg(topic="test.topic", payload=None, key=None) -> AgentMessage:
    """显式传 key 时原样使用(测试可控制两条消息共享同一 key);
    未传时生成带随机后缀的 key,防 Redis 跨测试 SETNX 残留误判为重复。"""
    if key is None:
        key = (make_idempotency_key(topic, "s1", 1, payload or {})
               + "-" + uuid.uuid4().hex[:8])
    return AgentMessage(
        type=MessageType.TASK_DISPATCH,
        topic=topic,
        from_agent="test",
        to_agent="test",
        payload=payload or {},
        idempotency_key=key,
    )


def test_publish_consume_roundtrip(queue):
    """发布 → 消费 → 内容一致 → ack。"""
    _prime(queue, StreamName.TASKS.value)
    msg = _msg(topic="task.analyze", payload={"x": 1})
    qid = queue.publish(msg)
    assert qid
    items = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=1, block_ms=50)
    assert len(items) == 1
    qid2, got = items[0]
    assert got.topic == "task.analyze"
    assert got.payload == {"x": 1}
    queue.ack(StreamName.TASKS.value, GROUP, qid2)


def test_consume_timeout_returns_empty(queue):
    """空队列阻塞等待:超时返回空列表(合同约定,非异常)。"""
    _prime(queue, StreamName.TASKS.value)
    items = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=1, block_ms=50)
    assert items == []


def test_idempotent_dedup(queue):
    """同一幂等 key 重发:第一条被处理,第二条被去重丢弃。"""
    _prime(queue, StreamName.TASKS.value)
    key = make_idempotency_key("t", "s", 1) + "-" + uuid.uuid4().hex[:8]
    queue.publish(_msg(topic="t", payload={"n": 1}, key=key))
    queue.publish(_msg(topic="t", payload={"n": 2}, key=key))   # 重发,内容不同但 key 相同
    first = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=10, block_ms=50)
    assert len(first) == 1
    assert first[0][1].payload == {"n": 1}
    rest = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=10, block_ms=50)
    assert rest == []


def test_streams_isolated(queue):
    """不同流互不串扰:各流只消费到自己发布的消息。"""
    _prime(queue, StreamName.TASKS.value)
    _prime(queue, StreamName.EVENTS.value)
    queue.publish(_msg(topic="a"), stream=StreamName.TASKS.value)
    queue.publish(_msg(topic="b"), stream=StreamName.EVENTS.value)
    t = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=5, block_ms=50)
    e = queue.consume(StreamName.EVENTS.value, GROUP, CONSUMER, count=5, block_ms=50)
    assert len(t) == 1 and t[0][1].topic == "a"
    assert len(e) == 1 and e[0][1].topic == "b"


def test_batch_count(queue):
    """count 参数:一次只取指定条数,剩下的下次取。"""
    _prime(queue, StreamName.TASKS.value)
    for i in range(5):
        queue.publish(_msg(topic=f"t{i}"))
    first = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=3, block_ms=50)
    second = queue.consume(StreamName.TASKS.value, GROUP, CONSUMER, count=3, block_ms=50)
    assert len(first) == 3
    assert len(second) == 2


def test_consume_batch_helper(queue):
    """consume_batch 封装:handler 返回 True → 全部 ack,第二轮为空。"""
    _prime(queue, StreamName.TASKS.value)
    for i in range(3):
        queue.publish(_msg(topic=f"t{i}"))

    collected = []

    def handler(msg):
        collected.append(msg.topic)
        return True

    handled = consume_batch(queue, StreamName.TASKS.value, GROUP, CONSUMER,
                            handler=handler, count=10, block_ms=50)
    assert handled == 3
    assert sorted(collected) == ["t0", "t1", "t2"]
    assert consume_batch(queue, StreamName.TASKS.value, GROUP, CONSUMER,
                         handler=handler, count=10, block_ms=50) == 0
