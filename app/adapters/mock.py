"""D 级模拟数据源。仅用于开发联调，界面必须显示「模拟数据」标识。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.adapters.base import Capabilities
from app.db.models import Bar, Quote, Tick

MOCK_UNIVERSE = [
    ("600000.SH", "示例银行"),
    ("600519.SH", "示例白酒"),
    ("000001.SZ", "示例股份"),
    ("300001.SZ", "示例创业"),
    ("002594.SZ", "示例汽车"),
    ("601318.SH", "示例保险"),
]


class MockAdapter:
    data_level = "D"

    def __init__(self, seed: int = 20261108) -> None:
        self._rng = random.Random(seed)
        self._subscribed: set[str] = set()

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            realtime=False,
            tick=True,
            order_book_levels=0,
            data_level="D",
            note="模拟数据，不可用于任何交易决策",
        )

    def get_stock_list(self) -> list[str]:
        """返回代码列表（与 tqcenter 适配器保持同一返回类型）。"""
        return [symbol for symbol, _ in MOCK_UNIVERSE]

    def get_quote(self, symbol: str) -> Quote:
        name = dict(MOCK_UNIVERSE).get(symbol, "未知标的")
        base = 8 + (abs(hash(symbol)) % 4000) / 100
        now = datetime.now()
        price = round(base * (1 + self._rng.uniform(-0.03, 0.03)), 2)
        prev_close = round(base, 2)
        return Quote(
            symbol=symbol,
            name=name,
            price=price,
            prev_close=prev_close,
            open=round(prev_close * 1.005, 2),
            high=round(max(price, prev_close) * 1.01, 2),
            low=round(min(price, prev_close) * 0.99, 2),
            volume=self._rng.randint(100_000, 9_000_000),
            amount=round(self._rng.uniform(5e7, 2e9), 2),
            turnover_rate=round(self._rng.uniform(0.3, 9.0), 2),
            volume_ratio=round(self._rng.uniform(0.5, 4.0), 2),
            limit_up_price=round(prev_close * 1.1, 2),
            limit_down_price=round(prev_close * 0.9, 2),
            data_level="D",
            source="mock",
            quote_time=now,
            received_at=now,
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {symbol: self.get_quote(symbol) for symbol in symbols}

    def get_kline(self, symbol: str, period: str, start: datetime | None = None,
                  end: datetime | None = None, limit: int = 500) -> list[Bar]:
        bars: list[Bar] = []
        price = 10.0
        start_time = datetime.now() - timedelta(days=limit)
        for i in range(limit):
            open_ = price
            close = round(open_ * (1 + self._rng.uniform(-0.04, 0.04)), 2)
            bars.append(Bar(
                symbol=symbol, period=period,
                bar_time=start_time + timedelta(days=i),
                open=open_, high=round(max(open_, close) * 1.01, 2),
                low=round(min(open_, close) * 0.99, 2), close=close,
                volume=self._rng.randint(10_000, 900_000),
                data_level="D", source="mock",
            ))
            price = close
        return bars

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]:
        ticks: list[Tick] = []
        cursor = start
        seq = 0
        while cursor <= end:
            seq += 1
            price = round(self._rng.uniform(9.5, 10.5), 2)
            volume = self._rng.choice([100, 200, 500, 1000, 2000, 5000, 20000])
            ticks.append(Tick(
                symbol=symbol, trade_time=cursor, price=price, volume=volume,
                amount=round(price * volume, 2),
                direction=self._rng.choice(["buy", "sell", "unknown"]),
                seq=seq, data_level="D", source="mock",
            ))
            cursor += timedelta(milliseconds=self._rng.randint(300, 3000))
        return ticks

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        self._subscribed.update(symbols)

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        self._subscribed.difference_update(symbols)
