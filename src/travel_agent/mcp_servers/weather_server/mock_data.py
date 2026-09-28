"""确定性天气 mock 数据:同城同日结果恒定(种子随机),支持故障注入。

- 确定性:以 `md5(city|date)` 作随机种子,同一查询永远同一结果,评测可复现;
- 故障注入:环境变量 MOCK_FAULT=weather_empty 时预报返回空列表(触发"无数据降级"链路)。
"""

import hashlib
import os
import random
from datetime import datetime, timedelta

from travel_agent.mcp_servers.weather_server.contracts import DailyWeather, WeatherInfo

CITIES = {"北京", "上海", "杭州", "成都", "三亚", "西安"}
CONDITIONS = ["晴", "多云", "小雨", "大雨", "雪", "极端高温"]
OUTDOOR_OK = {"晴", "多云"}

def _fault() -> str:
    """故障注入开关:每次调用时读取,支持运行时/评测逐用例注入。"""
    return os.getenv("MOCK_FAULT", "none")


MAX_FORECAST_DAYS = 16


def _rng(city: str, day: str) -> random.Random:
    seed = int(hashlib.md5(f"{city}|{day}".encode("utf-8")).hexdigest(), 16)
    return random.Random(seed)


def _parse_date(day: str) -> datetime:
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD,收到:{day!r}") from e


def _day_weather(city: str, day: str) -> DailyWeather:
    rng = _rng(city, day)
    condition = rng.choice(CONDITIONS)
    if condition == "极端高温":
        temp = rng.uniform(36.0, 41.0)
    elif condition == "雪":
        temp = rng.uniform(-8.0, 2.0)
    else:
        temp = rng.uniform(8.0, 34.0)
    return DailyWeather(
        city=city,
        date=day,
        condition=condition,
        temp_c=round(temp, 1),
        wind_kmh=round(rng.uniform(0.0, 30.0), 1),
        humidity_pct=rng.randint(30, 95),
        is_outdoor_friendly=condition in OUTDOOR_OK,
        source_ts=datetime.now(),
    )


def current(city: str, day: str) -> WeatherInfo:
    """某城某日当前天气。"""
    if city not in CITIES:
        raise ValueError(f"暂不支持的城市:{city!r},可选:{sorted(CITIES)}")
    _parse_date(day)
    return _day_weather(city, day)


def forecast(city: str, start_date: str, end_date: str) -> list[DailyWeather]:
    """某城日期区间逐日预报(含首尾,最长 {MAX_FORECAST_DAYS} 天)。"""
    if _fault() == "weather_empty":
        return []
    if city not in CITIES:
        raise ValueError(f"暂不支持的城市:{city!r},可选:{sorted(CITIES)}")
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    if (end - start).days + 1 > MAX_FORECAST_DAYS:
        raise ValueError(f"预报区间最长 {MAX_FORECAST_DAYS} 天")
    days = []
    d = start
    while d <= end:
        days.append(_day_weather(city, d.strftime("%Y-%m-%d")))
        d += timedelta(days=1)
    return days
