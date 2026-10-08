"""M9 消融实验测试:开关接线(预算/天气/质量门/队列)+ 消融跑分器结构。

消融开关是"关掉某功能重跑评测"的机制,测试分两层:
- 接线层:开关确实接进了对应环节 —— 预算/天气消融追加到 LLM 提示词、
  质量门消融短路条件路由、队列消融换成空队列;
- 结构层:ablate.py 跑分器自身正确 —— 行模型、开关切换、跑完恢复状态。

全部走 fake 离线模式或 monkeypatch 假跑,不调用真实 API,秒级完成。
"""

from types import SimpleNamespace

from travel_agent.agents.budget.agent import BudgetPlanner
from travel_agent.agents.executor.agent import Executor
from travel_agent.config import settings
from travel_agent.eval import ablate
from travel_agent.queue.base import NoOpQueue
from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.itinerary import Itinerary
from travel_agent.schemas.requirement import BudgetRange, TripRequirement
from travel_agent.supervisor.graph import route_after_gate


# ── 构造辅助 ──

def _req() -> TripRequirement:
    return TripRequirement(
        destination="杭州", origin_city="上海",
        start_date="2026-10-01", end_date="2026-10-03", travelers=1,
        budget_range=BudgetRange(min_cny=0.0, max_cny=6000.0),
    )


class _SpyLLM:
    """拦截 invoke:记录传入消息,返回固定结构化结果(不经提示词生成)。

    用 Spy 替换 agent.llm,直接检验 agent 是否把消融指令拼进了系统提示词,
    不关心 LLM 实际如何解析(fake 离线解析器本就不读提示词)。
    """

    def __init__(self, captured: list, result):
        self._captured = captured
        self._result = result

    def invoke(self, messages):
        self._captured.append(messages)
        return self._result


# ── 接线层:消融指令确实追加进提示词 ──

def test_budget_ablate_switch_appends_instruction(monkeypatch):
    """ablate_budget_adjust 开启时,预算系统提示词追加消融指令;关闭时不追加。"""
    off, on = [], []

    monkeypatch.setattr(settings, "ablate_budget_adjust", False)
    p = BudgetPlanner()
    p.llm = _SpyLLM(off, BudgetPlan(total_budget=0.0, items=[]))
    p.plan_budget(_req(), [], [])

    monkeypatch.setattr(settings, "ablate_budget_adjust", True)
    p = BudgetPlanner()
    p.llm = _SpyLLM(on, BudgetPlan(total_budget=0.0, items=[]))
    p.plan_budget(_req(), [], [])

    assert "消融开关" not in off[0][0].content
    assert "关闭超支检测与自动调整" in on[0][0].content


def test_executor_ablate_switch_appends_instruction(monkeypatch):
    """ablate_weather 开启时,行程执行者系统提示词追加消融指令;关闭时不追加。"""
    off, on = [], []
    budget = BudgetPlan(total_budget=0.0, items=[])

    monkeypatch.setattr(settings, "ablate_weather", False)
    e = Executor()
    e.llm = _SpyLLM(off, Itinerary())
    e.build_itinerary(_req(), budget, [], [], [])

    monkeypatch.setattr(settings, "ablate_weather", True)
    e = Executor()
    e.llm = _SpyLLM(on, Itinerary())
    e.build_itinerary(_req(), budget, [], [], [])

    assert "消融开关" not in off[0][0].content
    assert "关闭天气影响变更" in on[0][0].content


def test_route_after_gate_ablate_skips_failing_gate(monkeypatch):
    """ablate_small_gate 开启时,即使质量门判定失败也直接交付(短路)。"""
    failing = SimpleNamespace(
        passed=False, retried=False,
        checks={"requirement_complete": True, "budget_deviation": False,
                "adjustments_traceable": True, "data_sources_noted": True})

    monkeypatch.setattr(settings, "ablate_small_gate", False)
    assert route_after_gate({"quality": failing}) == "plan_budget"   # 正常:回退重试

    monkeypatch.setattr(settings, "ablate_small_gate", True)
    assert route_after_gate({"quality": failing}) == "deliver"       # 消融:跳过质量门


def test_noop_queue_is_inert():
    """空队列:发布即丢弃、消费恒空、ack/close 空操作、健康检查恒真。"""
    q = NoOpQueue()
    assert q.publish(None) == ""              # 不关心消息内容,直接丢弃
    assert q.consume("tasks", "g", "c") == []
    assert q.ack("tasks", "g", "1-0") is None
    assert q.healthcheck() is True
    assert q.close() is None


# ── 结构层:消融跑分器自身 ──

def test_ablation_row_rate():
    """行模型的通过率属性:分子/分母,零分母安全返回 0。"""
    assert ablate.AblationRow("x", 3, 4).rate == 0.75
    assert ablate.AblationRow("x", 0, 0).rate == 0.0


def test_set_all_toggles_all_switches():
    """_set_all 一次切换全部 4 个开关。"""
    ablate._set_all(True)
    assert all(getattr(settings, f) is True for f, _ in ablate.ABLATION_SWITCHES)
    ablate._set_all(False)
    assert all(getattr(settings, f) is False for f, _ in ablate.ABLATION_SWITCHES)


def test_run_ablation_baseline_plus_switches_and_restores(monkeypatch):
    """跑分器:基线 + 4 个开关共 5 行,顺序/计数正确,跑完恢复开关状态。

    用假 run_case 模拟"关掉预算调整会掉分":预算开关打开时判失败,
    其余开关不影响通过 —— 据此断言每行的通过数与标签。
    """
    fake_cases = [SimpleNamespace(task_id="t1", checks=[1]),
                  SimpleNamespace(task_id="t2", checks=[1]),
                  SimpleNamespace(task_id="t3", checks=[1])]

    def fake_run_case(case, checkpointer):
        return SimpleNamespace(passed=not settings.ablate_budget_adjust)

    monkeypatch.setattr(ablate, "load_cases", lambda d=None: fake_cases)
    monkeypatch.setattr(ablate, "validate_cases", lambda c: [])
    monkeypatch.setattr(ablate, "build_checkpointer", lambda **k: object())
    monkeypatch.setattr(ablate, "run_case", fake_run_case)

    ablate._set_all(False)                       # 干净起点
    rows = ablate.run_ablation(None, checkpointer=object())

    assert len(rows) == 5
    assert rows[0].label == "基线(全功能)"
    assert (rows[0].passed, rows[0].rate) == (3, 1.0)
    assert rows[1].label == "关掉 预算超支自动调整"
    assert rows[1].passed == 0
    assert [r.passed for r in rows[2:]] == [3, 3, 3]

    for field, _ in ablate.ABLATION_SWITCHES:    # 状态已恢复,不污染后续
        assert getattr(settings, field) is False
