"""flight-server:FastMCP 服务,暴露 2 个工具。

启动方式(stdio,由 MCP 客户端拉起):
    uv run python -m travel_agent.mcp_servers.flight_server.server
"""

from fastmcp import FastMCP

from travel_agent.mcp_servers.flight_server import mock_data
from travel_agent.mcp_servers.flight_server.contracts import FlightOption, FlightStatus

mcp = FastMCP("flight-server")


@mcp.tool()
def search_flights(origin: str, destination: str, date: str, travelers: int) -> list[FlightOption]:
    """查询某航线某日的航班列表(时刻 + 价格 + 余票)。travelers:出行人数。"""
    return mock_data.search(origin, destination, date, travelers)


@mcp.tool()
def get_flight_status(flight_no: str, date: str) -> FlightStatus:
    """查询指定航班某日的动态(准点/延误/取消)。"""
    return mock_data.status(flight_no, date)


if __name__ == "__main__":
    mcp.run(transport="stdio")
