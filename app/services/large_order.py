"""区间大单识别（模块D）。只描述成交金额、方向与价格位置，禁止推断资金身份。

口径（界面、报告、日志必须照此写，禁止说成「单笔」）：
  tier       = 区间累计成交额档位，区间长度等于轮询间隔（默认 3s）
  window_sec = 该区间实际跨度，用于说明「多少秒内累计」
  界面列名写「区间额」，明细里带「推演」角标

四档：普通大单 / 大额成交 / 超大额成交 / 连续异动
  前三档按单个区间的累计成交额判定（默认 100 万 / 300 万 / 500 万）；
  连续异动由连续 N 个区间同向、每区间达阈值倍数、累计达 1000 万判定，
  因此单个区间再大也只记「超大额成交」，不会被误标成连续异动。

标签只能取六种，且必须由规则产生，不允许自由发挥：
  买入活跃 / 卖出活跃 / 买卖分歧 / 成交放大 / 量价背离 / 资金行为不明确
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from app.adapters.minute import STOCKDB_FLAG
from app.services.intraday import SamplePoint

# 阈值一律按「每分钟成交额」定义（元/分钟），判定时按区间跨度折算。
#   · 3 秒快照采样区间 → 门槛 ÷20（普通 100 万，与阶段二完全一致）
#   · 1 分钟东财分钟线 → 原值（普通 2000 万）
# 这样做是因为两种分时来源的区间长度差 20 倍，用同一个绝对数字会让 1 分钟口径
# 灵敏度放大 20 倍：实测茅台 241 个分钟点里有 207 个会同时越线，档位就没有区分度了。
REFERENCE_SPAN_SEC = 60.0

DEFAULT_TIERS = {
    "normal": 20_000_000,
    "large": 60_000_000,
    "super": 100_000_000,
    "continuous": 200_000_000,
}


def scale_tiers(tiers: dict[str, float], span_sec: float | None) -> dict[str, float]:
    """把「每分钟」阈值折算到本次区间的实际跨度上。跨度未知按 1 分钟算。"""
    span = float(span_sec or 0.0)
    if span <= 0:
        span = REFERENCE_SPAN_SEC
    factor = max(1.0, span) / REFERENCE_SPAN_SEC
    return {key: float(value) * factor for key, value in tiers.items()}

TIER_LABEL = {
    "normal": "普通大单",
    "large": "大额成交",
    "super": "超大额成交",
    "continuous": "连续异动",
}
TIER_LEVEL = {"normal": "P4", "large": "P3", "super": "P2", "continuous": "P1"}
# 右栏表格窄，用两字简称；完整档位在气泡里
TIER_SHORT = {"normal": "普通", "large": "大额", "super": "超大", "continuous": "连续"}

ALLOWED_TAGS = ("买入活跃", "卖出活跃", "买卖分歧", "成交放大", "量价背离", "资金行为不明确")

DIRECTION_LABEL = {"buy": "偏买", "sell": "偏卖", "unknown": "不明"}
POSITION_LABEL = {
    "relative_high": "相对高位",
    "mid": "中位",
    "relative_low": "相对低位",
    "unknown": "位置未知",
}

MAX_HISTORY = 60
MAX_ORDERS = 500


@dataclass(slots=True)
class LargeOrder:
    """区间大单事件。amount 是区间累计额，不是单笔成交额。"""
    symbol: str
    time: datetime
    tier: str
    direction: str
    price: float
    volume: int
    amount: float
    threshold: float
    price_position: str = "unknown"
    tag: str = "资金行为不明确"
    window_sec: float = 0.0
    data_level: str = "B"
    source: str = "tqcenter:sampled"

    @property
    def tier_label(self) -> str:
        return TIER_LABEL.get(self.tier, self.tier)

    @property
    def tier_short(self) -> str:
        return TIER_SHORT.get(self.tier, self.tier)

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABEL.get(self.direction, "不明")

    @property
    def position_label(self) -> str:
        return POSITION_LABEL.get(self.price_position, "位置未知")


@dataclass(slots=True)
class _Interval:
    time: datetime
    direction: str
    amount: float
    price: float
    volume: int


def classify_tier(amount: float, tiers: dict[str, float] | None = None) -> str | None:
    """按单个区间累计额判档（阈值单位：元/分钟，调用方需先按跨度折算）。

    连续异动不在此判定，由 LargeOrderDetector 计数得出。
    """
    thresholds = dict(DEFAULT_TIERS)
    if tiers:
        thresholds.update({k: v for k, v in tiers.items() if k in thresholds and v})
    if amount >= thresholds["super"]:
        return "super"
    if amount >= thresholds["large"]:
        return "large"
    if amount >= thresholds["normal"]:
        return "normal"
    return None


def price_position(price: float, recent_low: float, recent_high: float) -> str:
    """近 N 日区间分位：>=70% 高位，<=30% 低位。"""
    if not recent_high or not recent_low or recent_high <= recent_low:
        return "unknown"
    pos = (price - recent_low) / (recent_high - recent_low)
    if pos >= 0.7:
        return "relative_high"
    if pos <= 0.3:
        return "relative_low"
    return "mid"


def tag_direction(buy_amount: float, sell_amount: float) -> str:
    """方向标签。只做金额对比，不做身份判断。"""
    total = buy_amount + sell_amount
    if total <= 0:
        return "资金行为不明确"
    ratio = buy_amount / total
    if ratio >= 0.7:
        return "买入活跃"
    if ratio <= 0.3:
        return "卖出活跃"
    return "买卖分歧"


class LargeOrderDetector:
    """把采样点流变成区间大单事件。只做规则判定，不做任何身份推断。"""

    def __init__(self, tiers: dict | None = None, streak_count: int = 3,
                 streak_multiplier: float = 2.0, lookback_days: int = 20) -> None:
        merged = dict(DEFAULT_TIERS)
        for key, value in (tiers or {}).items():
            if key in merged and value:
                merged[key] = float(value)
        self.tiers = merged
        self.streak_count = max(2, int(streak_count))
        self.streak_multiplier = max(1.0, float(streak_multiplier))
        self.lookback_days = max(5, int(lookback_days))

        self._history: dict[str, deque[_Interval]] = {}
        self._orders: dict[str, deque[LargeOrder]] = {}
        self._ranges: dict[str, tuple[float, float]] = {}
        self._streak_dir: dict[str, str] = {}
        self._streak_done: dict[str, bool] = {}

    # ---------------------------------------------------------------- 配置
    def configure(self, tiers: dict | None = None, streak_count: int | None = None,
                  streak_multiplier: float | None = None) -> None:
        """用户改阈值后调用；只影响之后产生的事件，不清空已有记录。"""
        if tiers:
            for key, value in tiers.items():
                if key in self.tiers and value:
                    self.tiers[key] = float(value)
        if streak_count is not None:
            self.streak_count = max(2, int(streak_count))
        if streak_multiplier is not None:
            self.streak_multiplier = max(1.0, float(streak_multiplier))

    # ---------------------------------------------------------------- 价格位置
    def set_daily_range(self, symbol: str, high: float | None, low: float | None) -> bool:
        """写入近 N 日区间（由日K算）。拿不到就不写，位置保持「位置未知」。"""
        if not high or not low or high <= low:
            self._ranges.pop(symbol, None)
            return False
        self._ranges[symbol] = (float(high), float(low))
        return True

    def set_daily_range_from_bars(self, symbol: str, bars: list, days: int | None = None) -> bool:
        window = bars[-int(days or self.lookback_days):] if bars else []
        if not window:
            return False
        return self.set_daily_range(symbol, max(b.high for b in window),
                                    min(b.low for b in window))

    def price_position(self, symbol: str, price: float) -> str:
        span = self._ranges.get(symbol)
        if not span or not price:
            return "unknown"
        high, low = span
        return price_position(price, low, high)

    # ---------------------------------------------------------------- 判定
    def feed(self, point: SamplePoint, data_level: str = "B") -> list[LargeOrder]:
        """吃一个采样点，返回本次新产生的大单事件（可能为空）。"""
        if point.interval_volume <= 0 or "no_update" in (point.quality_flag or ""):
            return []   # 无新增成交：不产生事件，也不参与连续计数

        symbol = point.symbol
        history = self._history.setdefault(symbol, deque(maxlen=MAX_HISTORY))
        history.append(_Interval(time=point.time, direction=point.direction,
                                 amount=float(point.interval_amount or 0.0),
                                 price=float(point.price or 0.0),
                                 volume=int(point.interval_volume)))

        amount = float(point.interval_amount or 0.0)
        position = self.price_position(symbol, point.price)
        orders: list[LargeOrder] = []
        # 来源要如实记录：分钟线与快照采样是两条链路，落库后必须能分清；
        # 分钟线内部也要区分东财与 free-stockdb，否则复盘时分不清数据可信度
        flag = point.quality_flag or ""
        if STOCKDB_FLAG in flag:
            source = "stockdb:minute"
        elif "em_minute" in flag:
            source = "eastmoney:trends"
        else:
            source = "tqcenter:sampled"
        # 本次区间对应的实际门槛：60 秒区间用原值，3 秒采样区间自动缩到 1/20
        tiers = scale_tiers(self.tiers, point.interval_span_sec)

        streak = self._check_streak(symbol, history, tiers)
        if streak is not None:
            orders.append(LargeOrder(
                symbol=symbol, time=point.time, tier="continuous",
                direction=streak["direction"], price=point.price,
                volume=streak["volume"], amount=streak["amount"],
                threshold=tiers["continuous"], price_position=position,
                tag="买入活跃" if streak["direction"] == "buy" else "卖出活跃",
                window_sec=streak["window"], data_level=data_level, source=source,
            ))
        else:
            tier = classify_tier(amount, tiers)
            if tier:
                orders.append(LargeOrder(
                    symbol=symbol, time=point.time, tier=tier,
                    direction=point.direction, price=point.price,
                    volume=int(point.interval_volume), amount=amount,
                    threshold=tiers[tier], price_position=position,
                    tag=self._tag(history, tier, point, tiers),
                    window_sec=float(point.interval_span_sec or 0.0),
                    data_level=data_level, source=source,
                ))

        if orders:
            bucket = self._orders.setdefault(symbol, deque(maxlen=MAX_ORDERS))
            bucket.extend(orders)
        return orders

    def _check_streak(self, symbol: str, history: deque[_Interval],
                      tiers: dict[str, float] | None = None) -> dict | None:
        """连续 N 个区间同向、每区间达阈值倍数、累计达连续异动门槛。

        同一段连续行情只报一次；方向切换或条件中断后重新计数。
        """
        if len(history) < self.streak_count:
            return None
        tail = list(history)[-self.streak_count:]
        directions = {item.direction for item in tail}
        current_dir = tail[-1].direction
        if current_dir != self._streak_dir.get(symbol):
            self._streak_dir[symbol] = current_dir
            self._streak_done[symbol] = False
        if len(directions) != 1 or "unknown" in directions:
            self._streak_done[symbol] = False
            return None
        limits = tiers or self.tiers
        per_interval = limits["normal"] * self.streak_multiplier
        if any(item.amount < per_interval for item in tail):
            self._streak_done[symbol] = False
            return None
        total = sum(item.amount for item in tail)
        if total < limits["continuous"]:
            self._streak_done[symbol] = False
            return None
        if self._streak_done.get(symbol):
            return None
        self._streak_done[symbol] = True
        return {
            "direction": current_dir,
            "amount": total,
            "volume": sum(item.volume for item in tail),
            "window": (tail[-1].time - tail[0].time).total_seconds(),
        }

    def _tag(self, history: deque[_Interval], tier: str, point: SamplePoint,
             tiers: dict[str, float] | None = None) -> str:
        """六选一，全部由规则产生；卖出也绝不写成「出货」。"""
        direction = point.direction
        items = list(history)
        limits = tiers or self.tiers
        # 买卖分歧：连续两个区间方向相反且都达到普通大单档
        if len(items) >= 2:
            prev, last = items[-2], items[-1]
            if (prev.direction in ("buy", "sell") and last.direction in ("buy", "sell")
                    and prev.direction != last.direction
                    and prev.amount >= limits["normal"]
                    and last.amount >= limits["normal"]):
                return "买卖分歧"
        # 量价背离：大额主动买但价格走低（或反之）
        if len(items) >= 2 and direction in ("buy", "sell"):
            prev_price = items[-2].price
            if prev_price:
                if direction == "buy" and point.price < prev_price:
                    return "量价背离"
                if direction == "sell" and point.price > prev_price:
                    return "量价背离"
        if direction == "buy":
            return "买入活跃"
        if direction == "sell":
            return "卖出活跃"
        return "成交放大" if tier in ("large", "super") else "资金行为不明确"

    # ---------------------------------------------------------------- 读取
    def orders(self, symbol: str, limit: int = 200) -> list[LargeOrder]:
        bucket = self._orders.get(symbol)
        if not bucket:
            return []
        return list(bucket)[-limit:]

    def history(self, symbol: str) -> list[_Interval]:
        return list(self._history.get(symbol, ()))

    def streak_state(self, symbol: str) -> tuple[str, int]:
        """返回 (当前连续方向, 已连续区间数)，供界面显示「连续 N 档」。"""
        items = list(self._history.get(symbol, ()))
        if not items:
            return "unknown", 0
        direction = items[-1].direction
        count = 0
        for item in reversed(items):
            if item.direction != direction or direction == "unknown":
                break
            count += 1
        return direction, count

    def reset(self, symbol: str | None = None) -> None:
        keys = [symbol] if symbol else list(self._history)
        for key in keys:
            self._history.pop(key, None)
            self._orders.pop(key, None)
            self._streak_dir.pop(key, None)
            self._streak_done.pop(key, None)
