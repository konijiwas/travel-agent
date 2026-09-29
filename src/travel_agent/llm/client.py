"""LLM 客户端:DeepSeek(OpenAI 兼容协议)统一封装,配置化可切换。

换模型(OpenAI/GLM/本地)只改 .env 三个 DEEPSEEK_* 变量,零代码改动。
LLM 调用一律经此模块,禁止业务代码直接实例化(AGENTS.md)。
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from travel_agent.config import settings


@lru_cache
def get_llm(temperature: float | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        api_key=SecretStr(settings.deepseek_api_key),
        temperature=settings.llm_temperature if temperature is None else temperature,
        max_retries=0,          # 关闭 SDK 内置重试,统一走 reliability(避免重试风暴)
        timeout=settings.llm_timeout_s,
    )


def get_structured_llm(schema: type[BaseModel]):
    """结构化输出 LLM:function_calling 为主、json_schema 兜底,配置可切。

    TA_EVAL_MODE=fake 时返回离线规则解析器(不调用 API,无 Key 可跑通)。
    """
    if settings.eval_mode == "fake":
        from travel_agent.llm.fake import FakeStructuredLLM

        return FakeStructuredLLM(schema)

    kwargs: dict = {}
    if settings.structured_method in ("function_calling", "json_schema"):
        kwargs["method"] = settings.structured_method
    return get_llm(temperature=0.1).with_structured_output(schema, **kwargs)
