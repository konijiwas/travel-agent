"""weather-server:FastMCP 服务,暴露 2 个工具。

启动方式(stdio,由 MCP 客户端拉起):
    uv run python -m travel_agent.mcp_servers.weather_server.server
"""

from fastmcp import FastMCP

from travel_agent.mcp_servers.weather_server import mock_data
from travel_agent.mcp_servers.weather_server.contracts import DailyWeather, WeatherInfo

mcp = FastMCP("weather-server")


@mcp.tool()
def get_current_weather(city: str, date: str) -> WeatherInfo:
    """查询指定城市某日的当前天气。city:城市名(如"杭州");date:YYYY-MM-DD。"""
    return mock_data.current(city, date)


@mcp.tool()
def get_forecast(city: str, start_date: str, end_date: str) -> list[DailyWeather]:
    """查询城市在日期区间内的逐日天气预报(含首尾两天)。"""
    return mock_data.forecast(city, start_date, end_date)


if __name__ == "__main__":
    mcp.run(transport="stdio")
