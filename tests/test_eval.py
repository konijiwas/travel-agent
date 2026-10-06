"""M8 评测体系测试:判分器、case 校验、事实提炼、小规模跑分冒烟。

评测代码本身也要被测试(评测结果错 = 发布决策错):
- 判分器:操作符语义、未知事实、校验器;
- case 文件:15 条、task_id 唯一、各类数量、断言齐备;
- 跑分:选 2 条不拉 MCP 子进程的 boundary case 冒烟(秒级)。
"""

import pytest

from travel_agent.eval.cases import EvalCase, EvalCheck, load_cases, validate_cases
from travel_agent.eval.checks import CheckOutcome, evaluate
from travel_agent.eval.metrics import build_facts
from travel_agent.eval.run_eval import run_case
from travel_agent.memory.checkpointer import build_checkpointer


def _check(fact, **op):
    return EvalCheck(fact=fact, **op)


# ── 判分器 ──

def test_evaluate_equals_and_gte():
    """equals / gte 的通过与失败。"""
    facts = {"days": 3, "status": "delivered"}
    ok = evaluate(facts, [_check("days", gte=3), _check("status", equals="delivered")])
    assert ok.passed
    assert ok.passed_count == 2

    bad = evaluate(facts, [_check("days", gte=4)])
    assert not bad.passed
    assert bad.failed[0].actual == 3          # 失败明细带实际值


def test_evaluate_in_and_contains():
    """in(标量在列表 / 列表含元素)与 contains(子串)。"""
    facts = {"status": "waiting_user", "tags": ["family"],
             "text": "航班数据获取失败(已降级)"}
    ok = evaluate(facts, [
        _check("status", in_list=["delivered", "waiting_user"]),
        _check("tags", in_list=["family"]),
        _check("text", contains="降级"),
    ])
    assert ok.passed


def test_evaluate_unknown_fact_fails_with_note():
    """未知事实:断言失败并注明原因(不静默)。"""
    out = evaluate({"status": "delivered"}, [_check("nonexistent", equals=1)])
    assert not out.passed
    assert "未知事实" in out.failed[0].note


def test_evaluate_numeric_comparison_on_bad_type():
    """数值比较遇 None/字符串:按 0 计,比较失败但不崩。"""
    out = evaluate({"days": None}, [_check("days", gte=1)])
    assert not out.passed


def test_check_validation_exactly_one_operator():
    """断言必须且只能有一个操作符。"""
    with pytest.raises(ValueError):
        EvalCheck(fact="x")                    # 零个操作符
    with pytest.raises(ValueError):
        EvalCheck(fact="x", equals=1, gte=2)   # 两个操作符


def test_case_validation_enums():
    """类别/故障枚举校验:非法值在加载时报错。"""
    with pytest.raises(ValueError):
        EvalCase(task_id="x", category="nonsense", input="", expected="",
                 checks=[{"fact": "a", "equals": 1}])
    with pytest.raises(ValueError):
        EvalCase(task_id="x", category="normal", input="", expected="",
                 fault="bad_fault", checks=[{"fact": "a", "equals": 1}])


# ── case 文件 ──

def test_case_files_load_15_and_valid():
    """15 条 case 齐备:task_id 唯一、各类数量符合方案、断言非空。"""
    cases = load_cases()
    assert len(cases) == 15
    assert validate_cases(cases) == []
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c.category] = by_cat.get(c.category, 0) + 1
        assert c.checks
    assert by_cat == {"normal": 4, "boundary": 4, "error": 3,
                      "security": 2, "decision": 2}


# ── 事实提炼(纯函数)──

def test_build_facts_basic():
    """空状态 + 交互信息:状态/崩溃/挂起类事实正确。"""
    facts = build_facts({}, [], ["clarify"], ["e1"], no_crash=True)
    assert facts["no_crash"] is True
    assert facts["status"] == "waiting_user"   # 无交付 + 有挂起
    assert facts["first_interrupt_kind"] == "clarify"
    assert facts["interrupt_count"] == 1
    assert facts["errors_text"] == "e1"
    assert facts["days_count"] == 0


def test_build_facts_security_markers():
    """安全事实:输出含预订/泄漏字样时置真。"""
    from travel_agent.supervisor.state import FinalOutput

    state = {"status": "delivered",
             "final_output": FinalOutput(summary="已预订航班,支付成功")}
    facts = build_facts(state, [], [], [], no_crash=True)
    assert facts["booking_attempted"] is True
    assert facts["prompt_leak"] is False


# ── 跑分冒烟(2 条不拉 MCP 子进程的 boundary case)──

@pytest.fixture(scope="module")
def loaded_cases() -> dict[str, EvalCase]:
    return {c.task_id: c for c in load_cases()}


def test_run_case_boundary01_empty_input(loaded_cases):
    """空输入:挂起追问(clarify),断言全过。"""
    run = run_case(loaded_cases["boundary-01"],
                   build_checkpointer(in_memory=True))
    assert run.passed
    assert run.facts["no_crash"] is True
    assert run.facts["first_interrupt_kind"] == "clarify"


def test_run_case_boundary04_incomplete_input(loaded_cases):
    """信息严重缺失 + 答案不足:不崩溃,状态合法。"""
    run = run_case(loaded_cases["boundary-04"],
                   build_checkpointer(in_memory=True))
    assert run.passed
    assert run.facts["status"] in ("delivered", "waiting_user")
