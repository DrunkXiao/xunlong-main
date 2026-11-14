"""预警引擎：条件匹配 -> 去重冷却 -> 通知 -> 落库。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app.db.models import AlertEvent, Quote

LEVEL_NOTIFY = {
    "P1": ["popup", "sound", "log"],
    "P2": ["popup", "sound", "log"],
    "P3": ["log"],
    "P4": ["log"],
}


class AlertEngine:
    def __init__(self, cooldown_sec: int = 300, per_symbol_per_minute: int = 5) -> None:
        self.cooldown_sec = cooldown_sec
        self.per_symbol_per_minute = per_symbol_per_minute
        self._last_fired: dict[tuple[str, str, str], tuple[datetime, float]] = {}
        self._minute_count: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- 去重
    def reset_cooldown(self) -> None:
        """规则增删改后清空冷却状态（规则变更必须即时生效）。"""
        self._last_fired.clear()
        self._minute_count.clear()

    def should_fire(self, symbol: str, rule_id: str, event_type: str,
                    price: float, now: datetime) -> bool:
        key = (str(rule_id), symbol, event_type)
        last = self._last_fired.get(key)
        if last:
            last_time, last_price = last
            if (now - last_time).total_seconds() < self.cooldown_sec:
                return False
            # 同值抑制：价格几乎未变视为同一事件延续
            if last_price and abs(price - last_price) <= max(0.01, last_price * 0.001):
                return False
        minute_key = (symbol, now.strftime("%Y%m%d%H%M"))
        if self._minute_count.get(minute_key, 0) >= self.per_symbol_per_minute:
            return False
        self._last_fired[key] = (now, price)
        self._minute_count[minute_key] = self._minute_count.get(minute_key, 0) + 1
        return True

    # ---------------------------------------------------------------- 规则
    def check_price_rules(self, quote: Quote, now: datetime | None = None) -> list[AlertEvent]:
        now = now or datetime.now()
        events: list[AlertEvent] = []
        if quote.price is None or quote.data_level == "D":
            return events

        if quote.limit_up_price:
            gap = (quote.limit_up_price - quote.price) / quote.limit_up_price * 100
            if 0 <= gap <= 1.0 and self.should_fire(quote.symbol, "near_limit_up", "near_limit_up", quote.price, now):
                events.append(AlertEvent(
                    symbol=quote.symbol, name=quote.name, event_type="near_limit_up",
                    trigger_price=quote.price, trigger_time=now,
                    condition=f"distance_to_limit_up={gap:.2f}%", level="P2",
                    data_level=quote.data_level, source=quote.source,
                ))
        if quote.limit_down_price:
            gap = (quote.price - quote.limit_down_price) / quote.limit_down_price * 100
            if 0 <= gap <= 1.0 and self.should_fire(quote.symbol, "near_limit_down", "near_limit_down", quote.price, now):
                events.append(AlertEvent(
                    symbol=quote.symbol, name=quote.name, event_type="near_limit_down",
                    trigger_price=quote.price, trigger_time=now,
                    condition=f"distance_to_limit_down={gap:.2f}%", level="P2",
                    data_level=quote.data_level, source=quote.source,
                ))
        return events

    # ---------------------------------------------------------------- 落库
    @staticmethod
    def persist(cur: sqlite3.Cursor, event: AlertEvent) -> None:
        cur.execute(
            """INSERT INTO alert_events
               (symbol, name, event_type, trigger_price, trigger_time, condition,
                volume, amount, data_level, source, level, handled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.symbol, event.name, event.event_type, event.trigger_price,
             event.trigger_time.isoformat(timespec="seconds"), event.condition,
             event.volume, event.amount, event.data_level, event.source,
             event.level, event.handled),
        )


def format_message(event: AlertEvent) -> str:
    """通知文案：只陈述事实，不含买卖建议。"""
    return (f"{event.name} {event.event_type}"
            f"（{event.trigger_price:.2f}，{event.trigger_time.strftime('%H:%M:%S')}）")
