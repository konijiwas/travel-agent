"""确定性航班 mock 数据:同航线同日期结果恒定(哈希派生),支持故障注入。

- 确定性:价格/时刻由 `md5(origin|destination|date|序号)` 派生,同一查询永远同一结果;
- 故障注入:环境变量 MOCK_FAULT=flight_timeout 时 search_flights 阻塞 6 秒,
  超过客户端 5s 超时 → 触发超时/熔断链路(评测 error 用例的确定性通道)。
"""

import hashlib
import os
import random
import time
from datetime import datetime, timedelta

from travel_agent.mcp_servers.flight_server.contracts import FlightOption, FlightStatus

AIRLINES = ["东方航空", "南方航空", "中国国航", "吉祥航空", "春秋航空"]
AIRLINE_CODES = ["MU", "CZ", "CA", "HO", "9C"]
PRICE_TABLE = [380.0, 520.0, 780.0, 1080.0, 1450.0, 1950.0]
CABINS = ["经济舱", "商务舱"]

def _fault() -> str:
    """故障注入开关:每次调用时读取,支持运行时/评测逐用例注入。"""
    return os.getenv("MOCK_FAULT", "none")


_CITIES = {"北京", "上海", "杭州", "成都", "三亚", "西安"}


def _h(*parts: str) -> int:
    return int(hashlib.md5("|".join(parts).encode("utf-8")).hexdigest(), 16)


def _parse_date(day: str) -> datetime:
    try:
        return datetime.strptime(day, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD,收到:{day!r}") from e


def _check_city(city: str) -> None:
    if city not in _CITIES:
        raise ValueError(f"暂不支持的城市:{city!r},可选:{sorted(_CITIES)}")


def search(origin: str, destination: str, date: str, travelers: int) -> list[FlightOption]:
    """某航线某日航班列表(3-5 班,确定性)。"""
    if _fault() == "flight_timeout":
        time.sleep(6.0)
    _check_city(origin)
    _check_city(destination)
    if origin == destination:
        raise ValueError("出发地与目的地不能相同")
    dep_date = _parse_date(date)
    rng = random.Random(_h(origin, destination, date))
    count = rng.randint(3, 5)
    flights: list[FlightOption] = []
    for i in range(count):
        h = _h(origin, destination, date, str(i))
        dep_hour = 7 + h % 13                     # 7:00-19:00 起飞
        dep = dep_date + timedelta(hours=dep_hour, minutes=(h // 13) % 60)
        dur = timedelta(hours=1 + (h // 60) % 3, minutes=h % 60)
        arr = dep + dur
        flights.append(
            FlightOption(
                flight_no=f"{AIRLINE_CODES[h % len(AIRLINE_CODES)]}{1000 + h % 9000}",
                airline=AIRLINES[h % len(AIRLINES)],
                origin=origin,
                destination=destination,
                departure_ts=dep.isoformat(),
                arrival_ts=arr.isoformat(),
                price_cny=PRICE_TABLE[h % len(PRICE_TABLE)] + (h % 3) * 30.0,
                cabin=CABINS[h % 2],
                availability=max(0, travelers - 1 + h % 6),
                score=round(0.5 + (h % 50) / 100.0, 2),
                source_ts=datetime.now(),
            )
        )
    return flights


def status(flight_no: str, date: str) -> FlightStatus:
    """航班动态(确定性:约 70% 准点 / 25% 延误 / 5% 取消)。"""
    _parse_date(date)
    h = _h(flight_no, date)
    roll = h % 100
    if roll < 70:
        st, delay, note = "on_time", 0, "准点"
    elif roll < 95:
        st, delay, note = "delayed", 30 + h % 210, "流量管制"
    else:
        st, delay, note = "cancelled", 0, "天气原因取消"
    return FlightStatus(
        flight_no=flight_no,
        date=date,
        status=st,
        delay_minutes=delay,
        note=note,
        source_ts=datetime.now(),
    )
