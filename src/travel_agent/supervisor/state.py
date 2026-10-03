"""Supervisor 状态定义:StateGraph 各节点共享的"白板"。"""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.itinerary import Itinerary
from travel_agent.schemas.message import AgentMessage
from travel_agent.schemas.requirement import ClarifyQuestion, TripRequirement


class ConfirmationRequest(BaseModel):
    """待用户确认的决策(预算调整提案 / 天气变更)。

    confirm_decision 节点把它 interrupt 给用户;用户同意后 payload 生效。
    """

    kind: str                   # "budget" | "weather"
    title: str                  # 展示给用户的标题
    detail: str                 # 具体方案说明(四要素/三要素已含)
    payload: dict = Field(default_factory=dict)   # 结构化内容(序列化后的提案)


class QualityReport(BaseModel):
    """质量门检查结果:六项检查 + 失败分类(与评测 failure_type 枚举对齐)。"""

    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)   # 检查项名 -> 是否通过
    failure_type: str = ""      # 工具选错/上下文污染/模型能力/业务规则/权限问题/超时未降级
    retried: bool = False       # 是否已回退重试过(重试仍失败则降级交付)
    notes: list[str] = Field(default_factory=list)


class FinalOutput(BaseModel):
    """交付物:行程 + 预算 + 调整说明汇总 + 数据时间戳。"""

    itinerary: Itinerary | None = None
    budget_plan: BudgetPlan | None = None
    summary: str = ""           # 调整与变更说明汇总(来自两本台账)
    data_timestamp: str = ""    # 数据获取时间标注(质量门⑥要求)


class SupervisorState(TypedDict, total=False):
    """Supervisor 状态机共享状态:每个节点读它、改它、返回增量。

    TypedDict(total=False):字段都可选,节点只需返回自己改动的字段;
    events / errors 声明了 Annotated[..., operator.add] 追加归约:
    节点返回 {events: [新事件]} 时新事件**追加**到已有列表,而不是覆盖。
    """

    # 会话与输入
    session_id: str
    user_input: str             # 本轮用户输入(追问回答/修改意见也是它)
    round_count: int            # 追问/重规划轮次,质量门用来截断
    auto_adjust: bool           # 自动调整开关(True 免确认但留痕)

    # 三 Agent 中间产物
    requirement: TripRequirement | None
    pending_questions: list[ClarifyQuestion]
    budget_plan: BudgetPlan | None
    pending_confirmation: ConfirmationRequest | None
    itinerary: Itinerary | None

    # 事件(经队列流入,追加语义:可追踪/可回放)
    events: Annotated[list[AgentMessage], operator.add]

    # 质量门与交付
    quality: QualityReport | None
    quality_retry_count: int        # 质量门回退重试次数(超过 max_retry_nodes 降级交付)
    confirm_result: str             # confirm_decision 节点结果:"accepted" / "rejected"
    final_output: FinalOutput | None
    status: Literal["running", "waiting_user", "delivered", "failed"]
    errors: Annotated[list[str], operator.add]
