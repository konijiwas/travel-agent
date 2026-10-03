"""M6 Supervisor 测试:质量门单测 + 端到端(全流程 / 追问挂起 / 确认挂起 / 队列消息)。"""

from langgraph.types import Command

from travel_agent.memory.checkpointer import build_checkpointer
from travel_agent.queue.memory import InMemoryQueue
from travel_agent.schemas.budget import BudgetItem, BudgetPlan
from travel_agent.schemas.itinerary import Activity, Conflict, Itinerary, ItineraryDay
from travel_agent.schemas.message import MessageType, StreamName
from travel_agent.schemas.requirement import BudgetRange, TripRequirement
from travel_agent.supervisor.graph import build_graph
from travel_agent.supervisor.quality_gate import check_quality


# ── 质量门单测(纯函数,直接构造状态)──

def _req(max_cny=6000.0) -> TripRequirement:
    return TripRequirement(
        destination="杭州", origin_city="上海",
        start_date="2026-10-01", end_date="2026-10-03", travelers=2,
        budget_range=BudgetRange(min_cny=0.0, max_cny=max_cny),
    )


def _budget(total: float) -> BudgetPlan:
    return BudgetPlan(
        total_budget=total,
        items=[BudgetItem(category="transport", amount=total, note="MU5131 往返依据")],
    )


def _itinerary(outdoor=True, weather_note="晴 26°C", conflicts=None) -> Itinerary:
    acts = [Activity(name="西湖游船", start_time="09:00", end_time="10:00",
                     kind="outdoor" if outdoor else "indoor")]
    return Itinerary(
        days=[ItineraryDay(date="2026-10-01", activities=acts,
                           weather_note=weather_note)],
        conflicts=conflicts or [],
    )


def test_gate_passes_complete_state():
    state = {"requirement": _req(), "budget_plan": _budget(5000.0),
             "itinerary": _itinerary()}
    report = check_quality(state)
    assert report.passed is True


def test_gate_fails_incomplete_requirement():
    report = check_quality({"requirement": None})
    assert report.passed is False
    assert report.failure_type == "模型能力"
    assert report.checks["requirement_complete"] is False


def test_gate_fails_budget_deviation():
    """预算 6000,总价 8000 → 超过 10% 上限,拦截。"""
    state = {"requirement": _req(6000.0), "budget_plan": _budget(8000.0),
             "itinerary": _itinerary()}
    report = check_quality(state)
    assert report.passed is False
    assert report.checks["budget_deviation"] is False


def test_gate_exempts_user_accepted_overrun():
    """用户已确认超支方案:人工决策豁免自动规则。"""
    state = {"requirement": _req(6000.0), "budget_plan": _budget(8000.0),
             "itinerary": _itinerary(), "confirm_result": "accepted"}
    report = check_quality(state)
    assert report.passed is True


def test_gate_fails_unresolved_conflict():
    state = {"requirement": _req(), "budget_plan": _budget(5000.0),
             "itinerary": _itinerary(conflicts=[Conflict(
                 day="2026-10-01", description="游船与航班重叠", resolution="")])}
    report = check_quality(state)
    assert report.passed is False
    assert report.checks["no_unresolved_conflicts"] is False


def test_gate_fails_missing_weather_note():
    """有室外活动但无天气说明 → 拦截。"""
    state = {"requirement": _req(), "budget_plan": _budget(5000.0),
             "itinerary": _itinerary(outdoor=True, weather_note="")}
    report = check_quality(state)
    assert report.passed is False
    assert report.checks["weather_notes_present"] is False


# ── 端到端(整图,内存队列 + 内存检查点)──

def _graph():
    return build_graph(
        checkpointer=build_checkpointer(in_memory=True),
        queue=InMemoryQueue(),
    )


def _invoke(graph, thread_id, user_input):
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke({"user_input": user_input, "auto_adjust": False}, config), config


def test_end_to_end_full_run():
    graph = _graph()
    result, _ = _invoke(graph, "e2e-1",
                        "从上海出发,2026-10-01 到 2026-10-03 去杭州玩,2人,预算20000")
    assert result["status"] == "delivered"
    assert len(result["itinerary"].days) == 3
    assert result["quality"].passed is True
    assert result["final_output"].summary


def test_end_to_end_clarify_interrupt_and_resume():
    """信息缺失 → interrupt 挂起(__interrupt__ 键)→ 补充后 resume → 完整交付。"""
    graph = _graph()
    config = {"configurable": {"thread_id": "e2e-2"}}
    result = graph.invoke({"user_input": "想去杭州玩", "auto_adjust": False}, config)
    assert "__interrupt__" in result                      # 挂起等待补充
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "clarify"
    assert payload["questions"]
    result = graph.invoke(
        Command(resume="从上海出发,2026-10-01 到 2026-10-03 去,2人,预算20000"),
        config)
    assert result["status"] == "delivered"
    assert result["requirement"].destination == "杭州"
    assert result["requirement"].travelers == 2


def test_end_to_end_budget_confirm_interrupt_and_accept():
    """预算严重不足 → 确认挂起 → 用户同意 → 交付(质量门豁免人工确认)。"""
    graph = _graph()
    config = {"configurable": {"thread_id": "e2e-3"}}
    result = graph.invoke({"user_input": "从上海出发,2026-10-01 到 2026-10-03 去杭州玩,2人,预算1000",
                           "auto_adjust": False}, config)
    assert "__interrupt__" in result                      # 挂起等待确认
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "confirm"
    result = graph.invoke(Command(resume="y"), config)
    assert result["status"] == "delivered"
    assert result["confirm_result"] == "accepted"
    # 用户确认的超支方案,预算调整留痕
    assert result["budget_plan"].adjustments


def test_end_to_end_queue_messages_emitted():
    """三类消息真实流转:tasks 有派发,results 有交付。"""
    q = InMemoryQueue()
    graph = build_graph(checkpointer=build_checkpointer(in_memory=True), queue=q)
    config = {"configurable": {"thread_id": "e2e-4"}}
    result = graph.invoke(
        {"user_input": "从上海出发,2026-10-01 到 2026-10-03 去杭州玩,2人,预算20000",
         "auto_adjust": False}, config)
    assert result["status"] == "delivered"

    tasks = q.consume(StreamName.TASKS.value, "g", "c", count=10, block_ms=50)
    assert tasks and tasks[0][1].type == MessageType.TASK_DISPATCH
    results = q.consume(StreamName.RESULTS.value, "g", "c", count=10, block_ms=50)
    assert results and results[0][1].topic == "result.deliver"
