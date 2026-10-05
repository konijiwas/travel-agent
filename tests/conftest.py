"""共享 fixtures:故障注入清理、FastMCP 工具结果数据提取。"""

import dataclasses

import pytest
from pydantic import BaseModel


@pytest.fixture(autouse=True)
def no_fault(monkeypatch):
    """每个测试默认无故障注入,避免环境污染。"""
    monkeypatch.delenv("MOCK_FAULT", raising=False)


@pytest.fixture(autouse=True)
def fake_llm_mode(monkeypatch):
    """测试默认走 fake 离线模式,绝不调用真实 API(真实用例自行恢复)。"""
    from travel_agent.config import settings

    monkeypatch.setattr(settings, "eval_mode", "fake")


@pytest.fixture(autouse=True)
def fresh_trajectory(tmp_path, monkeypatch):
    """每个测试 = 一条独立轨迹:换新账本与 tracer。

    不走 run_trip/chat 入口直接调 graph.invoke 的测试,拿到的
    ContextVar 默认账本/tracer 是进程级共享的 —— 不换新会导致
    CostGuard 用量跨测试累计(撞 token 上限)与 trace 串扰。
    同时把 trace 目录指到临时目录:测试打点不污染 data/traces。
    """
    from travel_agent.config import settings
    from travel_agent.memory import jsonl_writer
    from travel_agent.reliability import CostGuard, Tracer, set_guard, set_tracer

    monkeypatch.setattr(settings, "trace_dir", str(tmp_path / "traces"))
    jsonl_writer.get_writer.cache_clear()
    set_guard(CostGuard())
    set_tracer(Tracer(write_db=False))
    yield
    jsonl_writer.get_writer.cache_clear()


def _to_plain(data):
    """工具结果 → 纯 dict/list,便于断言。

    fastmcp 4.x 把工具返回包装成动态生成的 dataclass(Root),
    用 dataclasses.asdict 转换;单字段 "root" 的包装直接解包。
    """
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        d = dataclasses.asdict(data)
        if set(d.keys()) == {"root"}:
            return _to_plain(d["root"])
        return {k: _to_plain(v) for k, v in d.items()}
    if isinstance(data, BaseModel):
        return data.model_dump()
    if isinstance(data, list):
        return [_to_plain(d) for d in data]
    if isinstance(data, dict):
        return {k: _to_plain(v) for k, v in data.items()}
    return data


def extract_data(result) -> object:
    """兼容 FastMCP CallToolResult 的数据提取(优先结构化 .data)。"""
    data = getattr(result, "data", None)
    if data is not None:
        return _to_plain(data)
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            try:
                import json

                return json.loads(text)
            except (ValueError, TypeError):
                return text
    return None


@pytest.fixture
def extract():
    return extract_data
