"""评测用例:数据模型 + 加载校验(15 条 case 的格式错误即时暴露)。

EvalCase 是 case 文件每行的 Pydantic 模型:加载时即校验,
类别/故障枚举、断言操作符唯一性等错误在加载阶段直接报错,
而不是等到跑分时才发现。
"""

from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from travel_agent.config import PROJECT_ROOT

CASES_DIR = PROJECT_ROOT / "eval" / "cases"

CATEGORIES = ("normal", "boundary", "error", "security", "decision")
VALID_FAULTS = ("none", "flight_timeout", "hotel_no_room", "weather_empty")


class EvalCheck(BaseModel):
    """一条断言:fact(事实名)+ 恰好一个操作符 + 期望值。

    操作符白名单:equals(等值)/ not_equals / gt / gte / lt / lte
    (数值比较)/ contains(子串或列表元素包含)/ in(值属于列表)。
    由 eval/checks.py 的解释器执行,不走 Python eval,无注入风险。
    """

    model_config = {"populate_by_name": True}   # "in" 与 "in_list" 均可传参

    fact: str
    equals: object | None = None
    not_equals: object | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None
    contains: str | None = None
    in_list: list | None = Field(None, alias="in")   # JSON 里写 "in"

    @model_validator(mode="after")
    def _exactly_one_operator(self) -> "EvalCheck":
        operators = [self.equals, self.not_equals, self.gt, self.gte,
                     self.lt, self.lte, self.contains, self.in_list]
        present = [op for op in operators if op is not None]
        if len(present) != 1:
            raise ValueError(
                f"断言 {self.fact} 必须且只能有一个操作符,实际 {len(present)} 个")
        return self

    def operator(self) -> str:
        """返回生效的操作符名(写报告用)。"""
        for name in ("equals", "not_equals", "gt", "gte", "lt", "lte",
                     "contains", "in_list"):
            if getattr(self, name) is not None:
                return "in" if name == "in_list" else name
        raise AssertionError("不可达:校验保证恰有一个操作符")


class EvalCase(BaseModel):
    """评测 case:输入 + 期望 + 故障注入 + 断言列表。"""

    task_id: str
    category: str
    input: str
    expected: str                    # 期望说明(给人看,写进报告)
    fault: str = "none"              # MOCK_FAULT 注入值
    auto_adjust: bool = False        # 预算调整免确认开关(decision 类可用)
    answers: list[str] = Field(default_factory=list)   # 挂起时的预置回答
    checks: list[EvalCheck]

    @model_validator(mode="after")
    def _validate_enums(self) -> "EvalCase":
        if self.category not in CATEGORIES:
            raise ValueError(
                f"{self.task_id}: category={self.category!r} 不在 {CATEGORIES}")
        if self.fault not in VALID_FAULTS:
            raise ValueError(
                f"{self.task_id}: fault={self.fault!r} 不在 {VALID_FAULTS}")
        return self


def load_cases(cases_dir: str | Path | None = None) -> list[EvalCase]:
    """加载全部 case(按文件名排序读入,评测顺序稳定可复现)。"""
    directory = Path(cases_dir) if cases_dir else CASES_DIR
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:                       # 跳过空行(容忍末尾换行)
                cases.append(EvalCase.model_validate_json(line))
    return cases


def validate_cases(cases: list[EvalCase]) -> list[str]:
    """跨 case 校验:task_id 唯一、checks 非空。返回问题列表(空=通过)。"""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        if case.task_id in seen:
            problems.append(f"{case.task_id}: task_id 重复")
        seen.add(case.task_id)
        if not case.checks:
            problems.append(f"{case.task_id}: checks 为空")
    return problems
