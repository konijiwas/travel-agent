"""M5 行程执行者测试:雨天重排 / 冲突避免与 2h 缓冲 / 数据缺失降级 / 确定性。"""

from datetime import datetime

from travel_agent.agents.executor.agent import Executor
from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.options import DailyWeather, FlightOption, HotelOption
from travel_agent.schemas.requirement import BudgetRange, TripRequirement


# ── 构造辅助 ──

def _req() -> TripRequirement:
    return TripRequirement(
        destination="杭州", origin_city="上海",
        start_date="2026-10-01", end_date="2026-10-03", travelers=1,
        budget_range=BudgetRange(min_cny=0.0, max_cny=6000.0),
    )


def _flights() -> list[FlightOption]:
    return [FlightOption(
        flight_no="MU5131", airline="东方航空", origin="上海", destination="杭州",
        departure_ts="2026-10-01T09:10:00", arrival_ts="2026-10-01T10:20:00",
        price_cny=780.0, cabin="经济舱", availability=5, score=0.8,
        source_ts=datetime(2026, 9, 30, 12, 0, 0),
    )]


def _hotels() -> list[HotelOption]:
    return [HotelOption(
        hotel_id="HTL-1", name="杭州湖畔假日酒店", city="杭州", area="市中心",
        distance_to_center_km=2.0, price_per_night_cny=620.0, room_type="标准间",
        rating=4.6, amenities=["免费WiFi"], availability=5,
        source_ts=datetime(2026, 9, 30, 12, 0, 0),
    )]


def _weather(conditions: dict[str, tuple[str, bool]]) -> list[DailyWeather]:
    """date -> (condition, is_outdoor_friendly)。"""
    return [DailyWeather(
        city="杭州", date=d, condition=c, temp_c=26.0, wind_kmh=5.0,
        humidity_pct=60, is_outdoor_friendly=ok,
        source_ts=datetime(2026, 9, 30, 12, 0, 0),
    ) for d, (c, ok) in conditions.items()]


_BUDGET = BudgetPlan(total_budget=5000.0, items=[])
_GOOD = {"2026-10-01": ("晴", True), "2026-10-02": ("多云", True), "2026-10-03": ("晴", True)}


def _build(weather: list[DailyWeather]):
    return Executor().build_itinerary(_req(), _BUDGET, weather, _flights(), _hotels())


# ── 测试 ──

def test_rainy_day_replaces_outdoor_activity():
    """10-02 小雨:室外主活动替换为室内,三要素留痕,晴天日不受影响。"""
    it = _build(_weather({
        "2026-10-01": ("晴", True),
        "2026-10-02": ("小雨", False),
        "2026-10-03": ("晴", True),
    }))
    assert len(it.days) == 3

    d2 = it.days[1]                       # 中间日:activities[0] 是主活动
    main = d2.activities[0]
    assert main.kind == "indoor"
    assert "雨天" in main.note
    assert "小雨" in d2.weather_note and "改为" in d2.weather_note

    assert len(it.weather_changes) == 1
    ch = it.weather_changes[0]
    assert ch.day == "2026-10-02"
    assert ch.reason and ch.scope and ch.replacement   # 三要素齐全

    assert it.days[0].activities[1].kind == "outdoor"  # 晴天日主活动未被替换
    assert "晴" in it.days[0].weather_note


def test_no_time_overlap_and_two_hour_buffer():
    """全天好天气:时段不重叠、每天 2-4 个活动、抵达/返程 2h 缓冲、无冲突。"""
    it = _build(_weather(_GOOD))
    for day in it.days:
        acts = sorted(day.activities, key=lambda a: a.start_time)
        for prev, cur in zip(acts, acts[1:]):
            assert cur.start_time >= prev.end_time     # HH:MM 字符串比较即时间比较
        assert 2 <= len(acts) <= 4

    # 抵达日:10:20 落地,主活动 12:20 开始(= 到达 + 2h 缓冲,确定性精确值)
    assert it.days[0].activities[1].start_time == "12:20"
    # 返程日:午餐 13:00 结束,返程航班不早于 15:00(缓冲 ≥2h)
    assert it.days[2].activities[-1].start_time >= "15:00"
    assert it.conflicts == []


def test_missing_weather_degrades_gracefully():
    """天气数据为空:不做替换、如实标注、活动照常编排。"""
    it = _build([])
    assert it.weather_changes == []
    for day in it.days:
        assert day.weather_note == "天气未知,建议出行前确认"
        assert day.activities
    assert it.days[0].activities[1].kind == "outdoor"   # 规则6:不替换


def test_deterministic_same_input_same_output():
    """同输入两次编排,逐日逐活动完全一致(确定性保证)。"""
    def snapshot(it):
        return [(d.date, [(a.name, a.start_time, a.end_time) for a in d.activities])
                for d in it.days]

    w = _weather(_GOOD)
    assert snapshot(_build(w)) == snapshot(_build(w))
