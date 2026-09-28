"""weather-server 工具契约:与真实天气 API(和风天气/OpenWeatherMap)对齐。

契约即接口文档:工具名、参数、返回结构在此定义。
换真实 API 时只改 server.py 内部实现,以下契约保持不变,
Agent 侧 / 评测 / 测试零改动。
"""

from travel_agent.schemas.options import DailyWeather, WeatherInfo

__all__ = ["DailyWeather", "WeatherInfo", "TOOLS"]

TOOLS: dict = {
    "get_current_weather": {"city": str, "date": str, "returns": WeatherInfo},
    "get_forecast": {"city": str, "start_date": str, "end_date": str, "returns": list[DailyWeather]},
}
