"""需求侧数据模型:TripRequirement / 追问 / 需求分析师输出。"""

from pydantic import BaseModel, Field

# 偏好标签枚举值(需求分析师把自然语言偏好映射到这些标签)
PREFERENCE_TAGS = ["family", "adventure", "food", "culture", "relax", "business"]


class BudgetRange(BaseModel):
    """预算区间(人民币)。"""

    min_cny: float
    max_cny: float


class TripRequirement(BaseModel):
    """结构化旅行需求(需求分析师产出)。日期统一 YYYY-MM-DD。"""

    destination: str
    origin_city: str | None = None        # 出发城市
    start_date: str | None = None
    end_date: str | None = None
    travelers: int | None = None
    budget_range: BudgetRange | None = None
    preferences: list[str] = Field(default_factory=list)   # 偏好标签(PREFERENCE_TAGS 取值)
    constraints: list[str] = Field(default_factory=list)   # 硬约束文本


class ClarifyQuestion(BaseModel):
    """缺失信息的追问。field 取值:destination|origin_city|dates|travelers|budget。"""

    field: str
    question: str


class AnalystOutput(BaseModel):
    """需求分析师输出:完整需求,或缺失字段 + 追问问题(每轮最多 2 个)。"""

    requirement: TripRequirement | None = None
    missing_fields: list[str] = Field(default_factory=list)
    clarify_questions: list[ClarifyQuestion] = Field(default_factory=list)
