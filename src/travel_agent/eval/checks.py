"""评测判分器:断言 DSL 的解释执行(纯函数,无 I/O,单测友好)。

facts = 跑分器从一次运行结果中提炼的事实字典(扁平 key → 值),
checks = 该 case 的断言列表;evaluate() 逐条解释执行,产出
逐条结果 + 总体通过与否。操作符白名单,不做 Python eval。
"""

from dataclasses import dataclass
from typing import Any

from travel_agent.eval.cases import EvalCheck


@dataclass
class AssertionResult:
    """单条断言的执行结果(报告逐条展示用)。"""

    fact: str
    operator: str
    expected: Any
    actual: Any
    passed: bool
    note: str = ""


@dataclass
class CheckOutcome:
    """一个 case 全部断言的判分结果。"""

    passed: bool
    results: list[AssertionResult]
    failed: list[AssertionResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return self.total - len(self.failed)


def _contains_any(actual: Any, expected_items: list) -> bool:
    """actual 里是否存在 expected 中至少一个元素。

    - actual 是字符串:按子串判断;
    - actual 是列表/集合/元组:按元素判断;
    - actual 是标量:actual 本身是否在 expected 里。
    """
    if isinstance(actual, str):
        return any(item in actual for item in expected_items)
    if isinstance(actual, (list, tuple, set)):
        return any(item in actual for item in expected_items)
    return actual in expected_items


def _to_number(value: Any) -> float:
    """数值比较用:能转则转,不能转按 0 计(比较自然失败,不算异常)。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _compare(operator: str, actual: Any, expected: Any) -> bool:
    """执行单条比较(操作符白名单)。"""
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "gt":
        return _to_number(actual) > expected
    if operator == "gte":
        return _to_number(actual) >= expected
    if operator == "lt":
        return _to_number(actual) < expected
    if operator == "lte":
        return _to_number(actual) <= expected
    if operator == "contains":
        return _contains_any(actual, [expected])
    if operator == "in":
        return _contains_any(actual, list(expected))
    return False


def evaluate(facts: dict[str, Any], checks: list[EvalCheck]) -> CheckOutcome:
    """执行一个 case 的全部断言。fact 不存在 = 该断言失败并注明原因。"""
    results: list[AssertionResult] = []
    for check in checks:
        operator = check.operator()
        expected = getattr(check, "in_list" if operator == "in" else operator)
        if check.fact not in facts:
            results.append(AssertionResult(
                fact=check.fact, operator=operator, expected=expected,
                actual=None, passed=False,
                note=f"未知事实 {check.fact!r}(跑分器未产出该事实)"))
            continue
        actual = facts[check.fact]
        passed = _compare(operator, actual, expected)
        results.append(AssertionResult(
            fact=check.fact, operator=operator, expected=expected,
            actual=actual, passed=passed))
    failed = [r for r in results if not r.passed]
    return CheckOutcome(passed=not failed, results=results, failed=failed)
