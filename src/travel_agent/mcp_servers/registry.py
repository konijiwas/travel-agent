"""MCP Server 注册表:stdio 启动配置,供 MCP 客户端(langchain-mcp-adapters)加载。

每个 server 以独立子进程启动,工具名自动带 `{server}__` 前缀。
"""

MCP_SERVERS: dict[str, dict] = {
    "weather": {
        "command": "uv",
        "args": ["run", "python", "-m", "travel_agent.mcp_servers.weather_server.server"],
        "transport": "stdio",
    },
    "flight": {
        "command": "uv",
        "args": ["run", "python", "-m", "travel_agent.mcp_servers.flight_server.server"],
        "transport": "stdio",
    },
    "hotel": {
        "command": "uv",
        "args": ["run", "python", "-m", "travel_agent.mcp_servers.hotel_server.server"],
        "transport": "stdio",
    },
}
