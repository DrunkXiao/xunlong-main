"""适配器协议：业务层只依赖这些接口，禁止硬编码供应商。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.db.models import Bar, Quote, Tick


@dataclass(slots=True)
class Capabilities:
    """数据源能力矩阵。UI 据此决定是否渲染面板，而不渲染空面板。"""
    realtime: bool = False
    tick: bool = False
    order_book_levels: int = 0
    minute_kline: bool = False
    lhb: bool = False
    sector: bool = False
    subscribe_max: int = 0
    data_level: str = "D"
    note: str = ""

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, feature, False))

    def summary(self) -> str:
        items = [
            f"实时={'是' if self.realtime else '否'}",
            f"逐笔={'是' if self.tick else '否'}",
            f"盘口={self.order_book_levels}档",
            f"分钟K={'是' if self.minute_kline else '否'}",
            f"龙虎榜={'是' if self.lhb else '否'}",
        ]
        if self.subscribe_max:
            items.append(f"订阅上限={self.subscribe_max}")
        return " | ".join(items)


class DataSourceError(Exception):
    def __init__(self, kind: str, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


@runtime_checkable
class MarketDataAdapter(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...
    def get_stock_list(self) -> list[Quote]: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def get_kline(self, symbol: str, period: str, start: datetime | None = None,
                  end: datetime | None = None, limit: int = 500) -> list[Bar]: ...
    async def subscribe_quotes(self, symbols: list[str]) -> None: ...
    async def unsubscribe_quotes(self, symbols: list[str]) -> None: ...


@runtime_checkable
class TradeDataAdapter(Protocol):
    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]: ...
    async def subscribe_ticks(self, symbol: str) -> None: ...
    def get_order_book(self, symbol: str): ...


@runtime_checkable
class IndustryDataAdapter(Protocol):
    def get_industry(self, symbol: str) -> dict: ...
    def get_concepts(self, symbol: str) -> list[str]: ...
    def get_sector_quotes(self, sector: str) -> dict: ...
    def get_sector_constituents(self, sector: str) -> list[str]: ...


@runtime_checkable
class DragonTigerAdapter(Protocol):
    def get_lhb_records(self, symbol: str, start, end) -> list[dict]: ...
    def get_lhb_detail(self, trade_date, symbol: str) -> dict | None: ...
