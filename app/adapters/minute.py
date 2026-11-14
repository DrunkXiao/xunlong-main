"""分时（分钟线）公共数据类型。

分时来源现在有两条线上渠道：东方财富 trends2、free-stockdb 本地库。
两者取数协议完全不同，但**交给上层的东西必须是同一个形状**，否则
`MinuteFeed`、大单识别、分时图、落库都要写两套分支。

因此这里只放结构，不放取数逻辑：

- `MinuteBar`：一根分钟线，`volume` 一律是**股**，`amount` 是**元**。
  单位换算必须在适配器里做完，进到这里就是统一口径。
- `Trends`：某只股票某一天的整日分钟序列 + 元信息。
  `minute_flag` 说明这批点来自哪个来源，落库/画图/大单都靠它区分，
  不能两个来源都写 `em_minute` 互相冒充。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# 分钟线来源标记。quality_flag 以它开头，如 "em_minute,price_proxy"。
EM_FLAG = "em_minute"
STOCKDB_FLAG = "stockdb_minute"
MINUTE_FLAGS: tuple[str, ...] = (EM_FLAG, STOCKDB_FLAG)


@dataclass(slots=True)
class MinuteBar:
    """一根分钟线。volume 已换算为股，amount 为元。"""

    time: datetime
    open: float
    close: float
    high: float
    low: float
    volume: int = 0
    amount: float = 0.0
    avg_price: float | None = None
    volume_hand: int = 0        # 原始手数，留档方便与行情软件对账

    @property
    def price(self) -> float:
        return self.close


@dataclass(slots=True)
class Trends:
    """某只股票的整日分钟序列。"""

    symbol: str
    name: str = ""
    pre_close: float | None = None
    day: date | None = None
    bars: list[MinuteBar] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    source: str = ""
    data_level: str = "B"
    minute_flag: str = EM_FLAG

    @property
    def total_volume(self) -> int:
        return sum(b.volume for b in self.bars)

    @property
    def total_amount(self) -> float:
        return sum(b.amount for b in self.bars)

    @property
    def last(self) -> MinuteBar | None:
        return self.bars[-1] if self.bars else None
