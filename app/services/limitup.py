"""涨停四阶段选股规则引擎（移植自 a-share-limitup-hunter 3.x，纯计算，不含任何 IO）。

四阶段（每个阶段只对"已经发生"的事实计分，未发生的阶段不给分）：

    首板发现 S1 → 三倍量确认 S2 → 次日承接 S3 → 二次启动 S4

硬约束（违反即为错误输出，测试里有对应断言）：

1. **不用未来数据**：所有判定只看 `date` 及之前的 K 线，`asof()` 负责截断。
2. **不虚构不可得字段**：首封时间、炸板次数、封单金额、分时均价这些需要逐笔/盘口，
   本地日线拿不到，一律 `None` 并计入「数据完整度」，绝不填 0 或猜。
3. **不越阶段**：S1/S2 不给承接结论，未突破只写「等待启动」。
4. **不凑数**：精选最多 10 只，不足就少给，一只没有就明说。

口径提醒：涨停价按「四舍五入到分」计算（`limit_prices`），不能用 `round()`；
成交量单位是「股」、成交额是「元」（本地 .day 口径，与 tqcenter 快照的「手/万元」差 100 倍）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.adapters.tdx_daily import (
    asof,
    board_of,
    in_limitup_scope,
    limit_prices,
    limit_rate_of,
)

# ---------------------------------------------------------------- 常量与文案

WEIGHTS = {
    "first_board": 20.0,
    "vol": 20.0,
    "carry": 20.0,
    "launch": 25.0,
    "sector": 15.0,
}

STAGE_VERIFIED = {
    "S1": ("first_board", "vol", "sector"),
    "S2": ("first_board", "vol", "sector"),
    "S3": ("first_board", "vol", "carry", "sector"),
    "S4": ("first_board", "vol", "carry", "launch", "sector"),
}

STAGE_LABEL = {
    "S1": "首板发现",
    "S2": "三倍量确认",
    "S3": "次日承接",
    "S4": "二次启动确认",
}

DIM_LABEL = {
    "first_board": "首板质量",
    "vol": "三倍量确认",
    "carry": "次日承接",
    "launch": "二次启动",
    "sector": "板块与市场",
}

STATE_LABEL = {
    "S1": "首板发现",
    "S2": "三倍量确认",
    "S3": "等待次日承接",
    "S4": "承接有效",
    "S5": "缩量整理",
    "S6": "二次启动确认",
    "S7": "承接失败",
    "S8": "突破失败",
    "S9": "退出观察池",
    "S10": "交易周期结束",
}

CARRY_LABEL = {
    "A": "A 强势承接",
    "B": "B 平开换手",
    "C": "C 低开反包",
    "D": "D 承接失败",
}

LAUNCH_CHECKS = {
    "hold_support": "支撑未破",
    "vol_contraction": "整理缩量≤0.75",
    "range_stable": "区间不下移",
    "breakout": "收盘突破上沿",
    "vol_expansion": "放量≥1.5倍",
    "amount_grow": "额增≥1.3倍",
    "sector_ok": "板块未退潮",
}

LIMITUP_CHECKS = {
    "limit_up": "收盘涨停",
    "first_board": "前20日无涨停（严格首板）",
    "vol_3x": "5日量能≥3倍",
    "amount_2x": "成交额≥2倍",
    "close_above_open": "收盘≥开盘",
    "not_high_volume": "非高位巨量",
    "quality_not_c": "涨停质量非C级",
}

# 本地日线可以提供的字段（12 个）
DAILY_FIELDS = [
    "close", "pre_close", "pct", "volume", "amount", "upper_limit",
    "vol_ratio5", "amount_ratio5", "turnover", "ma5", "ma20", "sector_change",
]
# 需要分时/逐笔/委托队列才有，本地盘后数据不提供（5 个）
INTRADAY_FIELDS = [
    "first_touch_time", "first_seal_time", "open_board_count",
    "seal_amount", "intraday_vwap",
]

# 精选容量与阶段优先级
CAPACITY = {"S4": 5, "S3": 2, "S2": 3, "S1": 3}
PRIORITY = {"S4": 0, "S3": 1, "S2": 2, "S1": 3}

# 阶段 → 股票池状态
STAGE_TO_STATE = {"S1": "S1", "S2": "S2", "S3": "S4", "S4": "S6"}

NEXT_TRIGGER = {
    "S1": "次日不破首板收盘价 95%，且量能维持 1.5 倍以上 → 承接确认",
    "S2": "次日回踩不破首板收盘价、收盘站上分时均价线 → 承接确认",
    "S3": "缩量整理 1~10 日后再次放量并收盘突破整理上沿 → 二次启动",
    "S4": "已确认启动；次日放量延续则持有观察，跌破止损位离场",
}


class LimitupError(RuntimeError):
    """本地数据不足以出结论（目录缺失、日线不足、数据过期）。"""


# ---------------------------------------------------------------- 指标

def ma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def vol_ratio(bars: list[dict], n: int = 5) -> float | None:
    """当日量 / 前 n 日均量（不含当日）。"""
    if len(bars) < n + 1:
        return None
    base = sum(b["volume"] for b in bars[-n - 1:-1]) / n
    if base <= 0:
        return None
    return round(bars[-1]["volume"] / base, 2)


def amount_ratio(bars: list[dict], n: int = 5) -> float | None:
    if len(bars) < n + 1:
        return None
    base = sum(b["amount"] for b in bars[-n - 1:-1]) / n
    if base <= 0:
        return None
    return round(bars[-1]["amount"] / base, 2)


def turnover(volume: float, float_share_wan: float) -> float | None:
    """换手率(%) = 成交量(股) / 流通 A 股(股)；float_share 单位万股。"""
    if not float_share_wan or float_share_wan <= 0:
        return None
    return round(volume / (float_share_wan * 10000.0) * 100, 2)


def price_position(bars: list[dict], n: int = 60) -> float | None:
    if len(bars) < 2:
        return None
    seg = bars[-n:]
    hi = max(b["high"] for b in seg)
    lo = min(b["low"] for b in seg)
    if hi - lo <= 0:
        return None
    return round((seg[-1]["close"] - lo) / (hi - lo), 3)


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]["close"]
        trs.append(max(bars[i]["high"] - bars[i]["low"],
                       abs(bars[i]["high"] - prev),
                       abs(bars[i]["low"] - prev)))
    return round(sum(trs[-n:]) / n, 3)


def vwap(bar: dict) -> float | None:
    """当日成交均价（额/量），分时均价线的日线代理。"""
    volume = bar.get("volume") or 0
    if volume <= 0:
        return None
    return round((bar.get("amount") or 0) / volume, 3)


def upper_shadow_ratio(bar: dict) -> float:
    span = bar["high"] - bar["low"]
    if span <= 0:
        return 0.0
    return round((bar["high"] - bar["close"]) / span, 3)


def limit_up_in_window(bars: list[dict], rate: float, lookback: int = 20) -> bool:
    """近 lookback 个交易日（不含最后一根）是否出现过收盘涨停。"""
    seg = bars[-(lookback + 1):-1]
    for i in range(1, len(seg)):
        prev = seg[i - 1]["close"]
        if prev <= 0:
            continue
        up, _ = limit_prices(prev, rate)
        if up > 0 and abs(seg[i]["close"] - up) < 0.005:
            return True
    return False


def consecutive_boards(bars: list[dict], rate: float) -> int:
    """截至最后一根的连续涨停根数。"""
    count = 0
    for i in range(len(bars) - 1, 0, -1):
        prev = bars[i - 1]["close"]
        if prev <= 0:
            break
        up, _ = limit_prices(prev, rate)
        if up > 0 and abs(bars[i]["close"] - up) < 0.005:
            count += 1
        else:
            break
    return count


def macd(closes: list[float]) -> dict:
    if len(closes) < 26:
        return {"dif": None, "dea": None, "hist": None}

    def ema(values: list[float], n: int) -> list[float]:
        k = 2.0 / (n + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(out[-1] + k * (value - out[-1]))
        return out

    fast, slow = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = ema(dif, 9)
    return {"dif": round(dif[-1], 4), "dea": round(dea[-1], 4),
            "hist": round(2 * (dif[-1] - dea[-1]), 4)}


def rsi(closes: list[float], n: int = 6) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(change))

    def sma_cn(values: list[float], period: int) -> list[float]:
        out = [values[0]]
        for value in values[1:]:
            out.append((value + (period - 1) * out[-1]) / period)
        return out

    gain = sma_cn(gains, n)[-1]
    loss = sma_cn(losses, n)[-1]
    if loss == 0:
        return 100.0
    return round(gain / loss * 100, 2)


# ---------------------------------------------------------------- 快照

def build_snapshot(meta: dict, bars: list[dict], date: int,
                   sector_strength: dict | None) -> dict | None:
    """构建目标交易日的个股量价快照；数据不足或当日无 K 线时返回 None。"""
    if len(bars) < 25 or not bars or bars[-1]["date"] != date:
        return None
    last, prev = bars[-1], bars[-2]
    code = meta["code"]
    rate = meta.get("limit_rate") or limit_rate_of(code, meta.get("name", ""))
    up, down = limit_prices(prev["close"], rate)
    closes = [b["close"] for b in bars]
    sector = meta.get("sector") or {}
    strength = (sector_strength or {}).get(sector.get("tcode", ""), {})

    snap = {
        "date": date,
        "code": code,
        "symbol": meta.get("symbol") or f"{code}.{meta.get('market', 'SH')}",
        "name": meta.get("name") or code,
        "market": meta.get("market"),
        "board": meta.get("board") or board_of(code),
        "sector": sector.get("name") or meta.get("industry") or "未分类",
        "sector_tcode": sector.get("tcode", ""),
        "sector_index": sector.get("index_code", ""),
        "sector_change": strength.get("sector_change"),
        "sector_change_5d": strength.get("sector_change_5d"),
        "sector_amount": strength.get("sector_amount"),
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "close": last["close"],
        "pre_close": prev["close"],
        "pct": round(last["pct"], 2),
        "volume": last["volume"],
        "amount": last["amount"],
        "upper_limit": up,
        "lower_limit": down,
        "limit_rate": rate,
        "is_limit_up": up > 0 and abs(last["close"] - up) < 0.005,
        "touched_limit": up > 0 and last["high"] >= up - 0.005,
        "vol_ratio5": vol_ratio(bars, 5),
        "vol_ratio10": vol_ratio(bars, 10),
        "amount_ratio5": amount_ratio(bars, 5),
        "turnover": turnover(last["volume"], meta.get("float_share") or 0),
        "ma5": ma(closes, 5),
        "ma10": ma(closes, 10),
        "ma20": ma(closes, 20),
        "ma60": ma(closes, 60),
        "pct5": round((last["close"] / bars[-6]["close"] - 1) * 100, 2)
        if len(bars) >= 6 and bars[-6]["close"] > 0 else None,
        "pos20": price_position(bars, 20),
        "pos60": price_position(bars, 60),
        "atr14": atr(bars, 14),
        # vwap 是「额/量」的全天成交均价（日线代理），不等于分时均价线；
        # 真正的分时均价线在 intraday_vwap，本地日线拿不到，显式留空。
        "vwap": vwap(last),
        "intraday_vwap": None,
        "upper_shadow": upper_shadow_ratio(last),
        "is_first_board": False,
        "consecutive_boards": 0,
        # 本地日线拿不到，显式置空（不能填 0，那会被读成"封单为 0"）
        "first_touch_time": None,
        "first_seal_time": None,
        "open_board_count": None,
        "seal_amount": None,
        "quality": None,
        "quality_note": "未涨停",
    }
    if snap["is_limit_up"]:
        snap["is_first_board"] = not limit_up_in_window(bars, rate, 20)
        snap["consecutive_boards"] = consecutive_boards(bars, rate)
        snap["quality"], snap["quality_note"] = quality_grade(snap)
    snap["data_flags"] = [name for name in INTRADAY_FIELDS if snap.get(name) is None]
    return snap


def quality_grade(snap: dict) -> tuple[str, str]:
    """涨停质量分级（**日线代理口径**）。

    真实的「炸板次数 / 回封速度 / 封单金额」需要逐笔与盘口，本地日线没有，
    这里只按 K 线形态给代理评级，界面与报告必须标「日线代理」。
    """
    o, h, l, c = snap["open"], snap["high"], snap["low"], snap["close"]
    prev = snap["pre_close"]
    if h == l == c:
        return "A", "一字板（日线代理）"
    gap = (o / prev - 1) * 100 if prev else 0
    if h == c and gap >= 3:
        return "A", "高开直线封板（日线代理）"
    if h == c and snap["upper_shadow"] <= 0.02:
        return "B", "收盘即最高价，换手回封待分时确认（日线代理）"
    if l <= prev * 1.01:
        return "C", "盘中曾回落至前收附近，疑似炸板（日线代理）"
    return "C", "上影明显，封板质量存疑（日线代理）"


# ---------------------------------------------------------------- S1 / S2

def stage1_filter(snaps: list[dict]) -> list[dict]:
    """首板发现：收盘涨停 + 前 20 日无涨停 + 量能可算。"""
    return [s for s in snaps
            if s["is_limit_up"] and s["is_first_board"] and s["vol_ratio5"] is not None]


def stage2_confirm(snaps: list[dict]) -> tuple[list[dict], list[dict]]:
    """三倍量确认：返回 (通过, 被剔除并附原因)。原因要能被人工复核。"""
    passed, rejected = [], []
    for snap in snaps:
        reasons = []
        if (snap["vol_ratio5"] or 0) < 3:
            reasons.append(f"5日量能倍数 {snap['vol_ratio5']} < 3")
        if (snap["amount_ratio5"] or 0) < 2:
            reasons.append(f"成交额倍数 {snap['amount_ratio5']} < 2")
        if snap["close"] < snap["open"]:
            reasons.append("收盘低于开盘（放量滞涨/冲高回落）")
        if (snap["pos60"] or 0) >= 0.95 and (snap["vol_ratio5"] or 0) >= 6:
            reasons.append("高位巨量（60日区间顶部 + 6倍以上量能）")
        if snap["quality"] == "C":
            reasons.append("涨停质量 C 级（日线代理）")
        if reasons:
            rejected.append({"code": snap["code"], "name": snap["name"],
                             "reasons": reasons, "pct": snap["pct"],
                             "close": snap["close"], "vol_ratio5": snap["vol_ratio5"]})
        else:
            passed.append(snap)
    return passed, rejected


# ---------------------------------------------------------------- S3 次日承接

def evaluate_carry(record: dict, bars: list[dict], snap: dict | None) -> dict:
    """评估首板记录在承接日的表现。record 需含 first_board_date/first_board_close。"""
    board_date = record.get("first_board_date")
    board_close = record.get("first_board_close") or 0
    if snap is None:
        return {"carry_type": "D", "detail": "当日无有效行情（停牌或数据缺失）", "ok": False}
    if board_date and snap["date"] <= int(board_date):
        return {"carry_type": "D", "detail": "尚未进入承接日", "ok": False}

    gap = (snap["open"] / snap["pre_close"] - 1) * 100 if snap["pre_close"] else 0.0
    hold_support = snap["low"] >= board_close * 0.95
    above_vwap = snap["vwap"] is not None and snap["close"] >= snap["vwap"]
    vol_ok = 0.6 <= (snap["vol_ratio5"] or 0) <= 3.0
    sector_ok = (snap["sector_change"] if snap["sector_change"] is not None else 0) > -1.5

    if gap <= -2.5 and snap["close"] > snap["pre_close"] and snap["close"] > snap["open"]:
        ctype, detail = "C", "低开反包：低开后收复前收，需次日确认"
    elif snap["pct"] >= 2 and snap["close"] >= snap["open"] and hold_support and sector_ok:
        ctype, detail = "A", "强势承接：收红且收盘≥开盘，未破首板收盘 95%"
    elif snap["pct"] > -2 and hold_support:
        ctype, detail = "B", "平开换手：震荡整理，支撑有效"
    else:
        ctype, detail = "D", "承接失败：跌破首板收盘 95% 或收盘跌幅超 2%"
    return {
        "carry_type": ctype,
        "detail": detail,
        "ok": ctype in ("A", "B", "C"),
        "gap": round(gap, 2),
        "hold_support": hold_support,
        "above_vwap": above_vwap,
        "vol_ok": vol_ok,
        "sector_ok": sector_ok,
        "date": snap["date"],
        "pct": snap["pct"],
    }


# ---------------------------------------------------------------- S4 二次启动

def evaluate_second_launch(record: dict, bars: list[dict], snap: dict | None) -> dict:
    """首板后 1~10 日缩量整理 + 收盘放量突破整理上沿。"""
    board_date = record.get("first_board_date")
    board_close = record.get("first_board_close") or 0
    board_volume = record.get("first_board_volume") or 0
    if not board_date or not bars:
        return {"confirmed": False, "detail": "缺少首板基准数据", "waiting": True}

    index = next((i for i, b in enumerate(bars) if b["date"] == int(board_date)), None)
    if index is None:
        return {"confirmed": False, "detail": "首板日不在本地行情窗口内", "waiting": True}
    after = bars[index + 1:]
    if not after:
        return {"confirmed": False, "detail": "首板后尚无交易日数据", "waiting": True}

    today = bars[-1]
    consolidation = after[:-1]          # 整理期不含最新一根
    days = len(consolidation)
    if days == 0:
        return {"confirmed": False, "detail": "首板后首日，尚未形成整理区间", "waiting": True}
    if days > 10:
        return {"confirmed": False, "waiting": False, "expired": True,
                "detail": f"整理 {days} 日超出 10 日跟踪窗口，退出观察"}

    cons_vol = sum(b["volume"] for b in consolidation) / days
    cons_amount = sum(b["amount"] for b in consolidation) / days
    cons_high = max(b["high"] for b in consolidation)
    cons_low = min(b["low"] for b in consolidation)
    vol_contract = (cons_vol / board_volume) if board_volume else None
    second_ratio = (today["volume"] / cons_vol) if cons_vol else None
    amount_grow = (today["amount"] / cons_amount) if cons_amount else None
    support_ok = cons_low >= board_close * 0.93 and today["low"] >= board_close * 0.93
    lows = [b["low"] for b in consolidation]
    range_stable = len(lows) < 2 or lows[-1] >= min(lows) * 0.98
    sector_5d = (snap or {}).get("sector_change_5d")
    sector_ok = True if sector_5d is None else sector_5d > -3
    breakout = today["close"] > cons_high
    intraday_only = today["high"] > cons_high >= today["close"]

    checks = {
        "hold_support": support_ok,
        "vol_contraction": vol_contract is not None and vol_contract <= 0.75,
        "range_stable": range_stable,
        "breakout": breakout,
        "vol_expansion": second_ratio is not None and second_ratio >= 1.5,
        "amount_grow": amount_grow is not None and amount_grow >= 1.3,
        "sector_ok": sector_ok,
    }
    required = ("hold_support", "vol_contraction", "breakout", "vol_expansion", "sector_ok")
    confirmed = all(checks[k] for k in required)
    if confirmed:
        detail = (f"缩量整理 {days} 日后放量突破整理上沿 {cons_high:.2f}，"
                  f"量能 {second_ratio:.2f} 倍")
    elif intraday_only:
        detail = "盘中突破但收盘跌回区间内，标记为待确认"
    elif not breakout:
        detail = f"整理 {days} 日，收盘未突破上沿 {cons_high:.2f}，等待启动"
    else:
        detail = "突破但量能或支撑条件未同时满足，待确认"

    return {
        "confirmed": confirmed,
        "waiting": not confirmed,
        "intraday_only": intraday_only,
        "detail": detail,
        "cons_days": days,
        "cons_high": round(cons_high, 2),
        "cons_low": round(cons_low, 2),
        "vol_contract": round(vol_contract, 2) if vol_contract is not None else None,
        "second_vol_ratio": round(second_ratio, 2) if second_ratio is not None else None,
        "amount_grow": round(amount_grow, 2) if amount_grow is not None else None,
        "checks": checks,
    }


# ---------------------------------------------------------------- 评分

def _first_board_score(snap: dict) -> tuple[float, list[str]]:
    points, notes = 0.0, []
    if snap["is_limit_up"]:
        points += 4
        notes.append("收盘涨停 +4")
    if snap["high"] == snap["close"]:
        points += 4
        notes.append("收盘即最高价 +4")
    if snap["upper_shadow"] <= 0.05:
        points += 3
        notes.append("几乎无上影 +3")
    elif snap["upper_shadow"] <= 0.15:
        points += 1.5
        notes.append("上影较小 +1.5")
    to = snap["turnover"]
    if to is not None:
        if 3 <= to <= 15:
            points += 5
            notes.append(f"换手 {to}% 处于健康区间 +5")
        elif 1.5 <= to < 3 or 15 < to <= 25:
            points += 3
            notes.append(f"换手 {to}% 一般 +3")
        else:
            notes.append(f"换手 {to}% 偏离健康区间 +0")
    if snap["is_first_board"]:
        points += 4
        notes.append("严格首板（前20日无涨停）+4")
    return min(points, WEIGHTS["first_board"]), notes


def _vol_score(snap: dict) -> tuple[float, list[str]]:
    points, notes = 0.0, []
    v5 = snap["vol_ratio5"] or 0
    if v5 >= 5:
        points += 10
        notes.append(f"5日量能 {v5} 倍（≥5）+10")
    elif v5 >= 3:
        points += 8
        notes.append(f"5日量能 {v5} 倍（≥3）+8")
    elif v5 >= 2:
        points += 4
        notes.append(f"5日量能 {v5} 倍（未达3倍）+4")
    a5 = snap["amount_ratio5"] or 0
    if a5 >= 2:
        points += 6
        notes.append(f"成交额 {a5} 倍 +6")
    elif a5 >= 1.5:
        points += 3
        notes.append(f"成交额 {a5} 倍 +3")
    v10 = snap["vol_ratio10"] or 0
    if v10 >= 2:
        points += 4
        notes.append(f"10日量能 {v10} 倍 +4")
    return min(points, WEIGHTS["vol"]), notes


def _carry_score(carry: dict | None) -> tuple[float, list[str]]:
    if not carry:
        return 0.0, ["承接未发生（不计分）"]
    table = {"A": 20.0, "B": 14.0, "C": 10.0, "D": 0.0}
    points = table.get(carry["carry_type"], 0.0)
    notes = [f"承接类型 {carry['carry_type']} +{points}"]
    if carry.get("above_vwap"):
        notes.append("收盘站上当日均价")
    return points, notes


def _launch_score(launch: dict | None) -> tuple[float, list[str]]:
    if not launch or not launch.get("confirmed"):
        return 0.0, ["二次启动未确认（不计分）"]
    points, notes = 10.0, ["收盘有效突破整理上沿 +10"]
    ratio = launch.get("second_vol_ratio") or 0
    if ratio >= 2:
        points += 8
        notes.append(f"突破日量能 {ratio} 倍（≥2）+8")
    elif ratio >= 1.5:
        points += 6
        notes.append(f"突破日量能 {ratio} 倍 +6")
    contract = launch.get("vol_contract")
    if contract is not None and contract <= 0.6:
        points += 4
        notes.append(f"整理期缩量至首板 {contract} 倍 +4")
    elif contract is not None and contract <= 0.75:
        points += 2.5
        notes.append(f"整理期缩量至首板 {contract} 倍 +2.5")
    if (launch.get("checks") or {}).get("hold_support"):
        points += 3
        notes.append("关键支撑未破 +3")
    return min(points, WEIGHTS["launch"]), notes


def _sector_score(snap: dict, sector_rank: tuple[int, int] | None,
                  market: dict | None) -> tuple[float, list[str]]:
    points, notes = 0.0, []
    change = snap.get("sector_change")
    if change is not None:
        if sector_rank and sector_rank[1] > 0:
            percentile = 1 - (sector_rank[0] - 1) / max(sector_rank[1], 1)
            points += round(6 * percentile, 1)
            notes.append(f"板块排名 {sector_rank[0]}/{sector_rank[1]} +{round(6 * percentile, 1)}")
        elif change > 0:
            points += 3
            notes.append(f"板块上涨 {change}% +3")
    change_5d = snap.get("sector_change_5d")
    if change_5d is not None:
        if change_5d >= 3:
            points += 4
            notes.append(f"板块 5 日强度 {change_5d}% +4")
        elif change_5d > 0:
            points += 2
            notes.append(f"板块 5 日强度 {change_5d}% +2")
    if market:
        count = market.get("limit_up_count", 0)
        if count >= 50:
            points += 5
            notes.append(f"市场涨停 {count} 家，情绪偏强 +5")
        elif count >= 25:
            points += 3
            notes.append(f"市场涨停 {count} 家 +3")
        else:
            points += 1
            notes.append(f"市场涨停仅 {count} 家 +1")
    return min(points, WEIGHTS["sector"]), notes


def data_completeness(snap: dict) -> tuple[float, list[str]]:
    """按快照实际取值算完整度。

    分时字段（首封时间/炸板次数/封单金额/分时均价）本地日线恒为 None，
    所以当前口径上限就是 12/17 ≈ 0.706、置信最高 C；将来接上分时数据源后
    这些字段一旦有值，完整度会自动抬升，不需要改这里。
    """
    present = 0
    missing: list[str] = []
    for name in DAILY_FIELDS + INTRADAY_FIELDS:
        if snap.get(name) is None:
            missing.append(name)
        else:
            present += 1
    total = len(DAILY_FIELDS) + len(INTRADAY_FIELDS)
    return round(present / total, 3), missing


def grade_of(normalized: float) -> str:
    """百分制 → 评级。≥90 S级 / ≥80 A级 / ≥70 B级 / ≥60 C级 / 其余淘汰。"""
    if normalized >= 90:
        return "S级"
    if normalized >= 80:
        return "A级"
    if normalized >= 70:
        return "B级"
    if normalized >= 60:
        return "C级"
    return "淘汰"


def score(snap: dict, stage: str, carry: dict | None = None, launch: dict | None = None,
          sector_rank: tuple[int, int] | None = None,
          market: dict | None = None) -> dict:
    """按阶段门控评分：未发生的阶段不计入分母，另列「未验证指标」。"""
    verified = STAGE_VERIFIED[stage]
    parts = {
        "first_board": _first_board_score(snap),
        "vol": _vol_score(snap),
        "carry": _carry_score(carry),
        "launch": _launch_score(launch),
        "sector": _sector_score(snap, sector_rank, market),
    }
    raw = sum(parts[key][0] for key in verified)
    max_raw = sum(WEIGHTS[key] for key in verified)
    normalized = round(raw / max_raw * 100, 1) if max_raw else 0.0
    completeness, missing = data_completeness(snap)
    confidence = "A" if completeness >= 0.9 else "B" if completeness >= 0.75 \
        else "C" if completeness >= 0.6 else "D"

    # 未验证维度一律记 0 分并标注「不计分」：分母不含它，若 detail 还留着满分，
    # 界面上的「承接 20/20」会被读成"已经算过分了"，等于把没发生的事算进结论。
    detail = {}
    for key, (points, notes) in parts.items():
        if key in verified:
            detail[key] = {"points": round(points, 1), "notes": notes, "verified": True}
        else:
            detail[key] = {"points": 0.0, "verified": False,
                           "notes": [f"该阶段尚未发生（{DIM_LABEL[key]}不计分）"]}
    return {
        "raw": round(raw, 1),
        "max_raw": round(max_raw, 1),
        "normalized": normalized,
        "grade": grade_of(normalized),
        "confidence": confidence,
        "completeness": completeness,
        "verified_dims": list(verified),
        "unverified": [k for k in parts if k not in verified],
        "missing_fields": missing,
        "detail": detail,
    }


# ---------------------------------------------------------------- 板块 / 市场

def aggregate_sectors(snaps: list[dict], min_members: int = 3) -> dict[str, dict]:
    """成分股等权聚合板块当日与 5 日强度（880xxx 指数缺该日数据时降级用）。"""
    groups: dict[str, dict] = {}
    for snap in snaps:
        tcode = snap.get("sector_tcode") or ""
        if not tcode:
            continue
        group = groups.setdefault(tcode, {"name": snap["sector"], "pcts": [], "pct5s": [],
                                          "amount": 0.0})
        group["pcts"].append(snap["pct"])
        if snap.get("pct5") is not None:
            group["pct5s"].append(snap["pct5"])
        group["amount"] += snap["amount"]
    out: dict[str, dict] = {}
    for tcode, group in groups.items():
        if len(group["pcts"]) < min_members:
            continue
        out[tcode] = {
            "name": group["name"],
            "index_code": "",
            "sector_change": round(sum(group["pcts"]) / len(group["pcts"]), 2),
            "sector_change_5d": round(sum(group["pct5s"]) / len(group["pct5s"]), 2)
            if group["pct5s"] else None,
            "sector_amount": group["amount"],
            "caliber": "成分股等权（行业指数缺该日数据）",
        }
    return out


def market_environment(snaps: list[dict], sector_strength: dict) -> dict:
    """全市场环境：涨停家数、炸板率（触板未封的代理口径）、情绪分、主线板块。"""
    ups = [s for s in snaps if s["is_limit_up"]]
    touched = [s for s in snaps if s["touched_limit"]]
    downs = [s for s in snaps if s["pct"] < 0]
    fail = [s for s in touched if not s["is_limit_up"]]
    fail_rate = round(len(fail) / len(touched) * 100, 1) if touched else None
    max_boards = max((s["consecutive_boards"] for s in ups), default=0)

    ranked = sorted(((k, v) for k, v in sector_strength.items()),
                    key=lambda kv: kv[1].get("sector_change")
                    if kv[1].get("sector_change") is not None else -99,
                    reverse=True)
    leaders = [{"name": v["name"], "change": v.get("sector_change"),
                "count": len([s for s in ups if s["sector_tcode"] == k])}
               for k, v in ranked[:8]]
    main_lines = [x["name"] for x in leaders[:3] if (x["count"] or 0) >= 1]

    environment = 50.0
    environment += min(len(ups), 80) * 0.5
    environment -= min(len(downs), 3000) / 3000 * 20
    if fail_rate is not None:
        environment += (40 - fail_rate) * 0.25
    environment = max(0.0, min(100.0, environment))
    if environment >= 70:
        mood, risk = "偏强", "中低"
    elif environment >= 50:
        mood, risk = "中性", "中"
    elif environment >= 30:
        mood, risk = "偏弱", "中高"
    else:
        mood, risk = "冰点", "高"

    return {
        "limit_up_count": len(ups),
        "limit_down_count": len([s for s in snaps
                                 if not s["is_limit_up"] and s["pct"] <= -9.5]),
        "touched_count": len(touched),
        "fail_rate": fail_rate,
        "up_count": len([s for s in snaps if s["pct"] > 0]),
        "down_count": len(downs),
        "max_consecutive": max_boards,
        "first_board_count": len([s for s in ups if s["is_first_board"]]),
        "sentiment_score": round(environment, 1),
        "mood": mood,
        "risk_level": risk,
        "main_lines": main_lines,
        "sector_leaders": leaders,
    }


# ---------------------------------------------------------------- 精选与计划

def select_final(candidates: list[dict], limit: int = 10) -> list[dict]:
    """按「二次启动 > 承接有效 > 三倍量 > 首板」去重选取，不凑数。"""
    def code_of(item: dict) -> str:
        return item.get("code") or (item.get("snap") or {}).get("code", "")

    seen: set[str] = set()
    picked: list[dict] = []
    for stage in ("S4", "S3", "S2", "S1"):
        group = [c for c in candidates if c["stage"] == stage]
        group.sort(key=lambda c: (-c["score"]["normalized"], code_of(c)))
        quota = CAPACITY.get(stage, 0)
        taken = 0
        for item in group:
            if taken >= quota or len(picked) >= limit:
                break
            code = code_of(item)
            if code in seen or item["score"]["grade"] == "淘汰":
                continue
            seen.add(code)
            picked.append(item)
            taken += 1
    picked.sort(key=lambda c: (PRIORITY[c["stage"]], -c["score"]["normalized"]))
    return picked


def build_plan(snap: dict, record: dict | None, launch: dict | None, stage: str) -> dict:
    """交易计划：支撑 / 止损 / 目标 / 触发条件。止损用 ATR，不用固定百分比。"""
    close = snap["close"]
    atr14 = snap["atr14"] or max(close * 0.03, 0.05)
    board_close = (record or {}).get("first_board_close") or close
    cons_low = (launch or {}).get("cons_low")
    cons_high = (launch or {}).get("cons_high")

    support_pool = [board_close * 0.95]
    if cons_low:
        support_pool.append(cons_low)
    if snap.get("ma5"):
        support_pool.append(snap["ma5"])
    support = round(max(support_pool), 2)
    stop = round(support - 0.5 * atr14, 2)
    if stop >= close:
        stop = round(close - 1.2 * atr14, 2)
    resistance = round(cons_high if cons_high else max(snap["high"], close), 2)
    risk_per_share = max(close - stop, 0.01)
    t1 = round(close + risk_per_share * 1.5, 2)
    t2 = round(close + risk_per_share * 2.5, 2)

    if stage in ("S1", "S2"):
        vol_confirm = (f"次日成交量 ≥ 首板日量能 60% 且收盘站上 {snap['pre_close']:.2f} 为承接有效；"
                       f"放量突破需 > 首板日量能 1.5 倍")
        break_confirm = f"收盘价站上首板日最高价 {snap['high']:.2f} 且量能同步放大"
    elif stage == "S3":
        vol_confirm = "承接日量能维持在首板日 60%~150% 为健康换手；缩量至 60% 以下视为承接不足"
        break_confirm = f"收盘价站稳首板日收盘价 {board_close:.2f} 上方并放量"
    else:
        vol_confirm = "突破日成交量 ≥ 整理期均量 1.5 倍，且成交额同步放大 1.3 倍以上"
        break_confirm = f"收盘价站上整理区间上沿 {resistance:.2f}"

    return {
        "watch_range": (round(min(support, close) * 0.99, 2),
                        round(max(resistance, close) * 1.02, 2)),
        "support": support,
        "resistance": resistance,
        "stop": stop,
        "target1": t1,
        "target2": t2,
        "atr": round(atr14, 3),
        "risk_per_share": round(risk_per_share, 2),
        "risk_pct": round(risk_per_share / close * 100, 2) if close else None,
        "vol_confirm": vol_confirm,
        "break_confirm": break_confirm,
        "entry": f"回踩 {support} 不破且站上分时均价线可轻仓；高开超过 +5% 不追",
        "invalidation": f"收盘跌破 {stop}，或板块 5 日退潮、放量滞涨",
    }


def risk_of(snap: dict) -> str:
    if (snap.get("pos60") or 0) >= 0.9 or (snap.get("vol_ratio5") or 0) >= 8:
        return "高"
    if (snap.get("pos60") or 0) >= 0.75 or (snap.get("vol_ratio5") or 0) >= 5:
        return "中高"
    return "中"


def reason_text(snap: dict, stage: str, carry: dict | None, launch: dict | None) -> str:
    base = (f"{snap['sector']}板块，5 日量能 {_num(snap['vol_ratio5'])} 倍、"
            f"成交额倍数 {_num(snap['amount_ratio5'])}、换手 {_num(snap['turnover'])}%，"
            f"收盘 {snap['close']:.2f}（{snap['pct']:+.2f}%）")
    if stage == "S1":
        return f"严格首板（前 20 日无涨停），{base}；涨停质量 {snap.get('quality')} 级（日线代理）"
    if stage == "S2":
        return f"首板 + 三倍量确认（{_num(snap['vol_ratio5'])} 倍），{base}"
    if stage == "S3":
        return f"首板次日{(carry or {}).get('carry_type', '?')}类承接：" \
               f"{(carry or {}).get('detail', '')}；{base}"
    if stage == "S4":
        return f"二次启动确认：{(launch or {}).get('detail', '')}；{base}"
    return base


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def limitup_checklist(snap: dict, stage: str) -> dict:
    """把「八项/七项条件」摊平成界面可勾选的表，避免用户只看分数。"""
    return {
        "limit_up": bool(snap.get("is_limit_up")),
        "first_board": bool(snap.get("is_first_board")),
        "vol_3x": (snap.get("vol_ratio5") or 0) >= 3,
        "amount_2x": (snap.get("amount_ratio5") or 0) >= 2,
        "close_above_open": snap["close"] >= snap["open"],
        "not_high_volume": not ((snap.get("pos60") or 0) >= 0.95
                                and (snap.get("vol_ratio5") or 0) >= 6),
        "quality_not_c": snap.get("quality") != "C",
    }


# ---------------------------------------------------------------- 复盘收益

def review_returns(bars: list[dict], first_board_date: int,
                   horizons: tuple[int, ...] = (1, 2, 3, 5, 10)) -> dict:
    """首板日之后 T+N 个交易日的收盘收益（%）。不足 N 日的留 None，不填 0。"""
    if not bars or not first_board_date:
        return {}
    index = next((i for i, b in enumerate(bars) if b["date"] == int(first_board_date)), None)
    if index is None:
        return {}
    base = bars[index]["close"]
    if base <= 0:
        return {}
    out: dict[str, float | None] = {"first_board_close": round(base, 2)}
    for n in horizons:
        target = index + n
        if target < len(bars):
            out[f"t{n}"] = round((bars[target]["close"] / base - 1) * 100, 2)
        else:
            out[f"t{n}"] = None
    return out
