"""M2 契约测试:三个 mock MCP Server 的工具清单 / 返回结构 / 确定性 / 故障注入。

契约失败 = 与真实 API 的对齐层被破坏,需同步 schemas 与文档(AGENTS.md)。
"""

from fastmcp import Client

from travel_agent.mcp_servers.flight_server.server import mcp as flight_mcp
from travel_agent.mcp_servers.hotel_server.server import mcp as hotel_mcp
from travel_agent.mcp_servers.weather_server.server import mcp as weather_mcp


async def _tool_names(client) -> set[str]:
    tools = await client.list_tools()
    return {t.name for t in tools}


# ── 工具清单 ──

async def test_weather_tool_list(extract):
    async with Client(weather_mcp) as client:
        names = await _tool_names(client)
        assert {"get_current_weather", "get_forecast"} <= names


async def test_flight_tool_list(extract):
    async with Client(flight_mcp) as client:
        names = await _tool_names(client)
        assert {"search_flights", "get_flight_status"} <= names


async def test_hotel_tool_list(extract):
    async with Client(hotel_mcp) as client:
        names = await _tool_names(client)
        assert {"search_hotels", "check_availability"} <= names


# ── 天气:结构与确定性 ──

async def test_weather_contract_fields(extract):
    async with Client(weather_mcp) as client:
        r = extract(await client.call_tool(
            "get_current_weather", {"city": "杭州", "date": "2026-10-01"}))
    for field in ("city", "date", "condition", "temp_c", "wind_kmh",
                  "humidity_pct", "is_outdoor_friendly", "source_ts"):
        assert field in r, f"缺少字段 {field}"
    assert r["city"] == "杭州"
    # 一致性:is_outdoor_friendly 必须与 condition 对应
    assert r["is_outdoor_friendly"] == (r["condition"] in {"晴", "多云"})


async def test_weather_deterministic(extract):
    """同参数两次调用结果相同(确定性 mock 的核心保证)。"""
    args = {"city": "成都", "date": "2026-10-05"}
    async with Client(weather_mcp) as client:
        r1 = extract(await client.call_tool("get_current_weather", args))
        r2 = extract(await client.call_tool("get_current_weather", args))
    assert r1["condition"] == r2["condition"]
    assert r1["temp_c"] == r2["temp_c"]
    assert r1["is_outdoor_friendly"] == r2["is_outdoor_friendly"]


async def test_weather_forecast_range(extract):
    async with Client(weather_mcp) as client:
        r = extract(await client.call_tool(
            "get_forecast", {"city": "三亚", "start_date": "2026-10-01", "end_date": "2026-10-03"}))
    assert len(r) == 3
    assert [d["date"] for d in r] == ["2026-10-01", "2026-10-02", "2026-10-03"]


# ── 航班:结构与确定性 ──

async def test_flight_search_contract(extract):
    args = {"origin": "上海", "destination": "杭州", "date": "2026-10-03", "travelers": 3}
    async with Client(flight_mcp) as client:
        flights = extract(await client.call_tool("search_flights", args))
    assert 3 <= len(flights) <= 5
    for f in flights:
        assert f["flight_no"] and f["airline"]
        assert f["origin"] == "上海" and f["destination"] == "杭州"
        assert f["departure_ts"] < f["arrival_ts"]
        assert f["price_cny"] > 0
        assert f["availability"] > 0
        assert 0.0 <= f["score"] <= 1.0
        assert "source_ts" in f


async def test_flight_search_deterministic(extract):
    args = {"origin": "北京", "destination": "三亚", "date": "2026-11-01", "travelers": 2}
    async with Client(flight_mcp) as client:
        r1 = extract(await client.call_tool("search_flights", args))
        r2 = extract(await client.call_tool("search_flights", args))
    assert [f["flight_no"] for f in r1] == [f["flight_no"] for f in r2]
    assert [f["price_cny"] for f in r1] == [f["price_cny"] for f in r2]


async def test_flight_status_contract(extract):
    async with Client(flight_mcp) as client:
        r = extract(await client.call_tool(
            "get_flight_status", {"flight_no": "MU5131", "date": "2026-10-03"}))
    assert r["status"] in {"on_time", "delayed", "cancelled"}
    if r["status"] == "delayed":
        assert r["delay_minutes"] > 0


# ── 酒店:结构与确定性 ──

async def test_hotel_search_contract(extract):
    args = {"city": "杭州", "checkin": "2026-10-03", "checkout": "2026-10-05", "guests": 2}
    async with Client(hotel_mcp) as client:
        hotels = extract(await client.call_tool("search_hotels", args))
    assert len(hotels) >= 2
    for h in hotels:
        assert h["hotel_id"] and h["name"] and h["city"] == "杭州"
        assert h["price_per_night_cny"] > 0
        assert 4.0 <= h["rating"] <= 5.0
        assert h["amenities"]
        assert "source_ts" in h


async def test_hotel_max_price_filter(extract):
    async with Client(hotel_mcp) as client:
        hotels = extract(await client.call_tool(
            "search_hotels", {"city": "上海", "checkin": "2026-10-10",
                              "checkout": "2026-10-12", "guests": 1, "max_price": 800.0}))
    assert hotels and all(h["price_per_night_cny"] <= 800.0 for h in hotels)


async def test_hotel_availability_contract(extract):
    async with Client(hotel_mcp) as client:
        hotels = extract(await client.call_tool(
            "search_hotels", {"city": "西安", "checkin": "2026-10-10",
                              "checkout": "2026-10-11", "guests": 2}))
        hotel_id = hotels[0]["hotel_id"]
        r = extract(await client.call_tool(
            "check_availability", {"hotel_id": hotel_id,
                                   "checkin": "2026-10-10", "checkout": "2026-10-11"}))
    assert isinstance(r["available"], bool)
    assert r["remaining_rooms"] >= 0


# ── 故障注入(评测 error 用例的确定性通道)──

async def test_fault_hotel_no_room(extract, monkeypatch):
    monkeypatch.setenv("MOCK_FAULT", "hotel_no_room")
    async with Client(hotel_mcp) as client:
        hotels = extract(await client.call_tool(
            "search_hotels", {"city": "杭州", "checkin": "2026-10-03",
                              "checkout": "2026-10-05", "guests": 2}))
        r = extract(await client.call_tool(
            "check_availability", {"hotel_id": "HTL-杭州-1234",
                                   "checkin": "2026-10-03", "checkout": "2026-10-05"}))
    assert all(h["availability"] == 0 for h in hotels)
    assert r["available"] is False and r["remaining_rooms"] == 0


async def test_fault_weather_empty(extract, monkeypatch):
    monkeypatch.setenv("MOCK_FAULT", "weather_empty")
    async with Client(weather_mcp) as client:
        r = extract(await client.call_tool(
            "get_forecast", {"city": "杭州", "start_date": "2026-10-03", "end_date": "2026-10-05"}))
    assert r == []


async def test_invalid_city_rejected(extract):
    """参数错误应被服务端明确拒绝(校验层拦截)。"""
    async with Client(weather_mcp) as client:
        result = await client.call_tool(
            "get_current_weather", {"city": "不存在的城市", "date": "2026-10-01"},
            raise_on_error=False)
    assert result.is_error
