"""预算规划师 Agent:结构化需求 + 价格数据 → BudgetPlan。"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from travel_agent.agents.base import BaseAgent
from travel_agent.agents.budget.prompts import ABLATE_ADJUST_INSTRUCTION, SYSTEM_PROMPT
from travel_agent.config import settings
from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.options import FlightOption, HotelOption
from travel_agent.schemas.requirement import TripRequirement


class BudgetPlanner(BaseAgent):
    """预算规划师:分项分配 → 超支检测 → 优先级链自动调整。"""

    name = "budget"

    def __init__(self):
        super().__init__(BudgetPlan)

    def plan_budget(
        self,
        requirement: TripRequirement,
        flight_options: list[FlightOption],
        hotel_options: list[HotelOption],
    ) -> BudgetPlan:
        """输入结构化需求与价格数据,产出分项预算。

        价格数据当前由调用方传入(M6 起经 MCP 工具统一获取)。
        """
        payload = json.dumps(
            {
                "requirement": requirement.model_dump(mode="json"),
                "flight_options": [f.model_dump(mode="json") for f in flight_options],
                "hotel_options": [h.model_dump(mode="json") for h in hotel_options],
            },
            ensure_ascii=False,
        )
        system = SYSTEM_PROMPT
        if settings.ablate_budget_adjust:
            system += ABLATE_ADJUST_INSTRUCTION
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=payload),
        ]
        return self.invoke(messages)
