"""行程侧数据模型:Itinerary / 每日安排 / 活动 / 天气变更 / 时间冲突。"""

from pydantic import BaseModel, Field


class Activity(BaseModel):
    """一个活动。kind 取值:outdoor(室外)/ indoor(室内)/ transport(交通)/ meal(餐饮)。

    kind 是天气变更逻辑的关键:雨雪天气只替换 outdoor 活动。
    """

    name: str
    start_time: str            # "HH:MM"
    end_time: str              # "HH:MM"
    kind: str = "outdoor"
    note: str = ""             # 说明,如 "雨天由室外替换而来"


class ItineraryDay(BaseModel):
    """一天的安排。weather_note:有室外活动时必须非空(质量门⑤校验)。"""

    date: str                  # YYYY-MM-DD
    activities: list[Activity] = Field(default_factory=list)
    transport: str = ""        # 当日交通说明,如 "MU5131 09:10 抵达"
    hotel: str = ""            # 当日住宿,如 "杭州湖畔假日酒店"
    weather_note: str = ""


class WeatherChange(BaseModel):
    """一次天气导致的行程变更:原因 / 影响范围 / 替换方案三要素(可追溯)。

    对应设计文档:"变更必须记录原因、影响范围与替换方案"。
    """

    day: str                   # 受影响的日期
    reason: str                # 原因,如 "10-02 预报小雨"
    scope: str                 # 影响范围,如 "西湖游船(室外)"
    replacement: str           # 替换方案,如 "改为浙江省博物馆(室内),10-03 补游船"


class Conflict(BaseModel):
    """一个时间冲突。resolution:解决方式(质量门③校验冲突必须为 0 或已解决)。"""

    day: str
    description: str           # 如 "西湖游船 14:00-15:30 与返程航班 14:30 重叠"
    resolution: str = ""


class Itinerary(BaseModel):
    """行程执行者产出:逐日行程 + 天气变更记录 + 冲突记录。"""

    days: list[ItineraryDay] = Field(default_factory=list)
    weather_changes: list[WeatherChange] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
