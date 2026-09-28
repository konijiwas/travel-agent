"""hotel-server:FastMCP 服务,暴露 2 个工具。

启动方式(stdio,由 MCP 客户端拉起):
    uv run python -m travel_agent.mcp_servers.hotel_server.server
"""

from fastmcp import FastMCP

from travel_agent.mcp_servers.hotel_server import mock_data
from travel_agent.mcp_servers.hotel_server.contracts import AvailabilityInfo, HotelOption

mcp = FastMCP("hotel-server")


@mcp.tool()
def search_hotels(city: str, checkin: str, checkout: str, guests: int,
                  max_price: float | None = None) -> list[HotelOption]:
    """查询某城入住区间的酒店列表(房型 + 价格 + 位置)。max_price:每晚价格上限(可选)。"""
    return mock_data.search(city, checkin, checkout, guests, max_price)


@mcp.tool()
def check_availability(hotel_id: str, checkin: str, checkout: str) -> AvailabilityInfo:
    """查询指定酒店在入住区间的房态。"""
    return mock_data.availability(hotel_id, checkin, checkout)


if __name__ == "__main__":
    mcp.run(transport="stdio")
