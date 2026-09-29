"""需求分析师 Agent:解析用户输入 → AnalystOutput。"""

from langchain_core.messages import HumanMessage, SystemMessage

from travel_agent.agents.analyst.prompts import SYSTEM_PROMPT
from travel_agent.agents.base import BaseAgent
from travel_agent.schemas.requirement import AnalystOutput


class Analyst(BaseAgent):
    """需求分析师:只理解需求,不规划、不报价。"""

    name = "analyst"

    def __init__(self):
        super().__init__(AnalystOutput)

    def analyze(self, user_input: str) -> AnalystOutput:
        """解析一轮用户输入。后续轮次带追问回答时,由调用方把上下文拼进 user_input。"""
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_input),
        ]
        return self.invoke(messages)
