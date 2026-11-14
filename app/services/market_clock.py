"""A股交易时钟：市场状态、轮询节流、行情新鲜度判定。

交易日来源优先级：数据源交易日历（可用时）> 本地近似（周一到周五）。
近似模式必须在界面标注，不能假装权威。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

AUCTION_START = time(9, 15)
AUCTION_END = time(9, 25)
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

STATUS_AUCTION = "集合竞价"
STATUS_TRADING = "交易中"
STATUS_LUNCH = "午间休市"
STATUS_CLOSED = "已收盘"
STATUS_PREOPEN = "盘前"
STATUS_HOLIDAY = "非交易日"


@dataclass(slots=True)
class MarketStatus:
    label: str
    is_trading: bool          # 是否处于连续竞价（决定是否触发信号）
    is_auction: bool
    calendar_source: str      # api / approximate
    trading_date: date
    detail: str = ""

    @property
    def color_key(self) -> str:
        if self.is_trading:
            return "up"
        if self.is_auction:
            return "warn"
        return "text_dim"


class MarketClock:
    def __init__(self, calendar_source: str = "approximate") -> None:
        self.calendar_source = calendar_source
        self._trading_dates: set[str] | None = None

    def set_trading_dates(self, dates: list[str]) -> None:
        """dates 形如 ['20261109', ...]；传入后日历来源升级为 api。"""
        if dates:
            self._trading_dates = {str(d).replace("-", "") for d in dates}
            self.calendar_source = "api"

    def is_trading_day(self, day: date) -> bool:
        key = day.strftime("%Y%m%d")
        if self._trading_dates is not None:
            return key in self._trading_dates
        return day.weekday() < 5

    def status(self, now: datetime | None = None) -> MarketStatus:
        now = now or datetime.now()
        today = now.date()
        clock = now.time()
        if not self.is_trading_day(today):
            return MarketStatus(STATUS_HOLIDAY, False, False, self.calendar_source, today,
                                "非交易日，仅回放历史数据")
        if clock < AUCTION_START:
            return MarketStatus(STATUS_PREOPEN, False, False, self.calendar_source, today,
                                "盘前，尚无连续竞价数据")
        if clock < AUCTION_END:
            return MarketStatus(STATUS_AUCTION, False, True, self.calendar_source, today,
                                "集合竞价阶段，价格不连续")
        if clock < MORNING_OPEN:
            return MarketStatus(STATUS_PREOPEN, False, False, self.calendar_source, today,
                                "竞价结束待开盘")
        if clock < MORNING_CLOSE:
            return MarketStatus(STATUS_TRADING, True, False, self.calendar_source, today, "上午连续竞价")
        if clock < AFTERNOON_OPEN:
            return MarketStatus(STATUS_LUNCH, False, False, self.calendar_source, today,
                                "午间休市，行情不更新")
        if clock < AFTERNOON_CLOSE:
            return MarketStatus(STATUS_TRADING, True, False, self.calendar_source, today, "下午连续竞价")
        return MarketStatus(STATUS_CLOSED, False, False, self.calendar_source, today, "已收盘")

    def poll_interval(self, base_interval: float, now: datetime | None = None) -> float:
        """休市时降频，避免无意义请求；交易时段用配置值。"""
        status = self.status(now)
        if status.is_trading or status.is_auction:
            return base_interval
        return max(base_interval, 30.0)

    @staticmethod
    def session_elapsed_ratio(now: datetime | None = None) -> float:
        """分时图横轴位置：0=09:30，1=15:00，午休压缩掉。"""
        now = now or datetime.now()
        clock = now.time()
        morning_minutes = 120
        if clock <= MORNING_OPEN:
            return 0.0
        if clock <= MORNING_CLOSE:
            return _minutes(MORNING_OPEN, clock) / 240
        if clock <= AFTERNOON_OPEN:
            return morning_minutes / 240
        if clock <= AFTERNOON_CLOSE:
            return (morning_minutes + _minutes(AFTERNOON_OPEN, clock)) / 240
        return 1.0

    @staticmethod
    def is_stale(quote_time: datetime | None, now: datetime | None = None,
                 threshold_sec: float = 30.0) -> bool:
        if quote_time is None:
            return True
        now = now or datetime.now()
        return (now - quote_time).total_seconds() > threshold_sec

    @staticmethod
    def next_open(now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        day = now.date()
        for _ in range(10):
            if day.weekday() < 5:
                candidate = datetime.combine(day, AUCTION_START)
                if candidate > now:
                    return candidate
            day = day + timedelta(days=1)
        return now


def _minutes(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
