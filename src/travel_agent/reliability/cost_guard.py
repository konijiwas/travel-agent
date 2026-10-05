"""可靠性六件套 ⑤ 成本护栏(CostGuard)。

LLM 按 token 计费:一次行程规划可能调用模型十几次(分析/预算/
行程/质量门回退),若流程失控(死循环、超长上下文)费用会失控。
CostGuard 给**单次行程规划**设 token 与金额双上限,超限立即终止
并给出明细 —— "宁可这次失败,不让账单失控"。

金额计算用配置的输入/输出单价(DeepSeek 定价,可改):
    cost = prompt_tokens × price_in + completion_tokens × price_out

"当前护栏"用 ContextVar 传递:单次轨迹开始时 set_guard() 建新
护栏,LLM 调用层 record_usage() 记账;跨函数/协程自动传递,
不同轨迹互不串账。
"""

import contextvars
import threading
from typing import Any

from travel_agent.config import settings


class CostLimitExceededError(Exception):
    """成本超限:携带已用/上限明细,供 trace 与提示展示。"""

    def __init__(self, usage: dict[str, Any], limit_usd: float):
        self.usage = usage
        self.limit_usd = limit_usd
        super().__init__(
            f"成本超限:已用 ${usage['cost_usd']:.4f}(上限 ${limit_usd:.2f}),"
            f"token {usage['total_tokens']} / {usage['token_limit']}"
        )


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数(fake 离线模式无 API 用量返回时用)。

    规则:中文字符 1 字 ≈ 1 token,其余字符约 4 个 ≈ 1 token。
    估算只用于护栏(宁可估高不可估低),不追求精确。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return max(1, cjk + other // 4)


class CostGuard:
    """单次轨迹的预算账本(线程安全)。"""

    def __init__(self, limit_usd: float | None = None,
                 token_limit: int | None = None):
        self.limit_usd = settings.cost_limit_usd if limit_usd is None else limit_usd
        self.token_limit = settings.cost_token_limit if token_limit is None else token_limit
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._lock = threading.Lock()

    # ── 记账 ──

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记一笔模型调用用量;超限抛 CostLimitExceededError(调用方终止流程)。"""
        with self._lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            if self.total_tokens > self.token_limit or self.cost_usd > self.limit_usd:
                raise CostLimitExceededError(self.snapshot(), self.limit_usd)

    # ── 查询 ──

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        """按配置单价折算金额(美元)。"""
        return (self.prompt_tokens * settings.price_in_usd_per_m
                + self.completion_tokens * settings.price_out_usd_per_m) / 1_000_000

    def snapshot(self) -> dict[str, Any]:
        """当前账目快照(写 trace / 异常信息用)。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "token_limit": self.token_limit,
            "limit_usd": self.limit_usd,
        }


# ── 当前轨迹护栏(ContextVar:跨函数/协程传递,轨迹间隔离)──

_current_guard: contextvars.ContextVar[CostGuard] = contextvars.ContextVar(
    "current_cost_guard", default=CostGuard()
)


def set_guard(guard: CostGuard) -> None:
    """单次轨迹开始时调用:换一个新账本。"""
    _current_guard.set(guard)


def get_guard() -> CostGuard:
    """当前轨迹的账本(未显式设置时是独立默认实例)。"""
    return _current_guard.get()
