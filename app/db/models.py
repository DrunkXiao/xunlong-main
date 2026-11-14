"""领域数据结构。所有行情对象必须携带 source / data_level / timestamp。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

DATA_LEVELS = ("A", "B", "C", "D")
LEVEL_LABEL = {
    "A": "实时（授权）",
    "B": "延时",
    "C": "历史",
    "D": "模拟数据",
}


@dataclass(slots=True)
class Quote:
    symbol: str
    name: str
    price: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    limit_up_price: float | None = None
    limit_down_price: float | None = None
    inside_volume: int | None = None    # 内盘（主动卖），股
    outside_volume: int | None = None   # 外盘（主动买），股
    data_level: str = "D"
    source: str = ""
    quote_time: datetime | None = None
    received_at: datetime | None = None
    time_source: str = "exchange"   # exchange 交易所时间 / local_receive 本地接收时间

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100

    @property
    def latency_ms(self) -> int | None:
        if self.quote_time is None or self.received_at is None:
            return None
        return int((self.received_at - self.quote_time).total_seconds() * 1000)


@dataclass(slots=True)
class Bar:
    symbol: str
    period: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0
    data_level: str = "C"
    source: str = ""
    quality_flag: str = "exchange"   # exchange 交易所K线 / sampled 快照采样


@dataclass(slots=True)
class Tick:
    symbol: str
    trade_time: datetime
    price: float
    volume: int
    amount: float
    direction: str = "unknown"  # buy / sell / unknown
    seq: int = 0
    data_level: str = "C"
    source: str = ""
    quality_flag: str = "ok"


@dataclass(slots=True)
class AlertEvent:
    symbol: str
    name: str
    event_type: str
    trigger_price: float
    trigger_time: datetime
    condition: str
    level: str = "P3"
    volume: int | None = None
    amount: float | None = None
    data_level: str = "C"
    source: str = ""
    handled: int = 0
    extra: dict = field(default_factory=dict)
