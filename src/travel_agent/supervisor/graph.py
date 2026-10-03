"""Supervisor 状态机:LangGraph StateGraph 装配 + 九节点 + 条件路由 + 队列接入。

数据获取当前直接调 mock 实现(M7 起经 MCP 工具 + 可靠性包装统一获取)。
interrupt() 挂起(追问/确认)依赖 checkpointer:编译时必传。
"""

import logging
import uuid
from datetime import datetime

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from travel_agent.agents.analyst.agent import Analyst
from travel_agent.agents.budget.agent import BudgetPlanner
from travel_agent.agents.executor.agent import Executor
from travel_agent.config import settings
from travel_agent.memory.checkpointer import build_checkpointer
from travel_agent.queue.factory import get_queue
from travel_agent.schemas.message import (
    EVENT_BUDGET_OVERRUN,
    EVENT_WEATHER_CHANGED,
    AgentMessage,
    MessageType,
    StreamName,
    make_idempotency_key,
)
from travel_agent.supervisor.events import collect_events, publish_event
from travel_agent.supervisor.quality_gate import check_quality
from travel_agent.supervisor.state import ConfirmationRequest, FinalOutput, SupervisorState

logger = logging.getLogger(__name__)


# ── 数据获取(M7 起换 MCP 工具 + 可靠性包装)──

def _fetch_data(requirement):
    """拉取规划所需数据:直接调 mock 实现(接口与 MCP 工具一致)。"""
    from travel_agent.mcp_servers.flight_server import mock_data as flight_mock
    from travel_agent.mcp_servers.hotel_server import mock_data as hotel_mock
    from travel_agent.mcp_servers.weather_server import mock_data as weather_mock

    origin = requirement.origin_city or "上海"
    flights = flight_mock.search(origin, requirement.destination,
                                 requirement.start_date, requirement.travelers or 1)
    hotels = hotel_mock.search(requirement.destination, requirement.start_date,
                               requirement.end_date, requirement.travelers or 1)
    weather = weather_mock.forecast(requirement.destination,
                                    requirement.start_date, requirement.end_date)
    return flights, hotels, weather


# ── 节点实现 ──

def _make_receive_input(queue):
    def receive_input(state: SupervisorState) -> dict:
        session_id = state.get("session_id") or str(uuid.uuid4())
        round_no = state.get("round_count", 0) + 1
        queue.publish(
            AgentMessage(
                type=MessageType.TASK_DISPATCH,
                topic="task.start",
                from_agent="supervisor",
                to_agent="supervisor",
                payload={"user_input": state.get("user_input", "")},
                idempotency_key=make_idempotency_key("task.start", session_id, round_no),
            ),
            stream=StreamName.TASKS.value,
        )
        return {"session_id": session_id, "round_count": round_no}
    return receive_input


def _make_collect_events(queue):
    def collect_events_node(state: SupervisorState) -> dict:
        # 追加语义:operator.add 归约,历史事件不覆盖
        return {"events": collect_events(queue)}
    return collect_events_node


def _make_analyze():
    analyst = Analyst()

    def analyze(state: SupervisorState) -> dict:
        out = analyst.analyze(state.get("user_input", ""))
        if out.requirement is not None:
            return {"requirement": out.requirement, "pending_questions": []}
        return {"requirement": None, "pending_questions": out.clarify_questions,
                "errors": [f"需求不完整:{out.missing_fields}"]}
    return analyze


def clarify(state: SupervisorState) -> dict:
    """interrupt 挂起:把追问问题交给 CLI 展示,等用户回答后续跑。"""
    questions = state.get("pending_questions") or []
    answer = interrupt({
        "kind": "clarify",
        "title": "需要补充以下信息",
        "questions": [q.question for q in questions],
    })
    return {
        "user_input": f"{state.get('user_input', '')}\n补充:{answer}",
        "round_count": state.get("round_count", 0) + 1,
    }


def _make_plan_budget(queue):
    planner = BudgetPlanner()

    def plan_budget(state: SupervisorState) -> dict:
        req = state.get("requirement")
        if req is None:
            return {"errors": ["需求不完整且追问轮次用尽"], "status": "failed"}
        flights, hotels, _ = _fetch_data(req)
        plan = planner.plan_budget(req, flights, hotels)
        if plan.needs_confirmation:
            prop = plan.pending_proposal
            detail = (f"已按优先级链①②③调整后仍超支 {prop.overrun_cny:.0f} 元"
                      f"(预算上限 {req.budget_range.max_cny:.0f} 元)。"
                      f"{prop.note}")
            publish_event(queue, EVENT_BUDGET_OVERRUN, "budget",
                          {"overrun_cny": prop.overrun_cny},
                          state.get("session_id", ""), state.get("round_count", 1))
            return {
                "budget_plan": plan,
                "pending_confirmation": ConfirmationRequest(
                    kind="budget", title="预算超支待确认",
                    detail=detail, payload=prop.model_dump(mode="json")),
            }
        return {"budget_plan": plan, "pending_confirmation": None}
    return plan_budget


def confirm_decision(state: SupervisorState) -> dict:
    """interrupt 挂起:展示待确认方案;y 接受 / r 或 n 带意见回退重规划。"""
    pending = state.get("pending_confirmation")
    if pending is None:
        return {"confirm_result": "accepted"}
    answer = interrupt({
        "kind": "confirm",
        "title": pending.title,
        "detail": pending.detail,
        "options": "y 同意 / r 修改 / n 不同意",
    })
    text = str(answer).strip().lower()
    if text.startswith("y") or "同意" in text:
        return {"pending_confirmation": None, "confirm_result": "accepted"}
    return {
        "pending_confirmation": None,
        "confirm_result": "rejected",
        "user_input": f"{state.get('user_input', '')}\n修改意见:{answer}",
        "round_count": state.get("round_count", 0) + 1,
    }


def _make_plan_itinerary(queue):
    executor = Executor()

    def plan_itinerary(state: SupervisorState) -> dict:
        req = state.get("requirement")
        if req is None:
            return {"errors": ["需求缺失,无法编排行程"], "status": "failed"}
        flights, hotels, weather = _fetch_data(req)
        it = executor.build_itinerary(req, state.get("budget_plan"), weather,
                                      flights, hotels)
        if it.weather_changes:
            publish_event(queue, EVENT_WEATHER_CHANGED, "executor",
                          {"changes": [c.model_dump(mode="json") for c in it.weather_changes]},
                          state.get("session_id", ""), state.get("round_count", 1))
        return {"itinerary": it}
    return plan_itinerary


def quality_gate(state: SupervisorState) -> dict:
    """质量门:通过 → 交付;首次失败 → 回退重试;重试仍败 → 降级交付。"""
    report = check_quality(state)
    if report.passed:
        return {"quality": report}
    retry_count = state.get("quality_retry_count", 0)
    if retry_count >= settings.max_retry_nodes:
        report.retried = True
        report.notes.append("回退重试后仍未通过,降级交付(缺陷已在报告中标注)")
        return {"quality": report}
    report.notes.append(f"质量门未通过,回退重试(第 {retry_count + 1} 次)")
    return {"quality": report, "quality_retry_count": retry_count + 1}


def _make_deliver(queue):
    def deliver(state: SupervisorState) -> dict:
        summary_parts = []
        bp = state.get("budget_plan")
        if bp is not None:
            if bp.adjustments:
                summary_parts.append(f"预算调整 {len(bp.adjustments)} 条(四要素已记录)")
            if bp.needs_confirmation:
                summary_parts.append("预算超支方案已提交用户确认")
        it = state.get("itinerary")
        if it is not None:
            if it.weather_changes:
                summary_parts.append(f"天气变更 {len(it.weather_changes)} 条(三要素已记录)")
            if it.conflicts:
                summary_parts.append(f"时间冲突 {len(it.conflicts)} 条(均已解决)")
        quality = state.get("quality")
        if quality is not None and quality.retried:
            summary_parts.append("质量门降级交付(详见报告)")

        output = FinalOutput(
            itinerary=it,
            budget_plan=bp,
            summary=";".join(summary_parts) or "无调整",
            data_timestamp=datetime.now().isoformat(timespec="seconds"),
        )
        session_id = state.get("session_id", "")
        queue.publish(
            AgentMessage(
                type=MessageType.RESULT_RETURN,
                topic="result.deliver",
                from_agent="supervisor",
                to_agent="user",
                payload={"summary": output.summary},
                idempotency_key=make_idempotency_key(
                    "result.deliver", session_id, state.get("round_count", 1)),
            ),
            stream=StreamName.RESULTS.value,
        )
        return {"final_output": output, "status": "delivered"}
    return deliver


# ── 条件路由 ──

def route_after_analyze(state: SupervisorState) -> str:
    if state.get("requirement") is not None:
        return "plan_budget"
    if state.get("round_count", 0) >= settings.max_clarify_rounds:
        return "deliver"                       # 追问轮次用尽:降级交付
    return "clarify"


def route_after_budget(state: SupervisorState) -> str:
    if state.get("status") == "failed":
        return "deliver"
    # 用户已确认过的方案不再重复询问(重规划回退时跳过 confirm)
    if (state.get("pending_confirmation") is not None
            and not state.get("auto_adjust", False)
            and state.get("confirm_result") != "accepted"):
        return "confirm_decision"
    return "plan_itinerary"


def route_after_confirm(state: SupervisorState) -> str:
    return "plan_itinerary" if state.get("confirm_result") == "accepted" else "plan_budget"


def route_after_gate(state: SupervisorState) -> str:
    quality = state.get("quality")
    if quality is None or quality.passed or quality.retried:
        return "deliver"                       # 通过 / 重试耗尽 → 交付
    checks = quality.checks
    if not checks.get("requirement_complete", True):
        return "analyze"
    if not (checks.get("budget_deviation", True)
            and checks.get("adjustments_traceable", True)
            and checks.get("data_sources_noted", True)):
        return "plan_budget"
    return "plan_itinerary"


# ── 装配 ──

def build_graph(checkpointer=None, queue=None):
    """构建并编译 Supervisor 状态机(checkpointer 默认 SQLite 持久化)。

    queue 参数供测试/CLI 注入自定义队列实例(默认按配置工厂创建)。
    """
    queue = queue or get_queue()
    builder = StateGraph(SupervisorState)
    builder.add_node("receive_input", _make_receive_input(queue))
    builder.add_node("collect_events", _make_collect_events(queue))
    builder.add_node("analyze", _make_analyze())
    builder.add_node("clarify", clarify)
    builder.add_node("plan_budget", _make_plan_budget(queue))
    builder.add_node("confirm_decision", confirm_decision)
    builder.add_node("plan_itinerary", _make_plan_itinerary(queue))
    builder.add_node("quality_gate", quality_gate)
    builder.add_node("deliver", _make_deliver(queue))

    builder.add_edge(START, "receive_input")
    builder.add_edge("receive_input", "collect_events")
    builder.add_edge("collect_events", "analyze")
    builder.add_conditional_edges(
        "analyze", route_after_analyze,
        {"clarify": "clarify", "plan_budget": "plan_budget", "deliver": "deliver"})
    builder.add_edge("clarify", "analyze")
    builder.add_conditional_edges(
        "plan_budget", route_after_budget,
        {"confirm_decision": "confirm_decision",
         "plan_itinerary": "plan_itinerary", "deliver": "deliver"})
    builder.add_conditional_edges(
        "confirm_decision", route_after_confirm,
        {"plan_itinerary": "plan_itinerary", "plan_budget": "plan_budget"})
    builder.add_edge("plan_itinerary", "quality_gate")
    builder.add_conditional_edges(
        "quality_gate", route_after_gate,
        {"deliver": "deliver", "analyze": "analyze",
         "plan_budget": "plan_budget", "plan_itinerary": "plan_itinerary"})
    builder.add_edge("deliver", END)

    checkpointer = checkpointer or build_checkpointer()
    return builder.compile(checkpointer=checkpointer)


def run_trip(user_input: str, session_id: str | None = None,
             auto_adjust: bool | None = None, checkpointer=None) -> dict:
    """运行一次行程规划(单轮入口)。

    interrupt 挂起时返回状态含 __interrupt__ 键(本版本 langgraph 行为),
    调用方用 get_interrupt() 取载荷展示,再用 graph.invoke(Command(resume=回答), config) 续跑。
    """
    graph = build_graph(checkpointer)
    config = {"configurable": {"thread_id": session_id or str(uuid.uuid4())}}
    return graph.invoke(
        {
            "user_input": user_input,
            "auto_adjust": settings.auto_adjust if auto_adjust is None else auto_adjust,
        },
        config,
    )


def get_interrupt(state: dict) -> dict | None:
    """从返回状态中提取 interrupt 载荷(追问/确认挂起时非 None)。"""
    items = state.get("__interrupt__") or []
    return items[0].value if items else None
