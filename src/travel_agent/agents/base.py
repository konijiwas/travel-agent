"""Agent 基类:统一结构化输出调用 + 解析失败重试 1 次。"""

import logging

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel

from travel_agent.llm.client import get_structured_llm

logger = logging.getLogger(__name__)


class BaseAgent:
    """所有专业 Agent 的基类:绑定结构化输出 LLM,统一失败重试。

    后续里程碑(M6)会在此叠加 CostGuard / Tracer 等可靠性包装。
    """

    name: str = "base"

    def __init__(self, output_schema: type[BaseModel]):
        self.output_schema = output_schema
        self.llm = get_structured_llm(output_schema)

    def invoke(self, messages) -> BaseModel:
        """调用 LLM 并解析为 output_schema;解析失败自动重试 1 次。"""
        try:
            return self.llm.invoke(messages)
        except OutputParserException:
            logger.warning("%s: 结构化输出解析失败,重试 1 次", self.name)
            return self.llm.invoke(messages)
