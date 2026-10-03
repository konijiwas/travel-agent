"""质量门:六项硬规则校验(LLM 只生成,规则做确定性校验)。

check_quality 是纯函数:无 I/O、无 LLM 调用,输入状态字典输出 QualityReport,
单测友好;失败项经 failure_type 与评测枚举对齐(M8 直接复用)。
"""

from travel_agent.schemas.budget import BudgetPlan
from travel_agent.schemas.itinerary import Itinerary
from travel_agent.schemas.requirement import TripRequirement
from travel_agent.supervisor.state import QualityReport

BUDGET_TOLERANCE = 1.10          # 预算偏差容忍:上限 × 1.10(设计文档 ≤10%)


def check_quality(state: dict) -> QualityReport:
    """六项检查:需求完整性 / 预算偏差 / 时间冲突 / 调整可追溯 / 天气标注 / 数据来源。"""
    checks: dict[str, bool] = {}
    notes: list[str] = []
    failure_type = ""

    requirement: TripRequirement | None = state.get("requirement")
    budget_plan: BudgetPlan | None = state.get("budget_plan")
    itinerary: Itinerary | None = state.get("itinerary")

    # ① 需求完整性:四项关键信息必须齐备
    req_ok = _check_requirement(requirement)
    checks["requirement_complete"] = req_ok
    if not req_ok:
        notes.append("需求不完整:destination/dates/travelers/budget 存在缺失")
        failure_type = failure_type or "模型能力"

    # ② 预算偏差:总分项和 ≤ 预算上限 × 1.10
    #    用户已明确确认超支方案(confirm_result=accepted)时豁免:人工决策高于自动规则
    if budget_plan is not None and state.get("confirm_result") != "accepted":
        budget_ok = _check_budget(budget_plan, requirement)
        checks["budget_deviation"] = budget_ok
        if not budget_ok:
            notes.append("预算偏差超过 10% 上限")
            failure_type = failure_type or "业务规则"

    # ③ 时间冲突:必须为 0 或全部已解决
    if itinerary is not None:
        conflict_ok = all(c.resolution for c in itinerary.conflicts)
        checks["no_unresolved_conflicts"] = conflict_ok
        if not conflict_ok:
            notes.append("存在未解决的时间冲突")
            failure_type = failure_type or "业务规则"

    # ④ 调整可追溯:预算调整四要素 / 天气变更三要素,缺一不可
    if budget_plan is not None:
        trace_ok = all(
            a.reason and a.from_item and a.to_item and a.impact
            for a in budget_plan.adjustments)
        checks["adjustments_traceable"] = trace_ok
        if not trace_ok:
            notes.append("预算调整记录四要素缺失")
            failure_type = failure_type or "业务规则"
    if itinerary is not None:
        trace_ok = all(
            c.reason and c.scope and c.replacement
            for c in itinerary.weather_changes)
        checks["weather_changes_traceable"] = trace_ok
        if not trace_ok:
            notes.append("天气变更记录三要素缺失")
            failure_type = failure_type or "业务规则"

    # ⑤ 天气标注:有室外活动的日期必须有 weather_note
    if itinerary is not None:
        note_ok = all(
            day.weather_note or not any(a.kind == "outdoor" for a in day.activities)
            for day in itinerary.days)
        checks["weather_notes_present"] = note_ok
        if not note_ok:
            notes.append("存在有室外活动但无天气说明的日期")
            failure_type = failure_type or "业务规则"

    # ⑥ 数据来源:每个预算分项必须带依据说明(价格/天气的时间戳由 schema 强制,
    #    交付时间戳由 deliver 节点写入 FinalOutput.data_timestamp)
    if budget_plan is not None:
        src_ok = all(i.note for i in budget_plan.items)
        checks["data_sources_noted"] = src_ok
        if not src_ok:
            notes.append("预算分项缺少依据说明")
            failure_type = failure_type or "业务规则"

    passed = all(checks.values())
    return QualityReport(
        passed=passed,
        checks=checks,
        failure_type=failure_type if not passed else "",
        notes=notes,
    )


def _check_requirement(req: TripRequirement | None) -> bool:
    if req is None:
        return False
    return bool(
        req.destination
        and req.start_date and req.end_date
        and req.travelers is not None
        and req.budget_range is not None
    )


def _check_budget(plan: BudgetPlan, req: TripRequirement | None) -> bool:
    total = sum(i.amount for i in plan.items)
    if req is not None and req.budget_range is not None:
        return total <= req.budget_range.max_cny * BUDGET_TOLERANCE
    return True
