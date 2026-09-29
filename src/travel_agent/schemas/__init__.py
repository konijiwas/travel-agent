"""数据模型聚合导出:全部 Pydantic 模型从此导入。"""

from travel_agent.schemas.options import (
    AvailabilityInfo,
    DailyWeather,
    FlightOption,
    FlightStatus,
    HotelOption,
    WeatherInfo,
)
from travel_agent.schemas.requirement import (
    AnalystOutput,
    BudgetRange,
    ClarifyQuestion,
    PREFERENCE_TAGS,
    TripRequirement,
)

__all__ = [
    "AnalystOutput",
    "AvailabilityInfo",
    "BudgetRange",
    "ClarifyQuestion",
    "DailyWeather",
    "FlightOption",
    "FlightStatus",
    "HotelOption",
    "PREFERENCE_TAGS",
    "TripRequirement",
    "WeatherInfo",
]
