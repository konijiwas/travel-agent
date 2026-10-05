"""Supervisor 状态机:LangGraph StateGraph 装配 + 九节点 + 条件路由 + 队列接入。

数据获取(M7 起)经 tools.mcp_tools:stdio 子进程拉起 MCP Server,
每次调用穿可靠性六件套(权限→熔断→打点(超时→重试))。
interrupt() 挂起(追问/确认)依赖 checkpointer:编译时必传。
"""

import logging
import uuid
from datetime import datetime
from functools import wraps

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from travel_agent.agents.analyst.agent import Analyst
from travel_agent.agents.budget.agent import BudgetPlanner
from travel_agent.agents.executor.agent import Executor
from travel_agent.config import settings
from travel_agent.memory.checkpointer import build_checkpointer
from travel_agent.memory.store import get_store
from travel_agent.queue.factory import get_queue
from travel_agent.reliability import CostGuard, Tracer, get_tracer, set_guard, set_tracer
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
from travel_agent.tools.mcp_tools import fetch_flights, fetch_hotels, fetch_weather

logger = logging.getLogger(__name__)


# ── 数据获取(经 MCP 工具 + 六件套)──

def _fetch_data(requirement) -> tuple[list, list, list, list[str]]:
    """拉取规划所需数据:每个数据源独立 try,单源故障降级为空列表。

    返回 (flights, hotels, weather, errors):降级时 errors 带说明,
    由节点并入 state.errors(交付时可见);规划师有"无数据兜底"
    规则,空数据也能产出完整行程 —— 单源故障不拖垮全局。
    """
    errors: list[str] = []
    flights: list = []
    hotels: list = []
    weather: list = []
    try:
        flights = fetch_flights(requirement)
    except Exception as exc:
        errors.append(f"航班数据获取失败(已降级):{exc}")
    try:
        hotels = fetch_hotels(requirement)
    except Exception as exc:
        errors.append(f"酒店数据获取失败(已降级):{exc}")
    try:
        weather = fetch_weather(requirement)
    except Exception as exc:
        errors.append(f"天气数据获取失败(已降级):{exc}")
    return flights, hotels, weather, errors


def _traced(node_name: str, fn):
    """节点打点装饰器:每个节点包一个 span(记录耗时/状态,写 trace)。"""
    @wraps(fn)
    def wrapper(state: SupervisorState) -> dict:
        with get_tracer().span(
            "node", f"node.{node_name}",
            input={"round": state.get("round_count", 0),
                   "has_requirement": state.get("requirement") is not None},
        ):
            return fn(state)
    return wrapper


# ── 节点实现 ──

def _make_receive_input(queue):
    def receive_input(state: SupervisorState) -> dict:
        session_id = state.get("session_id") or str(uuid.uuid4())
        round_no = state.get("round_count", 0) + 1
        # 落 sessions 表。注意:tracer/guard 不能在节点里 set —— LangGraph
        # 每个节点在上下文副本里运行,节点内 set 只对自己生效;入口处
        # (run_trip / CLI 命令)set 的值会被副本继承,故统一在入口初始化。
        get_store().save_session(session_id, state.get("user_input", ""), "running")
        msg = AgentMessage(
            type=MessageType.TASK_DISPATCH,
            topic="task.start",
            from_agent="supervisor",
            to_agent="supervisor",
            payload={"user_input": state.get("user_input", "")},
            idempotency_key=make_idempotency_key("task.start", session_id, round_no),
        )
        queue.publish(msg, stream=StreamName.TASKS.value)
        get_store().save_message(msg, session_id)   # 消息留痕(幂等 key 去重)
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
        flights, hotels, _, fetch_errors = _fetch_data(req)
        plan = planner.plan_budget(req, flights, hotels)
        if plan.needs_confirmation:
            prop = plan.pending_proposal
            detail = (f"已按优先级链①②③调整后仍超支 {prop.overrun_cny:.0f} 元"
                      f"(预算上限 {req.budget_range.max_cny:.0f} 元)。"
                      f"{prop.note}")
            session_id = state.get("session_id", "")
            publish_event(queue, EVENT_BUDGET_OVERRUN, "budget",
                          {"overrun_cny": prop.overrun_cny},
                          session_id, state.get("round_count", 1))
            get_store().save_decision(
                f"budget_overrun:{session_id}:{state.get('round_count', 1)}",
                session_id, "budget_overrun", prop.model_dump(mode="json"))
            return {
                "budget_plan": plan,
                "errors": fetch_errors,
                "pending_confirmation": ConfirmationRequest(
                    kind="budget", title="预算超支待确认",
                    detail=detail, payload=prop.model_dump(mode="json")),
            }
        return {"budget_plan": plan, "errors": fetch_errors,
                "pending_confirmation": None}
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
    session_id = state.get("session_id", "")
    if text.startswith("y") or "同意" in text:
        get_store().save_decision(
            f"confirm:{session_id}:{state.get('round_count', 1)}",
            session_id, "confirm_accepted", {"answer": text})
        return {"pending_confirmation": None, "confirm_result": "accepted"}
    get_store().save_decision(
        f"confirm:{session_id}:{state.get('round_count', 1)}",
        session_id, "confirm_rejected", {"answer": text})
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
        flights, hotels, weather, fetch_errors = _fetch_data(req)
        it = executor.build_itinerary(req, state.get("budget_plan"), weather,
                                      flights, hotels)
        session_id = state.get("session_id", "")
        if it.weather_changes:
            changes = [c.model_dump(mode="json") for c in it.weather_changes]
            publish_event(queue, EVENT_WEATHER_CHANGED, "executor",
                          {"changes": changes}, session_id,
                          state.get("round_count", 1))
            get_store().save_decision(
                f"weather_changed:{session_id}:{state.get('round_count', 1)}",
                session_id, "weather_changed", {"changes": changes})
        return {"itinerary": it, "errors": fetch_errors}
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
        msg = AgentMessage(
            type=MessageType.RESULT_RETURN,
            topic="result.deliver",
            from_agent="supervisor",
            to_agent="user",
            payload={"summary": output.summary},
            idempotency_key=make_idempotency_key(
                "result.deliver", session_id, state.get("round_count", 1)),
        )
        queue.publish(msg, stream=StreamName.RESULTS.value)
        get_store().save_message(msg, session_id)   # 消息留痕(幂等 key 去重)
        get_store().update_session_status(session_id, state.get("status", "delivered"))
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
    # _traced:每个节点包一层 span(节点级打点,写 trace)
    builder.add_node("receive_input", _traced("receive_input", _make_receive_input(queue)))
    builder.add_node("collect_events", _traced("collect_events", _make_collect_events(queue)))
    builder.add_node("analyze", _traced("analyze", _make_analyze()))
    builder.add_node("clarify", _traced("clarify", clarify))
    builder.add_node("plan_budget", _traced("plan_budget", _make_plan_budget(queue)))
    builder.add_node("confirm_decision", _traced("confirm_decision", confirm_decision))
    builder.add_node("plan_itinerary", _traced("plan_itinerary", _make_plan_itinerary(queue)))
    builder.add_node("quality_gate", _traced("quality_gate", quality_gate))
    builder.add_node("deliver", _traced("deliver", _make_deliver(queue)))

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

    轨迹初始化(tracer/guard)必须在本入口完成:**invoke 之前 set 的
    ContextVar 会被 LangGraph 的节点上下文副本继承**(节点内部 set
    只对自己生效,见 receive_input 注释)。
    """
    session_id = session_id or str(uuid.uuid4())
    set_guard(CostGuard())
    set_tracer(Tracer(trace_id=session_id))
    graph = build_graph(checkpointer)
    config = {"configurable": {"thread_id": session_id}}
    return graph.invoke(
        {
            "user_input": user_input,
            "session_id": session_id,
            "auto_adjust": settings.auto_adjust if auto_adjust is None else auto_adjust,
        },
        config,
    )


def get_interrupt(state: dict) -> dict | None:
    """从返回状态中提取 interrupt 载荷(追问/确认挂起时非 None)。"""
    items = state.get("__interrupt__") or []
    return items[0].value if items else None
