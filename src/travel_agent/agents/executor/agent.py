"""行程执行者 Agent:需求 + 预算 + 天气/航班/酒店数据 → Itinerary。"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from travel_agent.agents.base import BaseAgent
from travel_agent.agents.executor.prompts import SYSTEM_PROMPT
from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.itinerary import Itinerary
from travel_agent.schemas.options import DailyWeather, FlightOption, HotelOption
from travel_agent.schemas.requirement import TripRequirement


class Executor(BaseAgent):
    """行程执行者:编排每日行程 → 时间冲突检查 → 天气影响变更。"""

    name = "executor"

    def __init__(self):
        super().__init__(Itinerary)

    def build_itinerary(
        self,
        requirement: TripRequirement,
        budget_plan: BudgetPlan,
        weather: list[DailyWeather],
        flight_options: list[FlightOption],
        hotel_options: list[HotelOption],
    ) -> Itinerary:
        """输入需求/预算/天气/价格数据,产出逐日行程。

        weather 为空时走提示词规则 6:不做替换、weather_note 标注"天气未知"。
        """
        payload = json.dumps(
            {
                "requirement": requirement.model_dump(mode="json"),
                "budget_plan": budget_plan.model_dump(mode="json"),
                "weather": [w.model_dump(mode="json") for w in weather],
                "flight_options": [f.model_dump(mode="json") for f in flight_options],
                "hotel_options": [h.model_dump(mode="json") for h in hotel_options],
            },
            ensure_ascii=False,
        )
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=payload),
        ]
        return self.invoke(messages)
