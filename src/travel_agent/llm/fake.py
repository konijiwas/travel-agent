"""离线 Fake LLM:不调用任何 API,按 schema 用确定性规则生成输出。

用途:
- 无 API Key 时跑通全流程(TA_EVAL_MODE=fake 离线冒烟);
- 评测基建与测试(确定性、零成本)。

实现:每种 schema 注册一个生成函数(_GENERATORS);
未注册的 schema 抛 NotImplementedError(M4/M5 再补充生成器)。
"""

import hashlib
import json
import re
from datetime import datetime, timedelta

from travel_agent.schemas.budget import (
    DEFAULT_RATIOS,
    AdjustProposal,
    BudgetAdjustment,
    BudgetItem,
    BudgetPlan,
)
from travel_agent.schemas.itinerary import Activity, Itinerary, ItineraryDay, WeatherChange
from travel_agent.schemas.requirement import (
    AnalystOutput,
    BudgetRange,
    ClarifyQuestion,
    TripRequirement,
)

CITIES = ["北京", "上海", "杭州", "成都", "三亚", "西安",
          "广州", "深圳", "南京", "重庆", "厦门", "青岛"]
_CITY_PAT = "|".join(CITIES)

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 偏好关键词表:按顺序匹配,命中即打标签
_PREF_MAP: list[tuple[str, list[str]]] = [
    ("family", ["亲子", "孩子", "小孩", "带娃", "家庭"]),
    ("food", ["美食", "小吃", "吃货"]),
    ("business", ["商务", "出差", "开会", "展会"]),
    ("adventure", ["户外", "爬山", "徒步", "冒险", "自然", "露营"]),
    ("culture", ["文化", "博物馆", "历史", "古镇", "古迹"]),
    ("relax", ["休闲", "放松", "度假", "海边"]),
]


def _extract_text(input_) -> str:
    """兼容多种输入形态:纯字符串 / langchain 消息列表 / 状态 dict。

    消息列表只取 **human(用户)消息**——系统提示词里的 few-shot 示例
    不能被当作用户输入解析,否则会污染规则提取。
    """
    if isinstance(input_, str):
        return input_
    if isinstance(input_, list):
        parts = []
        for m in input_:
            if getattr(m, "type", "") == "human":
                parts.append(getattr(m, "content", "") or "")
        return "\n".join(parts)
    if isinstance(input_, dict):
        if "messages" in input_:
            return _extract_text(input_["messages"])
        if "user_input" in input_:
            return str(input_["user_input"])
    return str(input_)


def _parse_travelers(text: str) -> int | None:
    """人数:支持 "2人"、"3个人"、"两大一小"、"2个大人1个小孩"。"""
    m = re.search(r"(\d+)\s*个?\s*[大人]", text)
    if m:
        total = int(m.group(1))
        for mm in re.finditer(r"(\d+)\s*个?\s*[小小孩]", text):
            total += int(mm.group(1))
        return total
    m = re.search(r"([一二两三四五六七八九十]+)\s*个?\s*[大人]", text)
    if m:
        return _CN_DIGITS.get(m.group(1), 1)
    m = re.search(r"(\d+)\s*个?\s*人", text)
    if m:
        return int(m.group(1))
    return None


def _parse_dates(text: str) -> tuple[str | None, str | None]:
    """日期:支持 YYYY-MM-DD 与 YYYY年M月D日;含"玩N天"时自动推算结束日。"""
    dates = []
    for m in re.finditer(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", text):
        dates.append(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    start = dates[0] if dates else None
    end = dates[1] if len(dates) > 1 else None
    if start and end is None:
        m = re.search(r"玩\s*(\d+)\s*天", text)
        if m:
            end = (datetime.strptime(start, "%Y-%m-%d")
                   + timedelta(days=int(m.group(1)) - 1)).strftime("%Y-%m-%d")
    return start, end


def _parse_budget(text: str) -> BudgetRange | None:
    """预算:单值"预算6000"→(0,6000);区间"预算5000-8000"→(5000,8000)。"""
    m = re.search(r"预算\s*(\d+(?:\.\d+)?)(?:\s*[-~到至]\s*(\d+(?:\.\d+)?))?", text)
    if not m:
        return None
    low = float(m.group(1))
    high = float(m.group(2)) if m.group(2) else low
    return BudgetRange(min_cny=0.0 if m.group(2) is None else low, max_cny=high)


def _parse_preferences(text: str) -> list[str]:
    return [tag for tag, keywords in _PREF_MAP if any(k in text for k in keywords)]


def _parse_constraints(text: str) -> list[str]:
    return [s.strip(" ,。;!") for s in
            re.findall(r"(?:不要|不想|避免|必须)[^,。;!\n]*", text) if s.strip()]


def _generate_analyst_output(text: str) -> AnalystOutput:
    """规则解析中文旅行需求 → AnalystOutput(确定性)。"""
    dest_m = re.search(rf"(?:去|到)({_CITY_PAT})", text)
    origin_m = re.search(rf"(?:从|自)({_CITY_PAT})(?:出发)?", text)
    start, end = _parse_dates(text)
    travelers = _parse_travelers(text)
    budget = _parse_budget(text)

    missing: list[str] = []
    questions: list[ClarifyQuestion] = []
    if dest_m is None:
        missing.append("destination")
        questions.append(ClarifyQuestion(field="destination", question="请问您想去哪个城市?"))
    if origin_m is None:
        missing.append("origin_city")
        questions.append(ClarifyQuestion(field="origin_city", question="请问从哪个城市出发?"))
    if start is None:
        missing.append("dates")
        questions.append(ClarifyQuestion(field="dates", question="请问计划哪天出发、玩几天?"))
    if travelers is None:
        missing.append("travelers")
        questions.append(ClarifyQuestion(field="travelers", question="请问有几位出行?"))
    if budget is None:
        missing.append("budget")
        questions.append(ClarifyQuestion(field="budget", question="请问预算大约多少?"))

    questions = questions[:2]           # 追问规则:每轮最多 2 个
    if missing:
        return AnalystOutput(missing_fields=missing, clarify_questions=questions)

    return AnalystOutput(
        requirement=TripRequirement(
            destination=dest_m.group(1),
            origin_city=origin_m.group(1) if origin_m else None,
            start_date=start,
            end_date=end,
            travelers=travelers,
            budget_range=budget,
            preferences=_parse_preferences(text),
            constraints=_parse_constraints(text),
        ),
    )


def _generate_budget_plan(text: str) -> BudgetPlan:
    """规则化预算规划:分项分配 → 超支检测 → 优先级链调整 → 超10%交用户确认。

    输入 text 为 JSON 字符串(由 agents/budget/agent.py 构造):
    {"requirement": {...}, "flight_options": [...], "hotel_options": [...]}
    计算规则与 agents/budget/prompts.py 的 SYSTEM_PROMPT 逐条对齐。
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"预算输入不是合法 JSON:{e}") from e
    req = data.get("requirement") or {}
    flights = data.get("flight_options") or []
    hotels = data.get("hotel_options") or []

    budget_max = float(req["budget_range"]["max_cny"])
    travelers = int(req["travelers"])
    start = datetime.strptime(req["start_date"], "%Y-%m-%d")
    end = datetime.strptime(req["end_date"], "%Y-%m-%d")
    nights = max((end - start).days, 1)          # 住宿晚数(至少 1 晚)

    # ── 分项预算(公式与提示词一致)──
    if flights:
        f = min(flights, key=lambda x: x["price_cny"])          # 最低价航班
        transport = f["price_cny"] * travelers * 2
        t_note = f"{f['flight_no']} 往返×{travelers}人(最低价估算)"
    else:
        transport = budget_max * DEFAULT_RATIOS["transport"]
        t_note = "无航班数据,按参考比例 30%"
    if hotels:
        h = min(hotels, key=lambda x: x["price_per_night_cny"])  # 最低价酒店
        hotel_cost = h["price_per_night_cny"] * nights
        h_note = f"{h['name']} ¥{h['price_per_night_cny']}/晚 ×{nights}晚"
    else:
        hotel_cost = budget_max * DEFAULT_RATIOS["hotel"]
        h_note = "无酒店数据,按参考比例 35%"

    items = [
        BudgetItem(category="transport", amount=round(transport, 2), note=t_note),
        BudgetItem(category="hotel", amount=round(hotel_cost, 2), note=h_note),
        BudgetItem(category="food", amount=round(100.0 * travelers * nights, 2),
                   note=f"100元/人/天 ×{travelers}人 ×{nights}天"),
        BudgetItem(category="activities", amount=round(budget_max * 0.15, 2),
                   note="参考比例 15%"),
        BudgetItem(category="buffer", amount=round(budget_max * 0.05, 2),
                   note="机动金 5%"),
    ]
    adjustments: list[BudgetAdjustment] = []

    def _total() -> float:
        return sum(i.amount for i in items)

    # ── 超支检测 ──
    over = _total() - budget_max
    if over <= 0:
        return BudgetPlan(total_budget=round(_total(), 2), items=items)

    # ── 调整优先级链 ──
    # ① 削减非核心活动
    act = items[3]
    cut = min(act.amount, over)
    if cut > 0:
        adjustments.append(BudgetAdjustment(
            reason=f"总价超出预算 {over:.0f} 元,执行优先级①:削减非核心活动",
            from_item=f"activities:¥{act.amount:.0f}",
            to_item=f"activities:¥{act.amount - cut:.0f}",
            impact=f"省 ¥{cut:.0f}"))
        act.amount = round(act.amount - cut, 2)
        act.status = "adjusted"

    # ② 替换更低档位:初始即已选最低价,无更低价可换(无操作空间)
    # ③ 跨类重分配:先压缩 food(不低于 60 元/人/天),再压缩 buffer(最低 0)
    over = _total() - budget_max
    if over > 0:
        food_item = items[2]
        food_min = 60.0 * travelers * nights
        cut = min(food_item.amount - food_min, over)
        if cut > 0:
            adjustments.append(BudgetAdjustment(
                reason=f"仍超支 {over:.0f} 元,执行优先级③:压缩餐饮",
                from_item=f"food:¥{food_item.amount:.0f}",
                to_item=f"food:¥{food_item.amount - cut:.0f}",
                impact=f"省 ¥{cut:.0f}(不低于60元/人/天)"))
            food_item.amount = round(food_item.amount - cut, 2)
            food_item.status = "adjusted"
        over = _total() - budget_max
    if over > 0:
        buf = items[4]
        cut = min(buf.amount, over)
        if cut > 0:
            adjustments.append(BudgetAdjustment(
                reason=f"仍超支 {over:.0f} 元,执行优先级③:压缩机动金",
                from_item=f"buffer:¥{buf.amount:.0f}",
                to_item=f"buffer:¥{buf.amount - cut:.0f}",
                impact=f"省 ¥{cut:.0f}"))
            buf.amount = round(buf.amount - cut, 2)
            buf.status = "adjusted"
        over = _total() - budget_max

    # ④ 仍超预算 10% → 停止自动调整,打包提案交用户确认
    if over > budget_max * 0.1:
        return BudgetPlan(
            total_budget=round(_total(), 2),
            items=items,
            adjustments=adjustments,
            needs_confirmation=True,
            pending_proposal=AdjustProposal(
                overrun_cny=round(over, 2),
                proposal=[],
                note="已按优先级链①③调整(②无更低档可换),仍超预算 10% 阈值,建议用户确认或追加预算",
            ),
        )
    return BudgetPlan(total_budget=round(_total(), 2), items=items,
                      adjustments=adjustments)


# 各城市活动池(室外/室内),供确定性编排使用;未列城市用默认池
_ACTIVITY_POOLS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "杭州": {
        "outdoor": [("西湖游船", "西湖景区"), ("灵隐寺+飞来峰", "西湖景区"), ("河坊街漫步", "吴山景区")],
        "indoor": [("浙江省博物馆", "西湖文化广场"), ("杭州博物馆", "吴山"), ("南宋官窑博物馆", "玉皇山")],
    },
    "北京": {
        "outdoor": [("故宫角楼打卡", "东城区"), ("颐和园", "海淀区"), ("天坛公园", "东城区")],
        "indoor": [("国家博物馆", "天安门"), ("首都博物馆", "西城区"), ("798艺术区", "朝阳区")],
    },
    "上海": {
        "outdoor": [("外滩漫步", "黄浦区"), ("豫园", "黄浦区"), ("世纪公园", "浦东新区")],
        "indoor": [("上海博物馆", "黄浦区"), ("自然博物馆", "静安区"), ("中华艺术宫", "浦东新区")],
    },
    "成都": {
        "outdoor": [("宽窄巷子", "青羊区"), ("人民公园", "青羊区"), ("大熊猫基地", "成华区")],
        "indoor": [("成都博物馆", "青羊区"), ("金沙遗址博物馆", "青羊区"), ("四川科技馆", "青羊区")],
    },
    "三亚": {
        "outdoor": [("亚龙湾沙滩", "亚龙湾"), ("天涯海角", "天涯区"), ("南山文化旅游区", "崖州区")],
        "indoor": [("三亚千古情演出", "吉阳区"), ("三亚市博物馆", "吉阳区"), ("国际免税城", "海棠区")],
    },
    "西安": {
        "outdoor": [("大雁塔广场", "雁塔区"), ("城墙骑行", "碑林区"), ("华清宫", "临潼区")],
        "indoor": [("陕西历史博物馆", "雁塔区"), ("西安博物院", "碑林区"), ("碑林博物馆", "碑林区")],
    },
}
_DEFAULT_POOL: dict[str, list[tuple[str, str]]] = {
    "outdoor": [("城市公园漫步", "市中心"), ("地标景点观光", "市中心")],
    "indoor": [("城市博物馆", "市中心"), ("当地特色展馆", "市中心")],
}


def _add_minutes(hhmm: str, minutes: int) -> str:
    """"HH:MM" + 分钟数 → "HH:MM"(支持跨天进位)。"""
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def _weather_for(weather: list[dict], date: str) -> dict | None:
    for w in weather:
        if w.get("date") == date:
            return w
    return None


def _pick(pool: list[tuple[str, str]], date: str, slot: str) -> tuple[str, str]:
    """按 日期+时段 哈希确定性选活动:同一天同一时段永远同一个活动。"""
    seed = int(hashlib.md5(f"{date}|{slot}".encode("utf-8")).hexdigest(), 16)
    return pool[seed % len(pool)]


def _generate_itinerary(text: str) -> Itinerary:
    """规则化行程编排:逐日安排 → 2h 缓冲 → 天气替换室外活动(三要素留痕)。

    输入 text 为 JSON 字符串(由 agents/executor/agent.py 构造):
    {"requirement": {...}, "budget_plan": {...}, "weather": [...],
     "flight_options": [...], "hotel_options": [...]}
    规则与 agents/executor/prompts.py 的 SYSTEM_PROMPT 逐条对齐。
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"行程输入不是合法 JSON:{e}") from e
    req = data.get("requirement") or {}
    weather = data.get("weather") or []
    flights = data.get("flight_options") or []
    hotels = data.get("hotel_options") or []

    city = req.get("destination", "")
    start = datetime.strptime(req["start_date"], "%Y-%m-%d")
    end = datetime.strptime(req["end_date"], "%Y-%m-%d")
    dates: list[str] = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    n_days = len(dates)

    flight = min(flights, key=lambda x: x["price_cny"]) if flights else None
    hotel_name = (min(hotels, key=lambda x: x["price_per_night_cny"])["name"]
                  if hotels else "酒店(待定)")
    arrival = (datetime.strptime(flight["arrival_ts"], "%Y-%m-%dT%H:%M:%S").strftime("%H:%M")
               if flight else "10:00")
    pools = _ACTIVITY_POOLS.get(city, _DEFAULT_POOL)

    days: list[ItineraryDay] = []
    changes: list[WeatherChange] = []

    for idx, date in enumerate(dates):
        w = _weather_for(weather, date)
        activities: list[Activity] = []
        weather_note = ""
        is_arrival = idx == 0
        is_return = idx == n_days - 1

        # 首日:抵达航班(transport),其后活动从 到达+2h 开始(缓冲规则)
        if is_arrival:
            activities.append(Activity(
                name=f"抵达航班 {flight['flight_no']}" if flight else "抵达(航班待定)",
                start_time=arrival, end_time=_add_minutes(arrival, 30),
                kind="transport",
                note=f"航班 {flight['flight_no']}" if flight else "航班待定"))
            first_start = _add_minutes(arrival, 120)
        else:
            first_start = "09:00"

        # 主活动槽(上午/首日):按天气决定 outdoor 或 indoor
        if w is None:
            name, _ = _pick(pools["outdoor"], date, "main")
            weather_note = "天气未知,建议出行前确认"       # 规则6:不替换,如实标注
        elif w["is_outdoor_friendly"]:
            name, _ = _pick(pools["outdoor"], date, "main")
            weather_note = f"{w['condition']} {w['temp_c']}°C"
        else:
            outdoor_name, _ = _pick(pools["outdoor"], date, "main")
            name, _ = _pick(pools["indoor"], date, "main")
            weather_note = f"{w['condition']} {w['temp_c']}°C,{outdoor_name}改为{name}"
            changes.append(WeatherChange(
                day=date,
                reason=f"{date} 预报{w['condition']}",
                scope=f"{outdoor_name}(室外)",
                replacement=f"改为{name}(室内)"))

        activities.append(Activity(
            name=name, start_time=first_start, end_time=_add_minutes(first_start, 120),
            kind="indoor" if (w is not None and not w["is_outdoor_friendly"]) else "outdoor",
            note="雨天由室外替换而来" if (w is not None and not w["is_outdoor_friendly"]) else ""))

        # 返程日:午餐后返程,返程前 2h 缓冲;非返程日:下午室内 + 晚餐
        # 各活动开始时间 = max(常规时间, 上一活动结束时间),保证永不重叠
        if is_return:
            lunch_start = max("12:00", activities[-1].end_time)
            activities.append(Activity(
                name="当地特色餐(午餐)", start_time=lunch_start,
                end_time=_add_minutes(lunch_start, 60), kind="meal"))
            return_start = max("15:30", _add_minutes(activities[-1].end_time, 120))
            activities.append(Activity(
                name=f"返程航班 {flight['flight_no']}(估算)" if flight else "返程(航班待定)",
                start_time=return_start, end_time=_add_minutes(return_start, 70),
                kind="transport", note="返程按去程航班估算"))
        else:
            aft_start = max("14:00", activities[-1].end_time)
            activities.append(Activity(
                name=_pick(pools["indoor"], date, "afternoon")[0],
                start_time=aft_start, end_time=_add_minutes(aft_start, 120), kind="indoor"))
            dinner_start = max("18:00", activities[-1].end_time)
            activities.append(Activity(
                name="当地特色餐(晚餐)", start_time=dinner_start,
                end_time=_add_minutes(dinner_start, 90), kind="meal"))

        days.append(ItineraryDay(
            date=date, activities=activities,
            transport=flight["flight_no"] if flight else "待定",
            hotel=hotel_name, weather_note=weather_note))

    return Itinerary(days=days, weather_changes=changes, conflicts=[])


_GENERATORS: dict = {
    AnalystOutput: _generate_analyst_output,
    BudgetPlan: _generate_budget_plan,
    Itinerary: _generate_itinerary,
}


class FakeStructuredLLM:
    """离线结构化输出 LLM:按 schema 查注册表,用确定性规则生成结果。"""

    def __init__(self, schema):
        self.schema = schema

    def invoke(self, input_, *args, **kwargs):
        gen = _GENERATORS.get(self.schema)
        if gen is None:
            raise NotImplementedError(
                f"FakeLLM 未注册 schema:{getattr(self.schema, '__name__', self.schema)}")
        return gen(_extract_text(input_))
