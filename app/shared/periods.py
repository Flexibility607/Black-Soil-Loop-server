from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.shared.errors import BusinessError
from app.shared.models import utcnow

SHANGHAI = ZoneInfo("Asia/Shanghai")
PERIOD_VALUES = ("7d", "30d", "month")


@dataclass(frozen=True)
class PeriodWindow:
    code: str
    start_date: date
    end_date: date
    start_at: datetime
    end_at: datetime


def period_window(period: str, now: datetime | None = None) -> PeriodWindow:
    if period not in PERIOD_VALUES:
        raise BusinessError(
            "INVALID_PERIOD",
            "统计周期仅支持 7d、30d 或 month",
            status_code=422,
            details={"period": period},
        )
    local_now = (now or utcnow()).astimezone(SHANGHAI)
    end_date = local_now.date()
    if period == "7d":
        start_date = end_date - timedelta(days=6)
    elif period == "30d":
        start_date = end_date - timedelta(days=29)
    else:
        start_date = end_date.replace(day=1)
    start_at = datetime.combine(start_date, time.min, tzinfo=SHANGHAI).astimezone(UTC)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=SHANGHAI).astimezone(UTC)
    return PeriodWindow(period, start_date, end_date, start_at, end_at)
