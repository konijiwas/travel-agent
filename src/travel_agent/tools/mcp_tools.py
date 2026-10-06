"""MCP 工具接线层:stdio 子进程 + 可靠性六件套包装。

用户决策:**每次现起现用** —— 每个工具调用临时拉起一次子进程
(venv python -m travel_agent.mcp_servers.<server>_server.server),
用完即关。生命周期极简、每次调用环境干净;代价是每次多约
1 秒启动时间(一次规划约 7 个工具调用,在 60s 延迟预算内)。

数据流:
    Supervisor 节点(同步) → fetch_*()
      → call_tool_sync() → asyncio.run(wrap_tool_call(...))
        六件套:权限→熔断→打点(每次尝试超时→重试)
        → fastmcp Client(stdio 子进程) → MCP Server 工具
        → structured_content(纯 dict/list) → 还原为契约 Pydantic 模型

来自最小实验实测的要点:
- fastmcp 4.0.10 stdio 写法:StdioTransport(command, args) + Client(t);
- 结果取 structured_content:直接是 JSON 兼容的 dict/list
  (data 字段是动态 dataclass Root,解包麻烦,不用);
- 子进程横幅/日志默认打到父进程 stderr → log_file 重定向到
  data/logs/,保持 CLI 输出干净;
- 子进程继承父进程环境变量 → MOCK_FAULT 故障注入照常生效。
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, TypeVar

from fastmcp import Client
from fastmcp.client.transports.stdio import StdioTransport

from travel_agent.config import settings
from travel_agent.reliability import wrap_tool_call
from travel_agent.schemas.options import DailyWeather, FlightOption, HotelOption
from travel_agent.schemas.requirement import TripRequirement

# 三个 server 的模块路径(与 mcp_servers/registry.py 的启动配置一致)
SERVER_MODULES = {
    "weather": "travel_agent.mcp_servers.weather_server.server",
    "flight": "travel_agent.mcp_servers.flight_server.server",
    "hotel": "travel_agent.mcp_servers.hotel_server.server",
}

# 业务语义:server 上的工具名,契约见 mcp_servers/*/contracts.py
TOOL_SEARCH_FLIGHTS = "search_flights"
TOOL_SEARCH_HOTELS = "search_hotels"
TOOL_GET_FORECAST = "get_forecast"

_LOG_DIR = Path(settings.trace_dir).parent / "logs"   # data/logs


class ToolExecutionError(Exception):
    """MCP 工具返回 is_error=True:服务端业务错误(参数校验不过等)。

    属"不可重试"类(同样的参数再调也是同样的错),直接向上抛。
    """

    def __init__(self, server: str, tool: str, detail: str):
        self.server = server
        self.tool = tool
        super().__init__(f"[{server}.{tool}] {detail}")


def _server_log_file(server: str) -> Path:
    """子进程 stderr 重定向目标(data/logs/{server}_server.log)。"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR / f"{server}_server.log"


def _subprocess_env() -> dict[str, str]:
    """传给 MCP 子进程的额外环境变量。

    mcp SDK 拉起子进程时只继承白名单变量(PATH/SYSTEMROOT 等),
    不会全量继承父进程环境 —— MOCK_FAULT 故障注入必须显式传递,
    否则评测/测试的故障注入悄悄失效。
    """
    env: dict[str, str] = {}
    fault = os.environ.get("MOCK_FAULT")
    if fault:
        env["MOCK_FAULT"] = fault
    return env


def _error_detail(result: Any) -> str:
    """从工具结果里提取错误信息(raise_on_error=False 时错误不抛异常)。"""
    try:
        if getattr(result, "error", None) is not None:
            return str(result.error)
        content = getattr(result, "content", None)
        if content:
            return " ".join(str(getattr(c, "text", c)) for c in content)
    except Exception:
        pass
    return "工具返回错误(无详情)"


async def _call_server(server: str, tool: str, arguments: dict) -> Any:
    """拉起子进程 → 调工具 → 返回 structured_content(纯 dict/list)。"""
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", SERVER_MODULES[server]],
        env=_subprocess_env(),             # 显式传递 MOCK_FAULT 等(白名单继承之外的变量)
        keep_alive=False,                  # 现起现用:会话结束即关子进程
        log_file=_server_log_file(server), # 横幅/日志进文件,不污染 CLI
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    if getattr(result, "is_error", False):
        raise ToolExecutionError(server, tool, _error_detail(result))
    return result.structured_content


def call_tool_sync(server: str, tool: str, arguments: dict | None = None) -> Any:
    """同步入口:Supervisor 节点直接调;内部 asyncio.run 跑六件套链。

    每次调用新建事件循环(与"现起现用"的子进程生命周期一致),
    Windows 下 asyncio.run 重复调用是安全的。
    """
    arguments = arguments or {}

    async def invoke():
        return await _call_server(server, tool, arguments)

    return asyncio.run(wrap_tool_call(
        f"{server}.{tool}", invoke,
        tool_args=arguments, breaker=server,   # 熔断按 server 粒度
        output=lambda content: _tool_output_summary(server, content),
    ))


def _tool_output_summary(server: str, content: dict | list | None) -> dict | None:
    """工具结果摘要写进 trace(评测指标与事实从 trace 提取)。

    不落全量数据(留痕原则:摘要够用即可);酒店类额外记
    "availability 全 0"(评测 error-02 的故障自检事实)。
    """
    if content is None:
        return None
    items = content if isinstance(content, list) else content.get("result", [])
    if not isinstance(items, list):
        items = [items]
    summary: dict[str, Any] = {"count": len(items)}
    if server == "hotel" and items:
        summary["availability_all_zero"] = all(
            i.get("availability", 0) == 0 for i in items)
    return summary


T = TypeVar("T")


def _to_models(raw: Any, model: type[T]) -> list[T]:
    """structured_content → 契约 Pydantic 模型列表。

    MCP 返回 JSON 兼容数据(dict/list),而 Agent 层吃 Pydantic 模型;
    还原时顺带校验:server 返回结构不对,这里立刻报错而不是
    把脏数据传给下游。
    """
    if raw is None:
        return []
    # fastmcp 对"返回列表"的工具把结果包成 {"result": [...]};
    # 返回单对象的工具则直接是字段 dict(契约模型无 "result" 字段,安全)
    if isinstance(raw, dict) and "result" in raw:
        raw = raw["result"]
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [model.model_validate(item) for item in items]


# ── 领域取数函数(Supervisor 用;参数与 MCP 工具契约一一对应)──

def fetch_flights(requirement: TripRequirement) -> list[FlightOption]:
    """按需求查航班(契约:search_flights(origin, destination, date, travelers))。"""
    return _to_models(call_tool_sync("flight", TOOL_SEARCH_FLIGHTS, {
        "origin": requirement.origin_city or "上海",
        "destination": requirement.destination,
        "date": requirement.start_date,
        "travelers": requirement.travelers or 1,
    }), FlightOption)


def fetch_hotels(requirement: TripRequirement) -> list[HotelOption]:
    """按需求查酒店(契约:search_hotels(city, checkin, checkout, guests))。"""
    return _to_models(call_tool_sync("hotel", TOOL_SEARCH_HOTELS, {
        "city": requirement.destination,
        "checkin": requirement.start_date,
        "checkout": requirement.end_date,
        "guests": requirement.travelers or 1,
    }), HotelOption)


def fetch_weather(requirement: TripRequirement) -> list[DailyWeather]:
    """按需求查天气(契约:get_forecast(city, start_date, end_date))。"""
    return _to_models(call_tool_sync("weather", TOOL_GET_FORECAST, {
        "city": requirement.destination,
        "start_date": requirement.start_date,
        "end_date": requirement.end_date,
    }), DailyWeather)
