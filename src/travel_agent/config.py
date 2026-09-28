"""全局配置:所有开关集中于此,经 pydantic-settings 读取 .env。

业务代码一律 `from travel_agent.config import settings`,禁止硬编码
key / 超时 / 开关值。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        extra="ignore",
    )

    # ── LLM(DeepSeek,OpenAI 兼容协议)──
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    analyst_model: str = ""              # 消融:需求分析换小模型时覆盖
    structured_method: str = "auto"      # auto | function_calling | json_schema
    llm_temperature: float = 0.2
    llm_timeout_s: float = 60.0

    # ── 队列 ──
    queue_backend: str = Field("auto", validation_alias="TA_QUEUE_BACKEND")
    redis_url: str = Field("redis://127.0.0.1:6379/0", validation_alias="TA_REDIS_URL")

    # ── 流程开关 ──
    auto_adjust: bool = Field(False, validation_alias="TA_AUTO_ADJUST")
    max_clarify_rounds: int = Field(2, validation_alias="TA_MAX_CLARIFY_ROUNDS")
    max_retry_nodes: int = 1             # 质量门不过时回退重试次数

    # ── 可靠性 ──
    tool_timeout_s: float = Field(5.0, validation_alias="TA_TOOL_TIMEOUT_S")
    breaker_failure_threshold: int = 3
    breaker_recovery_s: float = 30.0
    cost_limit_usd: float = Field(0.15, validation_alias="TA_COST_LIMIT_USD")
    cost_token_limit: int = 20_000
    price_in_usd_per_m: float = 0.28     # 输入价 $/百万 token(DeepSeek 可改)
    price_out_usd_per_m: float = 1.10    # 输出价 $/百万 token

    # ── 数据 / 留痕 ──
    db_path: str = Field(str(PROJECT_ROOT / "data" / "travel.db"), validation_alias="TA_DB_PATH")
    trace_dir: str = Field(str(PROJECT_ROOT / "data" / "traces"), validation_alias="TA_TRACE_DIR")

    # ── 消融开关(1 = 关闭该功能,供 M9 对照实验)──
    ablate_budget_adjust: bool = False
    ablate_weather: bool = False
    ablate_queue: bool = False
    ablate_small_gate: bool = False

    # ── 评测 ──
    eval_mode: str = "llm"               # llm | fake(离线冒烟,不调用真实 API)

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.trace_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
