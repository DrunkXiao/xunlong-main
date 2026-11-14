"""强势回调低吸（A 股日线收盘口径）—— 指标、形态、评分、全市场扫描。

规则来自 `ashare-strong-pullback` 策略 3.0，这里把它变成「可复现的计算 + 可解释的输出」：

1. **八项硬性条件**任一不满足即剔除，评分只用于排序，**不能补偿**硬性条件；
2. 所有结论都基于**已收盘日线**：盘中未收盘的 K 线不得确认阳包阴/锤子线，
   扫描器在交易时段内会自动丢掉当日未完成的 K 线；
3. 只描述价格与成交量形态，不推断资金身份，也不给买卖指令之外的承诺。

硬性条件（缺一不可）：

| key | 条件 |
|---|---|
| limitup_10d      | 近 10 个交易日内至少一次有效涨停（按前收算精确涨停价） |
| stage_high_10d   | 近 10 日内创过 20 日阶段新高 |
| trend_ok         | MA5 > MA10 > MA20，且 MA20 相比 5 日前上行 |
| support_ok       | 收盘价在 MA10 或前期颈线的 ±1.5% 以内 |
| volume_ok        | 回调 5 日均量 / 之前 10 日均量 ≤ 0.50（缩量） |
| candle_ok        | 当日出现阳包阴或锤子线（止跌确认） |
| no_distribution  | 不是「放量长阴」（收盘跌且量 > 近 10 日中位量 1.8 倍） |
| no_trend_break   | 收盘价未跌破 MA20 |

评分（100 分）：板块强度 20 + 趋势 20 + 人气/新高 15 + 支撑 20 + 缩量 15 + 止跌 10。
90 以上重点关注，80-89 优质候选，70-79 观察，70 以下不推荐。
`sector_score_mode` 为 `off` 时板块强度恒为 0，满分实际只有 80。
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from app.adapters.tdx_daily import (
    TdxDailyError, TdxDailySource, is_st_name, limit_up_cents, limit_up_pct, trim_unclosed,
)

# 八项硬性条件；顺序即界面展示顺序
CHECKS: tuple[tuple[str, str], ...] = (
    ("limitup_10d", "近10日有效涨停"),
    ("stage_high_10d", "近10日创20日新高"),
    ("trend_ok", "MA5>MA10>MA20 且 MA20 上行"),
    ("support_ok", "回调至 MA10/颈线 ±1.5%"),
    ("volume_ok", "回调缩量（≤阈值）"),
    ("candle_ok", "阳包阴 / 锤子线"),
    ("no_distribution", "无高位放量长阴"),
    ("no_trend_break", "收盘未破 MA20"),
)

SCORE_PARTS: tuple[tuple[str, str, int], ...] = (
    ("sector", "板块强度", 20),
    ("trend", "趋势强度", 20),
    ("momentum", "人气/新高", 15),
    ("support", "支撑有效", 20),
    ("volume", "缩量质量", 15),
    ("candle", "止跌形态", 10),
)

SIGNAL_LABEL = {
    "BUY-A": "确认（全部硬性条件通过）",
    "WATCH": "观察（回调到位，等缩量止跌）",
    "BLOCK": "剔除（硬性条件未通过）",
}


# ---------------------------------------------------------------- 参数

@dataclass(slots=True)
class PullbackParams:
    recent_limitup_days: int = 10
    stage_high_lookback: int = 20
    support_tolerance_pct: float = 1.5
    pullback_volume_ratio_max: float = 0.50
    pullback_window: int = 5
    rally_window: int = 10
    stop_buffer_pct: float = 2.0
    reward_risk_ratio: float = 2.5
    limitup_close_only: bool = True
    sector_score_mode: str = "off"
    min_bars: int = 60
    stale_days_max: int = 12
    stale_stock_days: int = 7
    account_equity: float = 1_000_000.0
    risk_pct: float = 1.0
    position_pct: float = 10.0
    max_positions: int = 3
    lot_size: int = 100
    max_candidates: int = 120          # 界面候选表最多留这么多只（全市场评估数另计）

    @classmethod
    def from_config(cls, config: dict) -> "PullbackParams":
        pb = (config or {}).get("pullback") or {}
        st = pb.get("strategy") or {}
        kwargs = {}
        for item in cls.__dataclass_fields__:      # 只吃自己认识的键，配置多余字段不报错
            if item in st:
                kwargs[item] = st[item]
        for item in ("min_bars", "stale_days_max", "stale_stock_days", "account_equity",
                     "risk_pct", "position_pct", "max_positions", "lot_size",
                     "max_candidates"):
            if item in pb:
                kwargs[item] = pb[item]
        params = cls(**kwargs)
        params.sector_score_mode = str(params.sector_score_mode or "off").lower()
        return params

    def summary(self) -> dict:
        """落盘用：只记关键口径，便于回溯「这份结果是什么参数算出来的」。"""
        return {
            "涨停窗口": f"{self.recent_limitup_days} 日",
            "新高窗口": f"{self.stage_high_lookback} 日",
            "支撑容差": f"±{self.support_tolerance_pct}%",
            "缩量上限": f"{self.pullback_volume_ratio_max:.2f}",
            "缩量窗口": f"{self.pullback_window}/{self.rally_window}",
            "止损缓冲": f"{self.stop_buffer_pct}%",
            "盈亏比": f"{self.reward_risk_ratio}R",
            "板块强度": self.sector_score_mode,
            "账户权益": f"{self.account_equity:.0f}",
        }


# ---------------------------------------------------------------- 结果对象

@dataclass(slots=True)
class Pick:
    """单只候选的全部证据。字段名即持久化键，改动要兼容旧 JSON。"""
    symbol: str
    code: str
    name: str
    industry: str
    date: str                      # 信号所属交易日 YYYY-MM-DD
    close: float
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    neckline: float | None = None
    support: float | None = None
    buy_low: float | None = None
    buy_high: float | None = None
    pullback_low: float | None = None
    stop: float | None = None
    target: float | None = None
    volume_ratio: float | None = None
    amount: float | None = None
    pattern: str = ""
    limitup: bool = False
    limitup_date: str = ""
    limitup_pct: float = 10.0
    stage_high: bool = False
    checks: dict = field(default_factory=dict)
    score: int = 0
    score_parts: dict = field(default_factory=dict)
    sector_score: float = 0.0
    signal: str = "BLOCK"
    shares: int = 0
    risk_amount: float = 0.0
    position_value: float = 0.0
    risk_pct: float = 0.0           # 风险额占账户权益百分比
    value_pct: float = 0.0          # 市值占账户权益百分比
    entry_ref: float = 0.0          # 计算股数/目标用的价格（买入区间上沿）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Pick":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass(slots=True)
class ScanResult:
    data_date: str = ""            # 本地日线最新交易日 YYYY-MM-DD
    stale_days: int | None = None
    allow_buy: bool = True         # 数据过期时降级：确认信号一律降为观察
    universe: int = 0
    scanned: int = 0               # 成功读到日线并评估的只数
    evaluated: int = 0             # 出过结论的只数（含被截掉的低分股）
    errors: list = field(default_factory=list)
    picks: list = field(default_factory=list)       # list[Pick]，已按候选优先级截断
    frames: dict = field(default_factory=dict)      # symbol -> DataFrame，只在内存里给图表用
    market: dict = field(default_factory=dict)
    reject: dict = field(default_factory=dict)      # 未通过原因统计
    elapsed: float = 0.0
    source_label: str = ""
    sector_mode: str = "off"
    params: dict = field(default_factory=dict)
    scanned_at: str = ""
    notes: list = field(default_factory=list)

    @property
    def confirmed(self) -> list:
        return [p for p in self.picks if p.signal == "BUY-A"]

    @property
    def watch(self) -> list:
        return [p for p in self.picks if p.signal == "WATCH"]

    def to_payload(self) -> dict:
        return {
            "data_date": self.data_date,
            "stale_days": self.stale_days,
            "allow_buy": self.allow_buy,
            "universe": self.universe,
            "scanned": self.scanned,
            "evaluated": self.evaluated,
            "errors": self.errors[:20],
            "picks": [p.to_dict() for p in self.picks],
            "market": self.market,
            "reject": self.reject,
            "elapsed": round(self.elapsed, 1),
            "source_label": self.source_label,
            "sector_mode": self.sector_mode,
            "params": self.params,
            "scanned_at": self.scanned_at,
            "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "ScanResult":
        payload = dict(data or {})
        payload["picks"] = [Pick.from_dict(p) for p in payload.get("picks") or []]
        payload["frames"] = {}          # K线不进 JSON（体积大且可随时重取）
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


# ---------------------------------------------------------------- 指标与形态

def add_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    """均线、量能、阶段新高、MA20 斜率。入参必须按日期升序。"""
    x = bars.reset_index(drop=True).copy()
    for window in (5, 10, 20):
        x[f"ma{window}"] = x["close"].rolling(window).mean()
    x["vol5"] = x["volume"].rolling(5).mean()
    x["high20_prev"] = x["high"].shift(1).rolling(20).max()
    x["high20"] = x["high"].rolling(20).max()
    x["ma20_slope"] = x["ma20"] / x["ma20"].shift(5) - 1
    x["ret1"] = x["close"].pct_change()
    return x


def is_hammer(row) -> bool:
    """锤子线：下影线 ≥ 实体 2 倍，上影线很短，收盘在当日振幅上 55% 以上。"""
    body = max(abs(row.close - row.open), row.close * 0.001)
    lower = min(row.open, row.close) - row.low
    upper = row.high - max(row.open, row.close)
    return bool(lower >= 2 * body and upper <= 1.2 * body
                and row.close >= row.low + (row.high - row.low) * 0.55)


def is_bullish_engulfing(prev, cur) -> bool:
    """阳包阴：前一根收阴，当根收阳且实体完全包住前一根实体。"""
    return bool(prev.close < prev.open and cur.close > cur.open
                and cur.open <= prev.close and cur.close >= prev.open)


def recent_limit_up(bars: pd.DataFrame, days: int, pct: float,
                    close_only: bool = True) -> tuple[bool, str]:
    """近 N 个交易日是否有涨停：涨停价按前收盘精确计算（分），不用涨跌幅近似。"""
    if len(bars) < 2:
        return False, ""
    if "close_cents" in bars:
        prev_cents = bars["close_cents"].shift(1)
    else:
        prev_cents = (bars["close"].shift(1) * 100).round()
    if close_only and "close_cents" in bars:
        price_cents = bars["close_cents"]
    elif "high_cents" in bars:
        price_cents = bars["high_cents"]
    else:
        price_cents = (bars["close" if close_only else "high"] * 100).round()

    limits = np.array([
        limit_up_cents(int(v), pct) if pd.notna(v) else 0 for v in prev_cents.fillna(0)
    ], dtype="int64")
    hit = (price_cents.fillna(0).astype("int64").to_numpy() >= limits) & prev_cents.notna().to_numpy()
    window = hit[-days:]
    if not bool(window.any()):
        return False, ""
    idx = len(hit) - days + int(np.flatnonzero(window)[-1])
    return True, str(bars.loc[idx, "date"].date())


def pullback_volume_ratio(bars: pd.DataFrame, pullback_window: int = 5,
                         rally_window: int = 10) -> float:
    """回调 5 日均量 / 之前 10 日均量。>1 表示还在放量，不是缩量回调。"""
    if len(bars) < pullback_window + rally_window + 5:
        return float("nan")
    pb = float(bars.tail(pullback_window)["volume"].mean())
    rally = float(bars.iloc[-(pullback_window + rally_window):-pullback_window]["volume"].mean())
    if not math.isfinite(rally) or rally <= 0:
        return float("nan")
    return pb / rally


# ---------------------------------------------------------------- 评估

def evaluate(symbol: str, name: str, industry: str, bars: pd.DataFrame,
             params: PullbackParams, sector_score: float = 0.0) -> Pick | None:
    """对单只股票出结论。数据不足返回 None（由扫描器计入「跳过」）。"""
    if bars is None or len(bars) < max(params.min_bars, params.stage_high_lookback + 5):
        return None
    x = add_indicators(bars)
    row = x.iloc[-1]
    prev = x.iloc[-2]
    code = symbol.split(".")[0]
    pct = limit_up_pct(code, is_st_name(name))

    # ---- 八项硬性条件
    recent = x.tail(params.recent_limitup_days)
    stage_high = bool((recent["high"] >= recent["high20"] - 1e-8).any())
    limitup, limitup_date = recent_limit_up(
        x, params.recent_limitup_days, pct, close_only=params.limitup_close_only)

    trend = bool(pd.notna(row.ma20) and row.ma5 > row.ma10 > row.ma20
                 and row.close >= row.ma20 and row.ma20_slope > 0)

    ma10_near = bool(pd.notna(row.ma10)
                     and abs(row.close / row.ma10 - 1) * 100 <= params.support_tolerance_pct)
    neckline = float(x["high"].iloc[-21:-1].max()) if len(x) >= 25 else float("nan")
    neck_near = bool(pd.notna(neckline)
                     and abs(row.close / neckline - 1) * 100 <= params.support_tolerance_pct)
    support_ok = ma10_near or neck_near

    vol_ratio = pullback_volume_ratio(x, params.pullback_window, params.rally_window)
    volume_ok = bool(pd.notna(vol_ratio) and vol_ratio <= params.pullback_volume_ratio_max)
    candle = bool(is_bullish_engulfing(prev, row) or is_hammer(row))
    median_vol = float(x["volume"].tail(10).median())
    no_distribution = not (row.close < row.open and row.volume > median_vol * 1.8)
    no_trend_break = bool(pd.notna(row.ma20) and row.close >= row.ma20)

    checks = {
        "limitup_10d": limitup,
        "stage_high_10d": stage_high,
        "trend_ok": trend,
        "support_ok": support_ok,
        "volume_ok": volume_ok,
        "candle_ok": candle,
        "no_distribution": no_distribution,
        "no_trend_break": no_trend_break,
    }
    hard = all(checks.values())

    # ---- 评分（硬性条件不参与加减分，只决定是否入选）
    sector_pts = max(0, min(20, int(round(sector_score))))
    trend_pts = 20 if trend else (10 if row.close > row.ma20 else 0)
    momentum_pts = 15 if (limitup and stage_high) else (7 if (limitup or stage_high) else 0)
    support_pts = 20 if (ma10_near and neck_near) else (15 if support_ok else 0)
    ratio = float(vol_ratio) if pd.notna(vol_ratio) else float("nan")
    volume_pts = 15 if math.isfinite(ratio) and ratio <= 0.5 else (
        8 if math.isfinite(ratio) and ratio <= 0.7 else 0)
    candle_pts = 10 if candle else 0
    score_parts = {
        "sector": sector_pts, "trend": trend_pts, "momentum": momentum_pts,
        "support": support_pts, "volume": volume_pts, "candle": candle_pts,
    }
    score = sum(score_parts.values())

    if hard and score >= 80:
        signal = "BUY-A"
    elif limitup and stage_high and trend and support_ok:
        signal = "WATCH"          # 回调到位但没缩量/没止跌：只能观察
    else:
        signal = "BLOCK"

    # ---- 支撑、买入区间、止损、目标
    support = float(row.ma10) if pd.notna(row.ma10) else float(row.close)
    if neck_near and pd.notna(neckline) and not ma10_near:
        support = float(neckline)
    tol = float(params.support_tolerance_pct) / 100.0
    buy_low = round(support * (1 - tol), 2)
    buy_high = round(support * (1 + tol), 2)

    pullback_low = float(x.tail(params.pullback_window)["low"].min())
    # 止损取「回调低点下方 2%」与「MA20×0.995」中更低的那个（与原策略实现一致）：
    # 两条线都当作下沿，触发哪条算哪条。代价是每股风险可能到 10% 以上，
    # 界面对「止损距买入价 > 8%」的标的会标黄提示，不会静默按 15% 风险下单。
    stop = min(pullback_low * (1 - params.stop_buffer_pct / 100),
               float(row.ma20) * 0.995 if pd.notna(row.ma20) else pullback_low)
    entry_ref = float(row.close)          # 与策略报告口径一致：以收盘价做参考买入价
    risk_per_share = max(entry_ref - stop, 0.0)
    target = entry_ref + params.reward_risk_ratio * risk_per_share if risk_per_share > 0 else float("nan")

    if is_bullish_engulfing(prev, row):
        pattern = "阳包阴"
    elif is_hammer(row):
        pattern = "锤子线"
    else:
        pattern = ""

    pick = Pick(
        symbol=symbol, code=code, name=name, industry=industry,
        date=str(row.date.date()),
        close=round(float(row.close), 3),
        ma5=_round(row.ma5), ma10=_round(row.ma10), ma20=_round(row.ma20),
        neckline=None if pd.isna(neckline) else round(neckline, 3),
        support=round(support, 3), buy_low=buy_low, buy_high=buy_high,
        pullback_low=round(pullback_low, 3), stop=round(stop, 3),
        target=None if pd.isna(target) else round(target, 3),
        volume_ratio=None if pd.isna(vol_ratio) else round(float(vol_ratio), 4),
        amount=round(float(row.amount), 0) if pd.notna(row.get("amount")) else None,
        pattern=pattern,
        limitup=limitup, limitup_date=limitup_date, limitup_pct=pct,
        stage_high=stage_high, checks=checks,
        score=int(score), score_parts=score_parts, sector_score=float(sector_pts),
        signal=signal, entry_ref=entry_ref,
    )
    plan_position(pick, params)
    return pick


def _round(value, digits: int = 3) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), digits)


def plan_position(pick: Pick, params: PullbackParams) -> Pick:
    """按 1% 单笔风险 / 10% 单票上限算股数。

    买入价用**收盘价**（与原策略报告口径一致），止损用 `pick.stop`；
    若实际在参考买入区间上沿成交，每股风险会略大于此处的估算，界面上有注明。
    """
    entry = pick.entry_ref or pick.close
    stop = pick.stop or 0.0
    risk_per_share = max(entry - stop, 0.0)
    equity = float(params.account_equity)
    lot = max(int(params.lot_size), 1)
    if risk_per_share <= 0 or equity <= 0 or entry <= 0:
        pick.shares = 0
        pick.risk_amount = 0.0
        pick.position_value = 0.0
        pick.risk_pct = 0.0
        pick.value_pct = 0.0
        return pick
    risk_budget = equity * params.risk_pct / 100.0
    risk_qty = math.floor((risk_budget / risk_per_share) / lot) * lot
    cap_qty = math.floor((equity * params.position_pct / 100.0 / entry) / lot) * lot
    shares = max(0, min(risk_qty, cap_qty))
    pick.shares = int(shares)
    pick.risk_amount = round(shares * risk_per_share, 2)
    pick.position_value = round(shares * entry, 2)
    pick.risk_pct = round(pick.risk_amount / equity * 100, 3)
    pick.value_pct = round(pick.position_value / equity * 100, 3)
    return pick


# ---------------------------------------------------------------- 板块强度

def build_sector_scores(records: list[dict]) -> dict[str, float]:
    """按行业内个股近 20 日涨幅均值排名给 0-20 分（需 sector_score_mode: breadth）。

    这是「同行业个股涨幅的相对排名」，不是资金流向，也不代表板块真实强弱。
    """
    perf: dict[str, list[float]] = {}
    for rec in records:
        bars = rec.get("bars")
        if bars is None or len(bars) < 21:
            continue
        try:
            ret20 = float(bars["close"].iloc[-1] / bars["close"].iloc[-21] - 1)
        except (IndexError, ZeroDivisionError, TypeError):
            continue
        perf.setdefault(rec.get("industry") or "未分类", []).append(ret20)
    if not perf:
        return {}
    avg = {key: float(np.mean(vals)) for key, vals in perf.items() if vals}
    order = sorted(avg.items(), key=lambda kv: kv[1])
    count = len(order)
    return {key: float(round(20 * (i / max(count - 1, 1)))) for i, (key, _) in enumerate(order)}


# ---------------------------------------------------------------- 市场环境

def market_summary(records: list[dict]) -> dict:
    """全样本市场环境：涨跌家数、平均涨跌、当日涨停数、MA20 上方占比。"""
    up = down = flat = above = limit_up = 0
    pcts: list[float] = []
    for rec in records:
        bars = rec.get("bars")
        if bars is None or len(bars) < 2:
            continue
        change = bars["pct_change"].iloc[-1] if "pct_change" in bars else float("nan")
        change = float(change) if pd.notna(change) else 0.0
        pcts.append(change)
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        ma20 = bars["close"].rolling(20).mean().iloc[-1]
        if pd.notna(ma20) and bars["close"].iloc[-1] >= ma20:
            above += 1
        hit, _ = recent_limit_up(
            bars, 1, limit_up_pct(rec["code"], is_st_name(rec.get("name", ""))), True)
        if hit:
            limit_up += 1
    total = len(pcts)
    return {
        "total": total, "up": up, "down": down, "flat": flat,
        "avg_pct": round(float(np.mean(pcts)), 3) if pcts else 0.0,
        "limit_up": limit_up,
        "above_ma20": above,
        "above_ratio": round(above / total, 4) if total else 0.0,
    }


# ---------------------------------------------------------------- 扫描

def stale_days(last_yyyymmdd) -> int | None:
    if not last_yyyymmdd:
        return None
    try:
        day = datetime.strptime(str(last_yyyymmdd), "%Y%m%d").date()
    except ValueError:
        return None
    return (datetime.now().date() - day).days


def scan(source: TdxDailySource, params: PullbackParams, *,
         limit: int = 0, on_progress=None, should_stop=None) -> ScanResult:
    """全市场扫描。`on_progress(done, total, phase)` 用于界面进度，`should_stop()` 用于中断。

    单只失败只记进 errors，不中断整轮；数据过期不生成确认信号（只用观察）。
    """
    started = time.time()
    result = ScanResult(
        source_label=f"通达信本地日线 {source.root}",
        sector_mode=params.sector_score_mode,
        params=params.summary(),
        scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    last = source.last_trade_date()
    if last is None:
        raise TdxDailyError("没有读到任何日线：请先在通达信里下载日线数据（vipdoc/*/lday）。")
    result.data_date = f"{str(last)[:4]}-{str(last)[4:6]}-{str(last)[6:]}"
    result.stale_days = stale_days(last)
    if result.stale_days is not None and result.stale_days > params.stale_days_max:
        result.allow_buy = False
        result.notes.append(
            f"数据已 {result.stale_days} 个自然日未更新（阈值 {params.stale_days_max}），"
            "按规则不生成确认信号，全部降为观察")

    universe = source.universe(limit=limit)
    result.universe = len(universe)
    latest = pd.Timestamp(str(last))

    rows: list[dict] = []
    records: list[dict] = []
    errors: list[dict] = []
    reject = {key: 0 for key, _ in CHECKS}

    for index, row in enumerate(universe):
        if should_stop is not None and should_stop():
            result.notes.append(f"扫描在第 {index} 只处被手动停止，结果不完整")
            break
        try:
            bars = trim_unclosed(source.bars(row))
            if bars is None or len(bars) < 2:
                errors.append({"symbol": row["symbol"], "error": "可用日线不足 2 根"})
                continue
            gap = (latest - bars["date"].iloc[-1]).days
            if gap > params.stale_stock_days:
                errors.append({"symbol": row["symbol"], "error": f"停牌或未更新（落后 {gap} 天）"})
                continue
            record = {**row, "bars": bars}
            records.append(record)
            pick = evaluate(row["symbol"], row["name"], row["industry"], bars, params)
            if pick is not None:
                rows.append(pick)
                for key, _ in CHECKS:
                    if not pick.checks.get(key):
                        reject[key] += 1
        except Exception as exc:            # 单只失败不影响整体
            errors.append({"symbol": row["symbol"], "error": f"{type(exc).__name__}: {exc}"})
        if on_progress is not None and index % 100 == 0:
            on_progress(index, len(universe), "扫描日线")

    # 板块强度（可选）：先按 0 分算出候选，再按行业排名补分并重判信号
    if params.sector_score_mode == "breadth" and rows:
        if on_progress is not None:
            on_progress(len(universe), len(universe), "计算板块强度")
        scores = build_sector_scores(records)
        for pick in rows:
            pick.sector_score = float(scores.get(pick.industry, 0))
            pick.score_parts["sector"] = int(pick.sector_score)
            pick.score = int(sum(pick.score_parts.values()))
            if pick.signal == "BUY-A" and pick.score < 80:
                pick.signal = "WATCH"

    if not result.allow_buy:
        for pick in rows:
            if pick.signal == "BUY-A":
                pick.signal = "WATCH"

    # 排序口径：确认 > 观察 > 剔除，同组内先看硬性条件通过数，再看分数
    def _rank(pick: Pick):
        priority = {"BUY-A": 2, "WATCH": 1}.get(pick.signal, 0)
        return (priority, sum(1 for v in pick.checks.values() if v), pick.score)

    rows.sort(key=_rank, reverse=True)
    result.evaluated = len(rows)
    cap = max(int(params.max_candidates), 10)
    result.picks = rows[:cap]
    keep = {p.symbol for p in result.picks}
    result.frames = {rec["symbol"]: rec["bars"] for rec in records if rec["symbol"] in keep}
    result.scanned = len(records)
    result.errors = errors[:50]
    result.market = market_summary(records)
    result.reject = reject
    result.elapsed = time.time() - started
    if on_progress is not None:
        on_progress(len(universe), len(universe), "完成")
    return result


def warmup_bars(params: PullbackParams) -> int:
    """算满指标所需的最少 K 线（测试与提示用）。"""
    return max(params.min_bars, params.stage_high_lookback + 5,
               params.pullback_window + params.rally_window + 6)
