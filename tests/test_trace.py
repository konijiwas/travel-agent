"""M7 留痕/回放/CLI 测试:JsonlWriter、Store 四表、trace 全链路落盘、chat 闭环。

trace 目录在夹具里指到临时目录(tmp_path):测试不污染 data/。
端到端用例会真实拉起 MCP 子进程(每次约 1-2s),故仅各一条。
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from travel_agent.config import settings
from travel_agent.main import app
from travel_agent.memory import jsonl_writer
from travel_agent.memory.jsonl_writer import JsonlWriter, read_trace
from travel_agent.memory.store import Store
from travel_agent.schemas.message import AgentMessage, MessageType
from travel_agent.schemas.trace import TraceEvent, new_trace_id


@pytest.fixture(autouse=True)
def isolated_trace_dir(tmp_path, monkeypatch):
    """trace 目录 → 临时目录,并重置 writer 缓存(缓存持有旧目录)。"""
    monkeypatch.setattr(settings, "trace_dir", str(tmp_path / "traces"))
    jsonl_writer.get_writer.cache_clear()
    yield
    jsonl_writer.get_writer.cache_clear()   # 测试后重建,回到真实配置目录


def _msg(key="k1") -> AgentMessage:
    return AgentMessage(
        type=MessageType.TASK_DISPATCH,
        topic="task.analyze",
        from_agent="supervisor",
        to_agent="analyst",
        payload={"x": 1},
        idempotency_key=key,
    )


# ── JsonlWriter ──

def test_jsonl_append_read_roundtrip(tmp_path):
    """写入 dict 与 Pydantic 模型,读回内容一致、顺序不变。"""
    w = JsonlWriter(tmp_path)
    w.append("a.jsonl", {"n": 1, "中": "文"})
    w.append("a.jsonl", TraceEvent(trace_id="t1", kind="cli", name="run"))
    records = w.read("a.jsonl")
    assert len(records) == 2
    assert records[0] == {"n": 1, "中": "文"}
    assert records[1]["trace_id"] == "t1"
    assert records[1]["kind"] == "cli"


def test_jsonl_read_missing_file(tmp_path):
    """文件不存在:返回空列表(回放前探测的约定)。"""
    assert JsonlWriter(tmp_path).read("nope.jsonl") == []


def test_jsonl_skips_corrupt_line(tmp_path):
    """损坏行被跳过,不影响其余行(JSONL 容错)。"""
    path = tmp_path / "b.jsonl"
    path.write_text('{"ok": 1}\n这不是JSON\n{"ok": 2}\n', encoding="utf-8")
    records = JsonlWriter(tmp_path).read("b.jsonl")
    assert [r["ok"] for r in records] == [1, 2]


# ── Store 四表 ──

def test_store_session_lifecycle(tmp_path):
    """sessions:创建(running)→ 更新状态(delivered)→ 列表可见。"""
    store = Store(tmp_path / "t.db")
    store.save_session("s1", "去杭州玩", "running")
    store.update_session_status("s1", "delivered")
    rows = store.list_sessions()
    assert len(rows) == 1
    assert rows[0][0] == "s1"
    assert rows[0][2] == "delivered"        # status 已更新
    store.close()


def test_store_message_idempotent_by_key(tmp_path):
    """同一幂等 key 重复落库:只留一行(OR IGNORE + UNIQUE 双保险)。"""
    store = Store(tmp_path / "t.db")
    store.save_message(_msg(key="same-key"), "s1")
    store.save_message(_msg(key="same-key"), "s1")   # 重发
    n = store._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE idempotency_key='same-key'").fetchone()[0]
    assert n == 1
    store.close()


def test_store_decision_roundtrip(tmp_path):
    """decisions:写入(JSON 序列化)→ 按 session 查回,内容可逆。"""
    store = Store(tmp_path / "t.db")
    store.save_decision("d1", "s1", "budget_overrun", {"overrun_cny": 500})
    rows = store.decisions_for("s1")
    assert len(rows) == 1
    assert rows[0][0] == "budget_overrun"
    assert json.loads(rows[0][1]) == {"overrun_cny": 500}
    assert store.decisions_for("s-other") == []
    store.close()


def test_store_trace_dedup_by_span_id(tmp_path):
    """traces:同 span_id 重复写入只留一行(幂等镜像)。"""
    store = Store(tmp_path / "t.db")
    ev = TraceEvent(trace_id="t1", kind="gate", name="quality_gate")
    store.save_trace(ev)
    store.save_trace(ev)
    n = store._conn.execute(
        "SELECT COUNT(*) FROM traces WHERE span_id=?", (ev.span_id,)).fetchone()[0]
    assert n == 1
    store.close()


# ── trace 全链路落盘(真实 MCP 子进程,仅一条)──

def test_graph_run_writes_full_trace(tmp_path):
    """一次完整规划:node/tool/llm 三类打点全部落盘,且可读回。"""
    from travel_agent.memory.checkpointer import build_checkpointer
    from travel_agent.supervisor.graph import run_trip

    result = run_trip(
        "从上海出发,2026-10-10去杭州玩2天,2人,预算6000",
        checkpointer=build_checkpointer(in_memory=True),
    )
    assert result.get("status") == "delivered"

    files = list(Path(settings.trace_dir).glob("session_*.jsonl"))
    assert len(files) == 1
    trace_id = files[0].stem.replace("session_", "")
    # 回归:trace 文件名必须等于会话 id(曾经因 ContextVar 在节点内
    # set 不生效,trace 落到默认 tracer 的文件里,导致回放找不到)
    assert trace_id == result.get("session_id")
    events = read_trace(trace_id)
    assert len(events) >= 10

    kinds = {e["kind"] for e in events}
    assert {"node", "tool", "llm"} <= kinds      # 三类打点齐备
    names = {e["name"] for e in events}
    assert "node.plan_budget" in names
    assert "flight.search_flights" in names
    assert "node.quality_gate" in names

    # 工具事件全部 ok(本次无故障注入);父子链:工具 span 挂在节点 span 下
    tool_events = [e for e in events if e["kind"] == "tool"]
    assert all(e["status"] == "ok" for e in tool_events)
    assert all(e["parent_span_id"] for e in tool_events)

    # 节点顺序:receive_input 是第一个节点打点
    node_names = [e["name"] for e in events if e["kind"] == "node"]
    assert node_names[0] == "node.receive_input"
    assert node_names[-1] == "node.deliver"


# ── CLI 三命令(真实 MCP 子进程,仅一条)──

def test_cli_chat_replay_config_roundtrip():
    """chat 完整闭环(追问→回答→交付)→ 用打印的 session 回放 → config show。"""
    runner = CliRunner()
    r = runner.invoke(app, ["chat"],
                      input="从上海出发,2026-10-10去杭州玩2天,2人\n预算6000\n")
    assert r.exit_code == 0, r.output
    assert "状态: delivered" in r.output
    assert "[追问]" in r.output              # 缺预算触发了追问
    m = re.search(r"session: ([0-9a-f-]+)", r.output)
    assert m, f"输出中没有 session id:\n{r.output}"
    trace_id = m.group(1)

    r2 = runner.invoke(app, ["replay", trace_id])
    assert r2.exit_code == 0, r2.output
    assert "node.plan_budget" in r2.output
    assert "flight.search_flights" in r2.output

    r3 = runner.invoke(app, ["config", "show"])
    assert r3.exit_code == 0
    assert "eval_mode" in r3.output
    assert "cost_limit_usd" in r3.output

    r4 = runner.invoke(app, ["replay", "不存在的trace"])
    assert r4.exit_code == 1                 # 找不到 → 退出码 1 并提示
