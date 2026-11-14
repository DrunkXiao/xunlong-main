"""测试替身：不连 tqcenter 的确定性行情源。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.adapters.base import Capabilities, DataSourceError
from app.db.models import Bar, Quote


@dataclass
class FakeOrderBook:
    symbol: str
    bids: list[tuple[float, int]] = field(default_factory=list)
    asks: list[tuple[float, int]] = field(default_factory=list)
    inside: int = 0
    outside: int = 0


class FakeAdapter:
    """行情随轮询小幅波动，便于验证采样差分与界面刷新。"""

    def __init__(self, symbols: list[str] | None = None, base_price: float = 10.0) -> None:
        self._symbols = symbols or ["600000.SH", "000001.SZ", "300750.SZ"]
        self.base_price = base_price
        self.calls = 0
        self._volumes = {symbol: 100_000 for symbol in self._symbols}
        self._inside = {symbol: 40_000 for symbol in self._symbols}
        self._outside = {symbol: 60_000 for symbol in self._symbols}
        self._amounts = {symbol: 1_000_000.0 for symbol in self._symbols}

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            realtime=True, tick=False, order_book_levels=5, minute_kline=False,
            lhb=False, sector=False, subscribe_max=100, data_level="A",
            note="测试替身：仅用于单元测试",
        )

    def get_stock_list(self) -> list[str]:
        return list(self._symbols)

    def get_quote(self, symbol: str) -> Quote:
        if symbol not in self._symbols:
            raise DataSourceError("symbol", f"未知代码 {symbol}", retryable=False)
        self.calls += 1
        drift = (self.calls % 5) * 0.01
        price = round(self.base_price + drift, 2)
        self._volumes[symbol] += 1000
        self._inside[symbol] += 400
        self._outside[symbol] += 600
        self._amounts[symbol] += price * 1000
        now = datetime.now()
        return Quote(
            symbol=symbol, name=f"测试{symbol[:6]}", price=price, prev_close=self.base_price - 0.1,
            open=self.base_price, high=price + 0.05, low=self.base_price - 0.05,
            volume=self._volumes[symbol], amount=self._amounts[symbol],
            turnover_rate=2.5, volume_ratio=1.8,
            limit_up_price=round((self.base_price - 0.1) * 1.1, 2),
            limit_down_price=round((self.base_price - 0.1) * 0.9, 2),
            inside_volume=self._inside[symbol], outside_volume=self._outside[symbol],
            data_level="A", source="fake", quote_time=now, received_at=now,
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for symbol in symbols:
            try:
                out[symbol] = self.get_quote(symbol)
            except DataSourceError:
                continue
        return out

    def get_kline(self, symbol: str, period: str, start=None, end=None, limit: int = 400) -> list[Bar]:
        if period in {"1m", "5m", "15m", "30m", "60m"}:
            raise DataSourceError("unsupported", "测试替身不提供分钟线", retryable=False)
        bars = []
        base = datetime(2026, 1, 5, 15, 0)
        for idx in range(min(limit, 120)):
            stamp = base + timedelta(days=idx)
            close = self.base_price + (idx % 7) * 0.1
            bars.append(Bar(
                symbol=symbol, period=period, bar_time=stamp,
                open=close - 0.05, high=close + 0.1, low=close - 0.1, close=close,
                volume=100_000 + idx * 1000, amount=(100_000 + idx * 1000) * close,
                data_level="C", source="fake",
            ))
        return bars

    def get_order_book(self, symbol: str) -> FakeOrderBook:
        price = self.base_price
        return FakeOrderBook(
            symbol=symbol,
            bids=[(round(price - 0.01 * (i + 1), 2), 1200 + i * 100) for i in range(5)],
            asks=[(round(price + 0.01 * (i + 1), 2), 900 + i * 100) for i in range(5)],
            inside=self._inside.get(symbol, 0), outside=self._outside.get(symbol, 0),
        )

    def disconnect(self) -> None:
        pass
