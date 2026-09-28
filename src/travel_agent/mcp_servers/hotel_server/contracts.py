"""hotel-server 工具契约:与真实酒店 API(Amadeus/Booking/携程)对齐。

契约即接口文档:工具名、参数、返回结构在此定义。
换真实 API 时只改 server.py 内部实现,以下契约保持不变。
"""

from travel_agent.schemas.options import AvailabilityInfo, HotelOption

__all__ = ["AvailabilityInfo", "HotelOption", "TOOLS"]

TOOLS: dict = {
    "search_hotels": {
        "city": str, "checkin": str, "checkout": str, "guests": int,
        "max_price": float | None,
        "returns": list[HotelOption],
    },
    "check_availability": {"hotel_id": str, "checkin": str, "checkout": str, "returns": AvailabilityInfo},
}
