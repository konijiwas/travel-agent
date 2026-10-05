"""LangGraph 检查点:会话状态持久化,支撑追问挂起 / 中断恢复。"""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from travel_agent.config import settings


def _build_serde() -> JsonPlusSerializer:
    """构建序列化器:显式白名单本项目状态里的全部自定义模型。

    SupervisorState 装着 TripRequirement/AgentMessage 等自定义模型,
    checkpoint 序列化(msgpack)对自定义类型要求显式白名单 ——
    未来版本将收紧为默认拒绝;`True` 表示"放行但打警告",
    只有具体类名清单才能干净通过。传类对象即可(自动转成
    (模块, 类名) 对);状态里新增模型时,在此追加它的类。

    注意:嵌套模型要单独列出(如 BudgetPlan 里的 BudgetItem),
    白名单只做 (模块, 类名) 精确匹配,不通配。
    """
    from travel_agent.schemas.budget import (
        AdjustProposal,
        BudgetAdjustment,
        BudgetItem,
        BudgetPlan,
    )
    from travel_agent.schemas.itinerary import (
        Activity,
        Conflict,
        Itinerary,
        ItineraryDay,
        WeatherChange,
    )
    from travel_agent.schemas.message import AgentMessage, MessageType
    from travel_agent.schemas.requirement import (
        BudgetRange,
        ClarifyQuestion,
        TripRequirement,
    )
    from travel_agent.supervisor.state import (
        ConfirmationRequest,
        FinalOutput,
        QualityReport,
    )

    allowlist = [
        # 需求
        TripRequirement, BudgetRange, ClarifyQuestion,
        # 预算(含嵌套)
        BudgetPlan, BudgetItem, BudgetAdjustment, AdjustProposal,
        # 行程(含嵌套)
        Itinerary, Activity, ItineraryDay, WeatherChange, Conflict,
        # 消息(含枚举)
        AgentMessage, MessageType,
        # Supervisor 状态模型
        ConfirmationRequest, QualityReport, FinalOutput,
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowlist)


def build_checkpointer(db_path: str | None = None, in_memory: bool = False) -> BaseCheckpointSaver:
    """创建检查点保存器。

    - in_memory=True → InMemorySaver(测试用,进程内有效);
    - 否则 → SqliteSaver 落盘持久化,路径取 db_path 参数或配置(默认 data/travel.db):
      interrupt() 挂起后(等用户回答追问/确认),状态存进 SQLite;
      进程重启后同一 thread_id 可从中断处续跑。

    注:langgraph 1.x 的 SqliteSaver.from_conn_string() 返回上下文管理器,
    此处直接用 SqliteSaver(conn) 构造,连接生命周期与进程一致。
    """
    serde = _build_serde()
    if in_memory:
        return InMemorySaver(serde=serde)
    path = db_path or settings.db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn, serde=serde)
