"""与 MCP 契约对齐的数据模型(天气/航班/酒店)。

三个 mock MCP Server 的工具入参/出参均以此为准;换真实 API 时
这些结构保持不变,即"接口契约与真实 API 对齐"。
所有价格/天气数据必须带 source_ts(质量门"数据时效"检查项)。
"""

from datetime import datetime

from pydantic import BaseModel


class WeatherInfo(BaseModel):
    """单日天气。condition 取值:晴|多云|小雨|大雨|雪|极端高温。"""

    city: str
    date: str                      # YYYY-MM-DD
    condition: str
    temp_c: float
    wind_kmh: float
    humidity_pct: int
    is_outdoor_friendly: bool      # 是否适合户外活动(晴/多云为 True)
    source_ts: datetime


class DailyWeather(WeatherInfo):
    """逐日预报项,结构与 WeatherInfo 一致。"""


class FlightOption(BaseModel):
    """一趟航班选项。时间戳为 ISO 格式 "YYYY-MM-DDTHH:MM:SS"。"""

    flight_no: str
    airline: str
    origin: str
    destination: str
    departure_ts: str
    arrival_ts: str
    price_cny: float
    cabin: str                     # 经济舱|商务舱
    availability: int              # 剩余座位
    score: float                   # 0-1 综合评分
    source_ts: datetime


class FlightStatus(BaseModel):
    """航班动态。status: on_time|delayed|cancelled。"""

    flight_no: str
    date: str
    status: str
    delay_minutes: int
    note: str
    source_ts: datetime


class HotelOption(BaseModel):
    """一家酒店选项。"""

    hotel_id: str
    name: str
    city: str
    area: str
    distance_to_center_km: float
    price_per_night_cny: float
    room_type: str
    rating: float                  # 4.0-5.0
    amenities: list[str]
    availability: int              # 剩余房量,0=无房
    source_ts: datetime


class AvailabilityInfo(BaseModel):
    """酒店房态查询结果。"""

    hotel_id: str
    checkin: str
    checkout: str
    available: bool
    remaining_rooms: int
    source_ts: datetime
