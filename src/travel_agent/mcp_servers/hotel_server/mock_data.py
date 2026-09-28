"""确定性酒店 mock 数据:同城同日期结果恒定(哈希派生),支持故障注入。

- 确定性:房价/评分由 `md5(city|checkin|序号)` 派生,同一查询永远同一结果;
- 故障注入:环境变量 MOCK_FAULT=hotel_no_room 时全部酒店 availability=0
  (评测"酒店无房"异常用例的确定性通道)。
"""

import hashlib
import os
import random
from datetime import datetime

from travel_agent.mcp_servers.hotel_server.contracts import AvailabilityInfo, HotelOption

NAMES = ["湖畔假日酒店", "城市中心酒店", "悦府精选", "云顶山庄", "智选假日酒店"]
AREAS = ["市中心", "景区", "商务区", "车站附近"]
PRICE_TABLE = [320.0, 480.0, 620.0, 880.0, 1200.0, 1680.0]
RATINGS = [4.2, 4.4, 4.6, 4.8, 4.9]
AMENITIES_POOL = ["免费WiFi", "含早餐", "健身房", "游泳池", "停车场", "亲子设施"]
ROOM_TYPES = ["标准间", "大床房", "家庭房"]

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


def search(city: str, checkin: str, checkout: str, guests: int,
           max_price: float | None = None) -> list[HotelOption]:
    """某城入住区间酒店列表(3-4 家,确定性)。"""
    _check_city(city)
    _parse_date(checkin)
    _parse_date(checkout)
    rng = random.Random(_h(city, checkin))
    count = rng.randint(3, 4)
    hotels: list[HotelOption] = []
    for i in range(count):
        h = _h(city, checkin, str(i))
        price = PRICE_TABLE[h % len(PRICE_TABLE)] + (h % 3) * 20.0
        if max_price is not None and price > max_price:
            continue
        amenities = [AMENITIES_POOL[j] for j in range(len(AMENITIES_POOL)) if h & (1 << j)]
        hotels.append(
            HotelOption(
                hotel_id=f"HTL-{city}-{h % 9000 + 1000}",
                name=f"{city}{NAMES[h % len(NAMES)]}",
                city=city,
                area=AREAS[h % len(AREAS)],
                distance_to_center_km=round(0.5 + (h % 80) / 10.0, 1),
                price_per_night_cny=price,
                room_type=ROOM_TYPES[h % len(ROOM_TYPES)],
                rating=RATINGS[h % len(RATINGS)],
                amenities=amenities,
                availability=0 if _fault() == "hotel_no_room" else 2 + h % 10,
                source_ts=datetime.now(),
            )
        )
    return hotels


def availability(hotel_id: str, checkin: str, checkout: str) -> AvailabilityInfo:
    """指定酒店房态(确定性)。"""
    _parse_date(checkin)
    _parse_date(checkout)
    h = _h(hotel_id, checkin)
    if _fault() == "hotel_no_room":
        available, rooms = False, 0
    else:
        available = h % 10 != 0          # 10% 概率满房
        rooms = 0 if not available else 1 + h % 14
    return AvailabilityInfo(
        hotel_id=hotel_id,
        checkin=checkin,
        checkout=checkout,
        available=available,
        remaining_rooms=rooms,
        source_ts=datetime.now(),
    )
