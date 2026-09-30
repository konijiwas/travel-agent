"""预算侧数据模型:BudgetPlan / 分项 / 调整记录 / 待确认方案。"""

from pydantic import BaseModel, Field

# 分项类别(枚举取值)
BUDGET_CATEGORIES = ["transport", "hotel", "food", "activities", "buffer"]

# 参考分配比例(实际价格数据会覆盖交通/住宿,餐饮/活动/机动按此软参考)
DEFAULT_RATIOS: dict[str, float] = {
    "transport": 0.30,
    "hotel": 0.35,
    "food": 0.15,
    "activities": 0.15,
    "buffer": 0.05,
}


class BudgetItem(BaseModel):
    """一个分项预算。

    status 取值:proposed(初始提出)/ adjusted(调整后)/ confirmed(用户确认)。
    """

    category: str
    amount: float
    note: str = ""              # 依据说明,如 "MU5131 往返 ×2人"
    status: str = "proposed"


class BudgetAdjustment(BaseModel):
    """一条预算调整记录。四要素缺一不可(质量门校验):reason / from_item / to_item / impact。

    设计文档中写的是 reason/from/to/impact,但 `from` 是 Python 保留字,
    故字段命名为 from_item / to_item,语义不变。
    """

    reason: str                 # 调整原因,如 "总价超出预算 380 元"
    from_item: str              # 调整前,如 "酒店:西湖悦府 ¥620/晚"
    to_item: str                # 调整后,如 "酒店:湖畔假日 ¥480/晚"
    impact: str                 # 影响,如 "3 晚共省 ¥420"


class AdjustProposal(BaseModel):
    """待用户确认的调整方案(调整优先级链走完仍超 10% 阈值时产出)。

    超支无法自动解决时:不静默超支,也不擅自决定 —— 打包成提案交给
    Supervisor,由 confirm_decision 节点请用户确认(权限分级原则)。
    """

    overrun_cny: float                          # 当前超支金额
    proposal: list[BudgetAdjustment]            # 建议的调整方案(可执行)
    note: str = ""                              # 说明,如 "已按优先级链①②③调整,仍超支"


class BudgetPlan(BaseModel):
    """预算规划师产出:分项预算 + 调整记录 + 是否需要用户确认。

    needs_confirmation=True 是给 Supervisor 条件路由的信号:
    有待确认提案时走 confirm_decision 节点,否则直达行程执行者。
    """

    total_budget: float                         # 总预算(= 各分项之和)
    items: list[BudgetItem] = Field(default_factory=list)
    adjustments: list[BudgetAdjustment] = Field(default_factory=list)
    needs_confirmation: bool = False            # True = 存在待确认方案(超支未解决)
    pending_proposal: AdjustProposal | None = None
