"""M4 预算规划师测试:未超支 / 超支自动调整 / 严重超支交确认 / 无数据兜底。"""

from datetime import datetime

from travel_agent.agents.budget.agent import BudgetPlanner
from travel_agent.schemas.options import FlightOption, HotelOption
from travel_agent.schemas.requirement import BudgetRange, TripRequirement


# ── 构造辅助 ──

def _req(max_cny: float, travelers: int = 1) -> TripRequirement:
    """10-01 至 10-03,住 2 晚。"""
    return TripRequirement(
        destination="杭州", origin_city="上海",
        start_date="2026-10-01", end_date="2026-10-03",
        travelers=travelers,
        budget_range=BudgetRange(min_cny=0.0, max_cny=max_cny),
    )


def _flights(price: float) -> list[FlightOption]:
    return [FlightOption(
        flight_no="MU5131", airline="东方航空", origin="上海", destination="杭州",
        departure_ts="2026-10-01T09:10:00", arrival_ts="2026-10-01T10:20:00",
        price_cny=price, cabin="经济舱", availability=5, score=0.8,
        source_ts=datetime(2026, 9, 30, 12, 0, 0),
    )]


def _hotels(price: float) -> list[HotelOption]:
    return [HotelOption(
        hotel_id="HTL-1", name="杭州湖畔假日酒店", city="杭州", area="市中心",
        distance_to_center_km=2.0, price_per_night_cny=price, room_type="标准间",
        rating=4.6, amenities=["免费WiFi"], availability=5,
        source_ts=datetime(2026, 9, 30, 12, 0, 0),
    )]


def _assert_total_identity(plan):
    """恒等式:total_budget 必须等于各分项之和。"""
    assert plan.total_budget == round(sum(i.amount for i in plan.items), 2)


def _assert_adjustment_traceable(adj):
    """四要素:reason / from_item / to_item / impact 缺一不可。"""
    assert adj.reason and adj.from_item and adj.to_item and adj.impact


# ── 测试 ──

def test_no_overrun_returns_clean_plan():
    """预算充足:无调整、无需确认、恒等式成立。"""
    plan = BudgetPlanner().plan_budget(_req(10000), _flights(780), _hotels(620))
    assert plan.needs_confirmation is False
    assert plan.adjustments == []
    assert plan.pending_proposal is None
    _assert_total_identity(plan)
    # 各分项 note 有依据
    assert all(i.note for i in plan.items)


def test_overrun_auto_adjusted_within_threshold():
    """超支但链①③能压回:自动调整,无需确认,调整全程可追溯。

    金额推演(1人,2晚,预算3000):
    交通 780×1×2=1560,住宿 620×2=1240,餐饮 200,活动 450,机动 150 → 3600,超 600
    ① 活动砍 450 → 超 150;③ 餐饮压到下限 120(省 80)→ 超 70;③ 机动砍 70 → 达标
    """
    plan = BudgetPlanner().plan_budget(_req(3000), _flights(780), _hotels(620))
    assert plan.needs_confirmation is False
    assert plan.pending_proposal is None
    assert len(plan.adjustments) == 3
    for adj in plan.adjustments:
        _assert_adjustment_traceable(adj)
    by_cat = {i.category: i for i in plan.items}
    assert by_cat["activities"].amount == 0.0
    assert by_cat["activities"].status == "adjusted"
    assert by_cat["food"].amount == 120.0
    assert by_cat["buffer"].amount == 80.0
    _assert_total_identity(plan)
    # 达标:总额不超过预算上限(轻微超支 ≤10% 允许,此处应恰好压平)
    assert plan.total_budget <= 3000 * 1.1


def test_severe_overrun_needs_confirmation():
    """链走完仍超 10%:停止自动调整,打包提案交用户确认。

    金额推演(预算2000):总额 3400,超 1400
    ① 活动砍 300 → 超 1100;③ 餐饮压 80 → 超 1020;③ 机动砍 100 → 仍超 920 > 200(10%)
    """
    plan = BudgetPlanner().plan_budget(_req(2000), _flights(780), _hotels(620))
    assert plan.needs_confirmation is True
    assert plan.pending_proposal is not None
    assert plan.pending_proposal.overrun_cny == 920.0
    assert len(plan.adjustments) == 3
    for adj in plan.adjustments:
        _assert_adjustment_traceable(adj)
    _assert_total_identity(plan)


def test_missing_price_data_falls_back_to_ratios():
    """航班/酒店数据为空:按参考比例兜底,不崩溃,note 注明。"""
    plan = BudgetPlanner().plan_budget(_req(6000, travelers=2), [], [])
    assert plan.needs_confirmation is False
    transport = next(i for i in plan.items if i.category == "transport")
    hotel = next(i for i in plan.items if i.category == "hotel")
    assert "无航班数据" in transport.note
    assert "无酒店数据" in hotel.note
    _assert_total_identity(plan)
