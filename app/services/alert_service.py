"""预警服务：规则从 SQLite 读，事件写回 SQLite。

只做「条件成立 -> 提示」，不产出买卖指令。文案模板：{名称} {事件描述}（{触发价}，{时间}）。

两类评估入口：
  evaluate(quotes)          价格 / 量比 / 换手（快照即可判定）
  evaluate_intraday(series) 成交额突增 / 区间额达标 / 快速拉升 / 快速下跌（需要采样序列）
  evaluate_large_orders()   区间大单与连续同向（口径是「区间累计额」，不是单笔）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models import AlertEvent, Quote
from app.db.repo import Repo
from app.services.alert_engine import AlertEngine
from app.services.intraday import SamplePoint, change_over, rolling_amount_mean
from app.services.large_order import LargeOrder

RULE_LABELS = {
    "price_above": "价格上穿",
    "price_below": "价格下破",
    "change_pct_above": "涨幅达到",
    "change_pct_below": "跌幅达到",
    "near_limit_up": "距涨停",
    "near_limit_down": "距跌停",
    "volume_ratio_above": "量比达到",
    "turnover_rate_above": "换手率达到",
    "amount_spike": "成交额突增",
    "amount_above": "区间成交额达",
    "surge": "快速拉升",
    "plunge": "快速下跌",
    "large_order_single": "区间大单",
    "large_order_interval": "区间大单",
    "large_order_streak": "连续同向区间",
}

# 规则 -> (所需参数键, 默认等级)
RULE_SHAPE = {
    "price_above": ("price", "P3"),
    "price_below": ("price", "P3"),
    "change_pct_above": ("pct", "P3"),
    "change_pct_below": ("pct", "P3"),
    "near_limit_up": ("pct", "P2"),
    "near_limit_down": ("pct", "P2"),
    "volume_ratio_above": ("x", "P3"),
    "turnover_rate_above": ("x", "P3"),
    "amount_spike": ("k", "P3"),
    "amount_above": ("amount", "P3"),
    "surge": ("pct", "P2"),
    "plunge": ("pct", "P2"),
    "large_order_single": ("amount", "P3"),
    "large_order_streak": ("amount", "P2"),
}

INTRADAY_RULES = ("amount_spike", "amount_above", "surge", "plunge")
LARGE_ORDER_RULES = ("large_order_single", "large_order_streak")


@dataclass(slots=True)
class Fired:
    event: AlertEvent
    rule_id: int
    rule_name: str
    message: str


class AlertService:
    def __init__(self, repo: Repo, engine: AlertEngine | None = None,
                 require_trading: bool = True) -> None:
        self.repo = repo
        self.engine = engine or AlertEngine()
        self.require_trading = require_trading
        self._rules: list[dict] = []
        self._rule_symbols: dict[int, set[str]] = {}
        self.reload()

    # ---------------------------------------------------------------- 规则装载
    def reload(self) -> int:
        """规则增删改后立即调用，无需重启（并清空冷却状态）。"""
        self._rules = self.repo.rules(enabled_only=True)
        self._rule_symbols = {}
        for rule in self._rules:
            symbols = set(rule.get("symbols") or [])
            if not symbols and rule.get("group_id"):
                symbols = set(self.repo.group_symbols(int(rule["group_id"])))
            self._rule_symbols[int(rule["id"])] = symbols
        self.engine.reset_cooldown()
        return len(self._rules)

    @property
    def rules(self) -> list[dict]:
        return self._rules

    def create_price_rule(self, symbol: str, rule_type: str, value: float,
                          name: str | None = None, level: str | None = None,
                          cooldown_sec: int = 300) -> int:
        key, default_level = RULE_SHAPE[rule_type]
        params = {key: float(value)}
        if rule_type in ("near_limit_up", "near_limit_down"):
            params[key] = float(value) or 1.0
        label = RULE_LABELS.get(rule_type, rule_type)
        rule_id = self.repo.add_rule(
            name=name or f"{symbol} {label} {value:g}",
            category="price",
            rule_type=rule_type,
            level=level or default_level,
            params=params,
            symbols=[symbol],
            cooldown_sec=cooldown_sec,
        )
        self.reload()
        return rule_id

    def create_metrics_rule(self, symbol: str, rule_type: str, value: float,
                            name: str | None = None, cooldown_sec: int = 300) -> int:
        key, level = RULE_SHAPE[rule_type]
        rule_id = self.repo.add_rule(
            name=name or f"{symbol} {RULE_LABELS.get(rule_type, rule_type)} {value:g}",
            category="volume", rule_type=rule_type, level=level,
            params={key: float(value)}, symbols=[symbol], cooldown_sec=cooldown_sec,
        )
        self.reload()
        return rule_id

    def create_intraday_rule(self, symbol: str, rule_type: str, value: float,
                             name: str | None = None, cooldown_sec: int = 300,
                             minutes: float = 3.0) -> int:
        """成交额 / 量能 / 分时异动类规则。口径基于采样区间，事件里会标明 sampled。"""
        key, level = RULE_SHAPE[rule_type]
        params: dict = {key: float(value)}
        if rule_type in ("surge", "plunge"):
            params["minutes"] = float(minutes)
        label = RULE_LABELS.get(rule_type, rule_type)
        rule_id = self.repo.add_rule(
            name=name or f"{symbol} {label} {value:g}",
            category="intraday" if rule_type in ("surge", "plunge") else "volume",
            rule_type=rule_type, level=level, params=params, symbols=[symbol],
            cooldown_sec=cooldown_sec,
        )
        self.reload()
        return rule_id

    def create_large_order_rule(self, symbol: str, rule_type: str, amount: float,
                                name: str | None = None, cooldown_sec: int = 300) -> int:
        key, level = RULE_SHAPE[rule_type]
        rule_id = self.repo.add_rule(
            name=name or f"{symbol} {RULE_LABELS[rule_type]} {amount / 1e4:.0f}万",
            category="volume", rule_type=rule_type, level=level,
            params={key: float(amount)}, symbols=[symbol], cooldown_sec=cooldown_sec,
        )
        self.reload()
        return rule_id

    # ---------------------------------------------------------------- 评估
    def evaluate(self, quotes: list[Quote], now: datetime | None = None,
                 trading: bool = True, persist: bool = True) -> list[Fired]:
        now = now or datetime.now()
        if not self._rules:
            return []
        if self.require_trading and not trading:
            return []

        fired: list[Fired] = []
        for rule in self._rules:
            rule_id = int(rule["id"])
            scope = self._rule_symbols.get(rule_id) or set()
            for quote in quotes:
                if scope and quote.symbol not in scope:
                    continue
                if quote.price is None or quote.data_level == "D":
                    continue
                hit = self._match(rule, quote)
                if hit is None:
                    continue
                condition, event_type, level = hit
                if not self.engine.should_fire(quote.symbol, str(rule_id), event_type,
                                               quote.price, now):
                    continue
                event = AlertEvent(
                    symbol=quote.symbol, name=quote.name or quote.symbol,
                    event_type=event_type, trigger_price=quote.price, trigger_time=now,
                    condition=condition, level=level, volume=quote.volume, amount=quote.amount,
                    data_level=quote.data_level, source=quote.source,
                )
                if persist:
                    self.repo.insert_alert(event, rule_id=rule_id)
                fired.append(Fired(event=event, rule_id=rule_id,
                                   rule_name=rule.get("name", ""),
                                   message=format_alert(event, rule)))
        return fired

    # ---------------------------------------------------- 采样序列类评估
    def evaluate_intraday(self, series: dict[str, list[SamplePoint]],
                          now: datetime | None = None, trading: bool = True,
                          persist: bool = True,
                          names: dict[str, str] | None = None) -> list[Fired]:
        """成交额突增 / 区间额达标 / 快速拉升 / 快速下跌。

        全部基于采样区间，事件 data_level 固定为 B 且 source 带 sampled，
        避免事后被当成逐笔结论。
        """
        now = now or datetime.now()
        rules = [r for r in self._rules if r.get("rule_type") in INTRADAY_RULES]
        if not rules or (self.require_trading and not trading):
            return []

        fired: list[Fired] = []
        names = names or {}
        for rule in rules:
            rule_id = int(rule["id"])
            scope = self._rule_symbols.get(rule_id) or set()
            for symbol, points in series.items():
                if scope and symbol not in scope:
                    continue
                if len(points) < 2:
                    continue
                hit = self._match_intraday(rule, points)
                if hit is None:
                    continue
                condition, event_type, level, last = hit
                if not self.engine.should_fire(symbol, str(rule_id), event_type,
                                               last.price, now):
                    continue
                event = AlertEvent(
                    symbol=symbol, name=names.get(symbol) or symbol,
                    event_type=event_type,
                    trigger_price=last.price, trigger_time=now,
                    condition=condition, level=level,
                    volume=last.interval_volume, amount=last.interval_amount,
                    data_level="B", source="tqcenter:sampled",
                )
                if persist:
                    self.repo.insert_alert(event, rule_id=rule_id)
                fired.append(Fired(event=event, rule_id=rule_id,
                                   rule_name=rule.get("name", ""),
                                   message=format_alert(event, rule)))
        return fired

    @staticmethod
    def _match_intraday(rule: dict, points: list[SamplePoint]
                        ) -> tuple[str, str, str, SamplePoint] | None:
        rule_type = rule.get("rule_type")
        params = rule.get("params") or {}
        level = rule.get("level") or "P3"
        last = points[-1]
        if last.interval_volume <= 0 or "no_update" in (last.quality_flag or ""):
            return None

        if rule_type == "amount_spike":
            k = float(params.get("k", 3))
            mean = rolling_amount_mean(points, 20)
            if mean and k > 0 and last.interval_amount >= mean * k:
                return (f"区间额 {last.interval_amount / 1e4:.0f}万 >= 近20区间均值 "
                        f"{mean / 1e4:.0f}万 × {k:g}（采样区间 {last.interval_span_sec:.0f}s）",
                        "amount_spike", level, last)
        elif rule_type == "amount_above":
            target = float(params.get("amount", 0))
            if target and last.interval_amount >= target:
                return (f"区间额 {last.interval_amount / 1e4:.0f}万 >= {target / 1e4:.0f}万"
                        f"（区间 {last.interval_span_sec:.0f}s，方向 {last.direction}）",
                        "amount_above", level, last)
        elif rule_type in ("surge", "plunge"):
            pct_target = float(params.get("pct", 1.5))
            minutes = float(params.get("minutes", 3))
            change = change_over(points, minutes)
            if change is None:
                return None
            pct, span = change
            if rule_type == "surge" and pct >= pct_target:
                return (f"{span / 60:.1f} 分钟内涨 {pct:+.2f}% >= {pct_target:.2f}%",
                        "surge", level, last)
            if rule_type == "plunge" and pct <= -abs(pct_target):
                return (f"{span / 60:.1f} 分钟内跌 {pct:+.2f}% <= -{abs(pct_target):.2f}%",
                        "plunge", level, last)
        return None

    # ---------------------------------------------------- 区间大单评估
    def evaluate_large_orders(self, orders: list[LargeOrder], now: datetime | None = None,
                              names: dict[str, str] | None = None,
                              trading: bool = True, persist: bool = True) -> list[Fired]:
        """区间大单 / 连续同向。condition 里明确写「区间累计」，不写「单笔」。"""
        now = now or datetime.now()
        rules = [r for r in self._rules if r.get("rule_type") in LARGE_ORDER_RULES]
        if not rules or not orders or (self.require_trading and not trading):
            return []
        names = names or {}

        fired: list[Fired] = []
        for rule in rules:
            rule_id = int(rule["id"])
            scope = self._rule_symbols.get(rule_id) or set()
            params = rule.get("params") or {}
            level = rule.get("level") or "P3"
            target = float(params.get("amount", 0))
            for order in orders:
                if scope and order.symbol not in scope:
                    continue
                if rule["rule_type"] == "large_order_single":
                    if order.tier == "continuous":
                        continue        # 连续异动走连续规则，避免双报
                elif order.tier != "continuous":
                    continue
                if target and order.amount < target:
                    continue
                event_type = ("large_order_streak" if rule["rule_type"] == "large_order_streak"
                              else "large_order_interval")
                if not self.engine.should_fire(order.symbol, str(rule_id), event_type,
                                               order.price, now):
                    continue
                condition = (f"区间累计 {order.amount / 1e4:.0f}万（{order.amount / 1e4:.0f}万 >= "
                             f"{target / 1e4:.0f}万）· {order.tier_label} · "
                             f"{order.direction_label} · {order.position_label} · "
                             f"区间 {order.window_sec:.0f}s")
                event = AlertEvent(
                    symbol=order.symbol,
                    name=names.get(order.symbol) or order.symbol,
                    event_type=event_type, trigger_price=order.price, trigger_time=now,
                    condition=condition, level=level, volume=order.volume,
                    amount=order.amount, data_level=order.data_level, source=order.source,
                )
                if persist:
                    self.repo.insert_alert(event, rule_id=rule_id)
                fired.append(Fired(event=event, rule_id=rule_id,
                                   rule_name=rule.get("name", ""),
                                   message=format_alert(event, rule)))
        return fired

    @staticmethod
    def _match(rule: dict, quote: Quote) -> tuple[str, str, str] | None:
        rule_type = rule.get("rule_type")
        params = rule.get("params") or {}
        level = rule.get("level") or "P3"
        price = quote.price or 0.0

        if rule_type == "price_above":
            target = float(params.get("price", 0))
            if target and price >= target:
                return f"最新价 {price:.2f} >= {target:.2f}", rule_type, level
        elif rule_type == "price_below":
            target = float(params.get("price", 0))
            if target and price <= target:
                return f"最新价 {price:.2f} <= {target:.2f}", rule_type, level
        elif rule_type == "change_pct_above":
            pct = float(params.get("pct", 0))
            value = quote.change_pct
            if value is not None and pct and value >= pct:
                return f"涨幅 {value:+.2f}% >= {pct:.2f}%", rule_type, level
        elif rule_type == "change_pct_below":
            pct = float(params.get("pct", 0))
            value = quote.change_pct
            if value is not None and pct and value <= -abs(pct):
                return f"跌幅 {value:+.2f}% <= -{abs(pct):.2f}%", rule_type, level
        elif rule_type == "near_limit_up":
            limit = quote.limit_up_price
            band = float(params.get("pct", 1.0)) or 1.0
            if limit:
                gap = (limit - price) / limit * 100
                if 0 <= gap <= band:
                    return f"距涨停 {gap:.2f}% <= {band:.2f}%（涨停价 {limit:.2f}）", rule_type, level
        elif rule_type == "near_limit_down":
            limit = quote.limit_down_price
            band = float(params.get("pct", 1.0)) or 1.0
            if limit:
                gap = (price - limit) / limit * 100
                if 0 <= gap <= band:
                    return f"距跌停 {gap:.2f}% <= {band:.2f}%（跌停价 {limit:.2f}）", rule_type, level
        elif rule_type == "volume_ratio_above":
            target = float(params.get("x", 0))
            if quote.volume_ratio is not None and target and quote.volume_ratio >= target:
                return f"量比 {quote.volume_ratio:.2f} >= {target:.2f}", rule_type, level
        elif rule_type == "turnover_rate_above":
            target = float(params.get("x", 0))
            if quote.turnover_rate is not None and target and quote.turnover_rate >= target:
                return f"换手率 {quote.turnover_rate:.2f}% >= {target:.2f}%", rule_type, level
        return None


def format_alert(event: AlertEvent, rule: dict | None = None) -> str:
    """通知文案：只陈述事实与判定输入，不带任何买卖建议。"""
    label = RULE_LABELS.get(event.event_type, event.event_type)
    if rule and rule.get("name"):
        label = rule["name"]
    stamp = event.trigger_time.strftime("%H:%M:%S")
    return f"[{event.level}] {event.name} {label}（{event.trigger_price:.2f}，{stamp}）| {event.condition}"
