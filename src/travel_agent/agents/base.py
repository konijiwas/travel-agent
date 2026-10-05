"""Agent 基类:统一结构化输出调用 + 解析失败重试 1 次 + 六件套包装(M7)。

每次 LLM 调用穿 wrap_sync_call:熔断(llm 粒度)+ 重试 + 打点,
并在调用后把 token 用量记入当前轨迹的 CostGuard(超限即终止)。
"""

import logging

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel

from travel_agent.llm.client import get_structured_llm
from travel_agent.reliability import (
    estimate_tokens,
    get_guard,
    wrap_sync_call,
)

logger = logging.getLogger(__name__)

# 所有 Agent 的 LLM 调用共用一个熔断器:它们背后是同一个故障源(DeepSeek API)
LLM_BREAKER = "llm"


class BaseAgent:
    """所有专业 Agent 的基类:绑定结构化输出 LLM,统一失败重试与打点。"""

    name: str = "base"

    def __init__(self, output_schema: type[BaseModel]):
        self.output_schema = output_schema
        self.llm = get_structured_llm(output_schema)

    def invoke(self, messages) -> BaseModel:
        """调用 LLM 并解析为 output_schema;解析失败自动重试 1 次。

        两层重试分工:
        - wrap_sync_call 内:瞬时故障(网络/限流)指数退避重试;
        - 本方法内:结构化输出解析失败(模型格式跑偏)重试 1 次 ——
          该类错误重打一次通常能修正,且不消耗六件套的熔断计数。
        """
        try:
            return self._invoke_wrapped(messages)
        except OutputParserException:
            logger.warning("%s: 结构化输出解析失败,重试 1 次", self.name)
            return self._invoke_wrapped(messages)

    def _invoke_wrapped(self, messages) -> BaseModel:
        """单次调用:六件套包装 + CostGuard 记账。"""

        def _call():
            return self.llm.invoke(messages)

        result = wrap_sync_call(
            f"{self.name}.invoke",
            _call,
            breaker=LLM_BREAKER,
            input_summary={"agent": self.name, "messages": len(messages),
                           "chars": sum(len(str(m.content)) for m in messages)},
        )
        self._record_usage(messages, result)
        return result

    def _record_usage(self, messages, result: BaseModel) -> None:
        """把本次调用用量记入当前轨迹账本(超限抛 CostLimitExceededError 终止)。

        用量按估算(中文 1 字≈1 token):fake 模式无 API 用量返回,
        真实模式的结构化输出只回传解析结果、不带 usage 元数据,
        两种模式统一估算,护栏方向"宁高勿低"。
        """
        prompt_tokens = estimate_tokens(
            "".join(str(m.content) for m in messages))
        completion_tokens = estimate_tokens(result.model_dump_json())
        get_guard().record_usage(prompt_tokens, completion_tokens)
