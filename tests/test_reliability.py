"""M7 可靠性六件套单元测试:全部确定性、无网络、秒级跑完。

覆盖:超时(成功/超时)、重试(成功/耗尽/不重试/退避公式/Retry-After)、
熔断(开闸/冷却恢复/半开再败)、权限(分级/确认拦截/黑名单/预演)、
成本护栏(记账/折算/双上限/估算)、wrap_tool_call 统一入口
(成功/熔断拦截/权限拦截/重试标记)。
"""

import asyncio

import pytest

from travel_agent.reliability import (
    ConfirmationRequiredError,
    PermissionDeniedError,
    ToolTimeoutError,
    assert_allowed,
    await_with_timeout,
    backoff_delay,
    call_with_retry,
    classify_tool,
    default_is_retryable,
    estimate_tokens,
    get_breaker,
    reset_breakers,
    wrap_tool_call,
)
from travel_agent.reliability.circuit_breaker import CircuitBreaker, CircuitOpenError
from travel_agent.reliability.cost_guard import (
    CostGuard,
    CostLimitExceededError,
    get_guard,
)


@pytest.fixture(autouse=True)
def clean_breakers():
    """每个用例清空熔断器注册表:防止用例间状态串扰。"""
    reset_breakers()
    yield
    reset_breakers()


# ── ① 超时 ──

async def test_timeout_raises_tool_timeout_error():
    """超过时限:抛 ToolTimeoutError 并携带工具名与时限。"""
    async def slow():
        await asyncio.sleep(5)
        return "late"

    with pytest.raises(ToolTimeoutError) as ei:
        await await_with_timeout("slow_tool", slow, timeout_s=0.1)
    assert ei.value.name == "slow_tool"
    assert ei.value.timeout_s == 0.1


async def test_timeout_passes_when_fast():
    """时限内完成:正常返回结果,无异常。"""
    async def fast():
        await asyncio.sleep(0)
        return "done"

    assert await await_with_timeout("fast_tool", fast, timeout_s=1.0) == "done"


# ── ③ 重试 ──

@pytest.fixture
def no_sleep(monkeypatch):
    """把退避等待换成记录器:测试秒级完成,且能断言等待参数。"""
    waits = []

    async def fake_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("travel_agent.reliability.retry.asyncio.sleep", fake_sleep)
    return waits


async def test_retry_succeeds_on_second_attempt(no_sleep):
    """第一次瞬时失败(OSError),第二次成功:返回结果且等待过退避。"""
    calls = [0]

    async def flaky():
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("连接被重置")
        return "ok"

    result = await call_with_retry("flaky", flaky, max_attempts=3)
    assert result == "ok"
    assert calls[0] == 2
    assert len(no_sleep) == 1              # 只等待过一次(第二次成功后不再等)


async def test_retry_exhausted_raises_last_error(no_sleep):
    """持续失败:重试耗尽后原样抛出最后一次异常,尝试次数用满。"""
    calls = [0]

    async def always_fail():
        calls[0] += 1
        raise OSError("一直失败")

    with pytest.raises(OSError):
        await call_with_retry("bad", always_fail, max_attempts=3)
    assert calls[0] == 3


async def test_retry_skips_value_error(no_sleep):
    """参数类错误(ValueError)不可重试:立即抛出,不等待。"""
    calls = [0]

    async def bad_input():
        calls[0] += 1
        raise ValueError("出发地与目的地不能相同")

    with pytest.raises(ValueError):
        await call_with_retry("bad_input", bad_input, max_attempts=3)
    assert calls[0] == 1
    assert no_sleep == []                  # 不可重试 → 一次退避都不等


async def test_retry_uses_retry_after(no_sleep):
    """服务端带 retry_after 建议:等待时间 = 该值(封顶),不再指数计算。"""
    calls = [0]

    async def limited():
        calls[0] += 1
        exc = OSError("429 限流")
        exc.retry_after = 0.2              # 服务端建议 0.2s 后再来
        raise exc

    with pytest.raises(OSError):
        await call_with_retry("limited", limited, max_attempts=2)
    assert no_sleep == [0.2]


def test_backoff_delay_exponential_with_jitter():
    """退避公式:指数增长 + 抖动区间;retry_after 直接采用并封顶。"""
    d1 = backoff_delay(2, None)            # 第2次尝试前:0.5 * 2^0 = 0.5 起
    assert 0.5 <= d1 <= 0.75               # 抖动幅度 = 等待的一半
    d2 = backoff_delay(4, None)            # 0.5*2^2=2 起
    assert 2.0 <= d2 <= 3.0
    assert backoff_delay(2, retry_after=0.3) == 0.3
    assert backoff_delay(2, retry_after=99) == 5.0   # 超上限 → 封顶 5s


def test_default_is_retryable_classification():
    """错误分类:瞬时类可重试,业务/参数类不可;按状态码判断。"""
    assert default_is_retryable(TimeoutError()) is True
    assert default_is_retryable(ConnectionError()) is True
    assert default_is_retryable(OSError()) is True
    assert default_is_retryable(ValueError()) is False

    class HttpLike(Exception):
        def __init__(self, status):
            self.status_code = status

    assert default_is_retryable(HttpLike(429)) is True     # 限流:可重试
    assert default_is_retryable(HttpLike(503)) is True     # 服务端故障:可重试
    assert default_is_retryable(HttpLike(400)) is False    # 参数错:不可重试
    assert default_is_retryable(HttpLike(402)) is False    # 余额不足:立即暴露


# ── ② 熔断 ──

def test_breaker_opens_after_threshold():
    """连续失败达到阈值:开闸,后续请求被拒绝。"""
    brk = CircuitBreaker("x", failure_threshold=3, recovery_s=30)
    assert brk.state == "closed"
    brk.record_failure()
    brk.record_failure()
    assert brk.allow() is True             # 还没到阈值
    brk.record_failure()                   # 第 3 败
    assert brk.state == "open"
    assert brk.allow() is False            # 开闸:拒绝


def test_breaker_recovers_after_cooldown():
    """冷却时间到:进入半开,探测成功 → 恢复闭合。"""
    brk = CircuitBreaker("y", failure_threshold=1, recovery_s=0.05)
    brk.record_failure()                   # 立即开闸
    assert brk.allow() is False
    import time
    time.sleep(0.06)                       # 冷却结束
    assert brk.state == "half_open"
    assert brk.allow() is True             # 放行探测请求
    brk.record_success()
    assert brk.state == "closed"


def test_breaker_half_open_failure_reopens():
    """半开探测失败:重新开闸计时(不会直接全量放开)。"""
    brk = CircuitBreaker("z", failure_threshold=1, recovery_s=0.05)
    brk.record_failure()
    import time
    time.sleep(0.06)
    assert brk.allow() is True             # 探测
    brk.record_failure()                   # 探测失败
    assert brk.state == "open"
    assert brk.allow() is False


def test_breaker_registry_shared_and_reset():
    """注册表:同名共享实例;reset 后重建。"""
    b1 = get_breaker("flight")
    b2 = get_breaker("flight")
    assert b1 is b2
    reset_breakers()
    assert get_breaker("flight") is not b1


# ── ④ 权限分级 ──

def test_classify_tool_levels():
    """工具名分级:查询自动;危险动词(含 server 前缀)需确认。"""
    assert classify_tool("search_flights") == "auto"
    assert classify_tool("get_current_weather") == "auto"
    assert classify_tool("book_ticket") == "confirm"
    assert classify_tool("pay_order") == "confirm"
    assert classify_tool("flight__book_ticket") == "confirm"   # 带前缀也命中


def test_assert_allowed_confirm_requires():
    """confirm 类:默认拦截并携带参数摘要;预批准/预演豁免。"""
    with pytest.raises(ConfirmationRequiredError) as ei:
        assert_allowed("book_ticket", {"price": 9999})
    assert ei.value.tool_name == "book_ticket"
    assert ei.value.args_summary == {"price": 9999}

    assert_allowed("book_ticket", {"price": 1}, pre_approved=True)   # 已同意:放行
    assert_allowed("book_ticket", {"price": 1}, dry_run=True)        # 预演:放行
    assert_allowed("search_flights", None)                           # auto:直接放行


def test_assert_allowed_deny_list(monkeypatch):
    """黑名单:任何情况都拒绝执行。"""
    monkeypatch.setattr("travel_agent.reliability.permissions._DENY_NAMES",
                        ("evil_tool",))
    with pytest.raises(PermissionDeniedError):
        assert_allowed("evil_tool", None)
    assert classify_tool("evil_tool") == "deny"


# ── ⑤ 成本护栏 ──

def test_cost_guard_records_and_costs():
    """记账与金额折算:输入/输出按各自单价,除以百万。"""
    guard = CostGuard(limit_usd=10, token_limit=100_000)
    guard.record_usage(1000, 2000)
    assert guard.prompt_tokens == 1000
    assert guard.completion_tokens == 2000
    assert guard.total_tokens == 3000
    expected = (1000 * 0.28 + 2000 * 1.10) / 1_000_000
    assert guard.cost_usd == pytest.approx(expected)
    snap = guard.snapshot()
    assert snap["total_tokens"] == 3000
    assert snap["limit_usd"] == 10


def test_cost_guard_token_limit_exceeded():
    """token 超限:抛 CostLimitExceededError 并携带账目明细。"""
    guard = CostGuard(limit_usd=100, token_limit=100)
    with pytest.raises(CostLimitExceededError) as ei:
        guard.record_usage(60, 50)         # 合计 110 > 100
    assert ei.value.usage["total_tokens"] == 110
    assert ei.value.limit_usd == 100


def test_cost_guard_usd_limit_exceeded():
    """金额超限:同样终止(双上限任一触发)。"""
    guard = CostGuard(limit_usd=0.0001, token_limit=100_000)
    with pytest.raises(CostLimitExceededError):
        guard.record_usage(1000, 1000)     # 金额 ~$0.00138 > 0.0001


def test_estimate_tokens_cjk_aware():
    """估算:中文按字计,其余 4 字符 ≈ 1 token;空文本为 0。"""
    assert estimate_tokens("杭州天气") == 4
    assert estimate_tokens("hello") == 1          # 5 个字母 // 4 = 1
    assert estimate_tokens("hello world!") == 3   # 12 个非中文 // 4
    assert estimate_tokens("去杭州OK") == 3       # 3 中文 + 2 字母(2//4=0)
    assert estimate_tokens("") == 0


# ── 统一入口 wrap_tool_call ──

@pytest.fixture
def recorder(monkeypatch):
    """把 trace 落盘替换成内存列表:断言打点内容且不污染 data/ 目录。"""
    events = []
    monkeypatch.setattr(
        "travel_agent.reliability.observability.jsonl_writer.record_event",
        events.append)
    return events


async def test_wrap_tool_call_success(recorder):
    """正常路径:返回结果,熔断器记成功,span 状态 ok。"""
    async def work():
        return {"days": 3}

    result = await wrap_tool_call("weather.get_forecast", work,
                                  tool_args={"city": "杭州"}, breaker="weather")
    assert result == {"days": 3}
    assert get_breaker("weather").state == "closed"
    spans = [e for e in recorder if e.kind == "tool"]
    assert len(spans) == 1
    assert spans[0].name == "weather.get_forecast"
    assert spans[0].status == "ok"
    assert spans[0].input == {"city": "杭州"}


async def test_wrap_tool_call_breaker_open_rejects(recorder):
    """熔断开闸:请求不发,抛 CircuitOpenError,打点状态 open。"""
    brk = get_breaker("flight")
    for _ in range(brk.threshold):
        brk.record_failure()

    called = [False]

    async def work():
        called[0] = True

    with pytest.raises(CircuitOpenError):
        await wrap_tool_call("flight.search_flights", work, breaker="flight")
    assert called[0] is False                  # 请求根本没发出
    opens = [e for e in recorder if e.status == "open"]
    assert len(opens) == 1


async def test_wrap_tool_call_permission_blocked(recorder):
    """confirm 类未批准:抛 ConfirmationRequiredError,不打点不计数。"""
    called = [False]

    async def book():
        called[0] = True

    with pytest.raises(ConfirmationRequiredError):
        await wrap_tool_call("flight__book_ticket", book,
                             tool_args={"price": 9999})
    assert called[0] is False
    assert recorder == []                      # 权限拦截不是故障:不打点
    assert get_breaker("flight__book_ticket").state == "closed"


async def test_wrap_tool_call_marks_retry(recorder, monkeypatch):
    """发生过重试:span 状态记 retry,重试后成功正常返回。"""
    calls = [0]

    async def flaky():
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("瞬时断连")
        return "ok"

    async def fake_sleep(_):
        pass

    monkeypatch.setattr("travel_agent.reliability.retry.asyncio.sleep", fake_sleep)

    result = await wrap_tool_call("hotel.search_hotels", flaky, breaker="hotel")
    assert result == "ok"
    spans = [e for e in recorder if e.kind == "tool"]
    assert spans[0].status == "retry"


async def test_wrap_tool_call_timeout_status(recorder, monkeypatch):
    """超时(且重试耗尽):span 状态记 timeout,熔断器记一败。"""
    from travel_agent.config import settings

    async def hang():
        await asyncio.Event().wait()   # 永不完成:真挂起(不依赖 asyncio.sleep)

    async def fake_sleep(_):
        pass

    # 把全局超时临时调到 0.1s(默认 5s 会让测试等 10s+),退避等待改为记录
    monkeypatch.setattr(settings, "tool_timeout_s", 0.1)
    monkeypatch.setattr("travel_agent.reliability.retry.asyncio.sleep", fake_sleep)

    with pytest.raises(ToolTimeoutError):
        await wrap_tool_call("weather.get_forecast", hang,
                             breaker="weather", tool_args={})
    spans = [e for e in recorder if e.kind == "tool"]
    assert spans[0].status == "timeout"
    assert get_breaker("weather")._failures == 1   # 重试耗尽:记一败
