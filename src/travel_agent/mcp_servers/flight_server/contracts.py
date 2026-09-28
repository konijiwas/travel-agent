"""flight-server 工具契约:与真实航班 API(Amadeus/携程开放平台)对齐。

契约即接口文档:工具名、参数、返回结构在此定义。
换真实 API 时只改 server.py 内部实现,以下契约保持不变。
"""

from travel_agent.schemas.options import FlightOption, FlightStatus

__all__ = ["FlightOption", "FlightStatus", "TOOLS"]

TOOLS: dict = {
    "search_flights": {
        "origin": str, "destination": str, "date": str, "travelers": int,
        "returns": list[FlightOption],
    },
    "get_flight_status": {"flight_no": str, "date": str, "returns": FlightStatus},
}
