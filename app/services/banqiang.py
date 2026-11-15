"""量学板枪选股规则引擎（移植自 liangxue-banqiang-hunter，纯计算，不含任何 IO）。

五阶段（板日 = D0，枪日 = D1，回调 = D2~D6）：

    第一阶段 涨停板筛选      → stage1_board()
    第二阶段 放量出枪识别    → stage2_gun()
    第三阶段 缩量回调识别    → stage3_pullback()
    第四阶段 黄金阶梯与筹码  → stage4_stage_chip()
    第五阶段 二次启动买点    → stage5_buy()

口径（与 references/strategy.md 逐条对应，别随手改常量）：

- 涨停是**双条件**：涨幅落在板幅 ±0.35 百分点，且收盘价 = 涨停价（容差 0.02 元）。
  只看涨幅会把低价股（1.82 元 ST 涨停价 1.91 = +4.95%）误判成涨停；
- 「回撤 ≥ 1/2」是**评级线不是生死线**：≥0.50 走深调买点 A（阳盖阴），
  0.25~0.50 走浅调买点 B（过枪的高量顶）并降一档评级，<0.25 直接淘汰；
- 止损 = 回调低点 ×(1−1%)，板日开盘单列为**结构底线**，两者不是一回事；
- 封单额 / 开板次数 / 首封时间 / 分时人线 / 三波上涨 / 真实筹码分布：本地日线给不出，
  一律标「不可验证」并按中性基准给分，绝不拿日线冒充。

风险提示：本模块只做形态识别与评分，不构成投资建议，不自动下单。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.adapters.tdx_daily import limit_prices, limit_rate_of

# ==========================================================================
# 一、参数常量区（调参只改这里，改完同步 config.yaml 与 references/strategy.md）
# ==========================================================================

# ---- 第一阶段：板日 ------------------------------------------------------
BOARD_LOOKBACK = 5          # 最近 5 个交易日内出现过涨停
BOARD_PCT_TOL = 0.35        # 涨停涨幅容差（百分点），吸收报价四舍五入
LIMIT_PRICE_TOL = 0.02      # 收盘价与涨停价的最大价差（元）
BOARD_VOL_MIN = 1.30        # 板日量 ≥ 前一日 × 1.30
BOARD_SMALL_DOUBLE = (1.5, 3.0)   # 小倍阳区间（量比）

# ---- 第二阶段：枪日 ------------------------------------------------------
GUN_VOL_VS_BOARD = 1.00     # 枪日量 ≥ 板日量
GUN_VOL_VS_MA5 = 1.20       # 枪日量 ≥ 前 5 日均量 × 1.20
GUN_BREAK_BOARD_HIGH = True  # 枪日最高价须超过板日最高价
GUN_SHADOW_MIN = 0.12       # 上影占振幅比例下限（长上影）
GUN_SHADOW_PCT = 0.008      # 上影绝对幅度下限（占收盘价）
GUN_CLOSE_POS_A = 0.50      # A 级枪：收盘位置下限
GUN_CLOSE_POS_C = 0.35      # 低于此值且上影过大 → C 级冲高回落淘汰
GUN_SHADOW_C = 0.55         # C 级：上影 / 振幅 超过此值
GUN_VOL_TOP_RANK = 3        # 「高量柱」：近 60 日量能排名前 N

# ---- 第三阶段：缩量回调 --------------------------------------------------
SHRINK_RATIO = 2.0 / 3.0    # 回调首日量 ≤ 枪日量 × 2/3（缩量 1/3 以上）
VOL_STEP_TOL = 1.25         # 回调期逐级递减容忍（后一日量 ≤ 前一日 × 1.25）
MAX_PULLBACK_DAYS = 5       # 回调 ≤ 5 个交易日
MIN_RATIO = 0.25            # 回撤比入池下限
RATIO_OK = 0.50             # 回撤 ≥ 前期涨幅的一半 = 文档达标线
RATIO_SWEET = (0.50, 0.70)  # 最健康的回撤区间
RATIO_WEAK = 0.85           # 回撤过深，结构偏脆
BIG_YIN_PCT = 5.0           # 大阴线判定（跌幅%）
DROP_VETO_PCT = 7.0         # 单日暴跌否决线（%）

# ---- 第四阶段：位置 / 阶梯 / 筹码 ---------------------------------------
POS_DAYS = 120              # 价格分位回看
POS_MIN, POS_MAX = 0.20, 0.85
STEP_DAYS = 60              # 阶梯统计窗口
STEP_VOL_MULT = 1.80        # 台阶：量 ≥ 前一日 × 1.8 且创近 5 日新高
STEP_MIN, STEP_MAX = 1, 12
STEP_GOOD = (2, 3)          # 优先：第三级阶梯附近
STEP_WARN = 5               # 谨慎：第五级
STEP_VETO = 7               # 高风险：第七级及以上
GAP_PEAK_DAYS = 60          # 凹间峰回看窗口
GAP_PEAK_RECENT = 10        # 最近 10 日不参与凹峰识别
GAP_NEAR_PCT = 5.0          # 距凹峰 5% 内 = 接近突破
CHIP_DAYS = 120             # 筹码估算窗口
HIGHVOL_DAYS = 250          # 「500里高量」代理窗口

# ---- 均线 ----------------------------------------------------------------
MA_FAST_1, MA_FAST_2, MA_FAST_3, MA_LONG = 5, 10, 20, 60
MA_SLOPE_DAYS = 5

# ---- 风控与目标位 --------------------------------------------------------
STOP_LOSS_PCT = 0.01        # 止损 = 结构位 × (1-1%)
TARGET2_LOOKBACK = 120      # 左侧大阴实顶回看
TARGET2_BIG_YIN = 5.0       # 大阴线跌幅门槛
CHASE_TOL = 0.03            # 次日开盘高于参考买价 3% 视为追高（仅提示）

# ---- 评分权重（合计 = 100）----------------------------------------------
DIM_LABEL = {
    "board": "首板质量与涨停强度",
    "gun": "第二日放量出枪质量",
    "pull": "第三日缩量回调质量",
    "support": "回调支撑及底部结构",
    "step": "黄金阶梯及凹间峰位置",
    "chip": "四维筹码及上方压力",
    "sector": "板块热度及市场人气",
    "buy": "分时资金及二次启动信号",
}
DIM_MAX = {"board": 15, "gun": 15, "pull": 20, "support": 15,
           "step": 10, "chip": 10, "sector": 5, "buy": 10}

GRADE_BANDS = [(90, "S"), (80, "A"), (70, "B"), (60, "C"), (0, "D")]

# 状态机（扫描版只会产出前四种；持仓/卖出需要带持仓跟踪，本模块不产出）
STATE_ORDER = {
    "买入观察": 5,
    "买点临近": 4,
    "缩量回调中": 3,
    "形态形成中": 2,
    "卖出预警": 1,
    "持仓观察": 0,
    "形态失效": -1,
}
STATE_GROUPS = ("买入观察", "买点临近", "缩量回调中", "形态形成中")

# 界面「逐项核」用的条件清单：键 -> 人话。值在 checks 里给出 ok / 实测值
CHECKS = {
    "board_limit": "板日：收盘 = 涨停价（不是炸板）",
    "board_yang": "板日：收阳线",
    "board_vol": "板日：量 ≥ 前一日 1.3 倍",
    "gun_vol": "枪日：量 ≥ 板日，且 ≥ 5 日均量 1.2 倍",
    "gun_high": "枪日：最高价创出新高（≥ 板日最高）",
    "gun_shape": "枪日：长上影或高量强枪（有出枪形态）",
    "pull_shrink": "回调：首日缩量 ≥ 1/3",
    "pull_vol": "回调：量能逐级收缩（放量反弹 < 2 次）",
    "pull_support": "回调：不破关键支撑（深调守板日开盘）",
    "pull_ratio": "回调：回撤比 ≥ 0.25（0.50 为达标线）",
    "pull_days": "回调：≤ 5 个交易日",
    "buy_trigger": "买点：触发条件已满足",
    "ma": "均线：MA5>MA10>MA20 且 MA60 向上",
}

# 数据缺口：这些字段本地日线拿不到，报告里必须逐条说清
UNVERIFIABLE = (
    "封单金额 / 开板次数 / 首次封板时间：本地日线不可得，用「板日开盘涨幅」代理封板强度",
    "分时三波上涨 / 谷底 / 人线：分钟线不可得，D8 分时资金按中性基准给分",
    "筹码分布：用近 120 日「成交均价×成交量」估算，不是真实持仓分布",
    "价格为不复权：除权除息日前后请人工核对该股日线",
)


def r2(value: float) -> float:
    """四舍五入到分（避开 Python 银行家舍入）。"""
    return math.floor(float(value) * 100 + 0.5) / 100.0


# ==========================================================================
# 二、日线序列（把 list[dict] 拆成列存，规则引擎里只按下标取）
# ==========================================================================

@dataclass
class Series:
    """列存日线：d 日期(int YYYYMMDD)、o/h/l/c 价、v 量、a 额。"""

    d: list
    o: list
    h: list
    l: list
    c: list
    v: list
    a: list

    def __len__(self) -> int:
        return len(self.c)


def build_series(bars: list) -> Series:
    """TdxDailySource.bars_list() 的输出 → 列存。缺字段直接抛，不做静默补零。"""
    return Series(
        d=[int(b["date"]) for b in bars],
        o=[float(b["open"]) for b in bars],
        h=[float(b["high"]) for b in bars],
        l=[float(b["low"]) for b in bars],
        c=[float(b["close"]) for b in bars],
        v=[float(b.get("volume") or 0.0) for b in bars],
        a=[float(b.get("amount") or 0.0) for b in bars],
    )


def ma_at(values: list, n: int, idx: int):
    if idx - n + 1 < 0 or idx >= len(values) or idx < 0:
        return None
    return sum(values[idx - n + 1: idx + 1]) / n


def limit_up_price(prev_close: float, rate: float) -> float:
    return limit_prices(prev_close, rate)[0]


def is_limit_up(s: Series, i: int, rate: float) -> bool:
    """真实涨停：涨幅落在板幅附近 **且** 收盘价等于涨停价（双条件）。"""
    if i < 1 or i >= len(s):
        return False
    pre = s.c[i - 1]
    if pre <= 0:
        return False
    pct = (s.c[i] / pre - 1) * 100
    if not (rate * 100 - BOARD_PCT_TOL <= pct <= rate * 100 + BOARD_PCT_TOL):
        return False
    return abs(s.c[i] - limit_up_price(pre, rate)) <= LIMIT_PRICE_TOL


def board_streak(s: Series, i: int, rate: float) -> int:
    """板日 i 是几连板。"""
    n = 0
    j = i
    while j >= 1 and is_limit_up(s, j, rate):
        n += 1
        j -= 1
    return max(n, 1)


def board_days(s: Series, end: int, rate: float, lookback: int = BOARD_LOOKBACK) -> list:
    """最近 lookback 个交易日内的板日（含 end 当天），由近到远。"""
    out = []
    for back in range(0, lookback + 1):
        i = end - back
        if i < 1:
            break
        if is_limit_up(s, i, rate):
            out.append(i)
    return out


@dataclass
class Fail:
    """漏斗原因：stage 是推进到第几阶段倒下，text 是可直接展示的一句话。"""

    stage: int
    text: str

    def __str__(self) -> str:
        return f"{self.stage}|{self.text}"


# ==========================================================================
# 三、五阶段识别
# ==========================================================================

def stage1_board(s: Series, i: int, rate: float) -> tuple:
    """第一阶段：真实涨停 + 阳线 + 成交量明显变化。"""
    if i < 1:
        return None, Fail(1, "板日-数据不足")
    pre = s.c[i - 1]
    limit_price = limit_up_price(pre, rate)
    if not is_limit_up(s, i, rate):
        if s.h[i] >= limit_price - LIMIT_PRICE_TOL:
            return None, Fail(1, "板日-炸板未封（收盘没封住涨停）")
        return None, Fail(1, "板日-未涨停")
    if s.c[i] <= s.o[i]:
        return None, Fail(1, "板日-非阳线")
    vol_mult = s.v[i] / s.v[i - 1] if s.v[i - 1] else 0.0
    if vol_mult < BOARD_VOL_MIN:
        return None, Fail(1, "板日-量能未放大")
    streak = board_streak(s, i, rate)
    return {
        "rate": rate,
        "limit_price": limit_price,
        "board_vol_mult": vol_mult,
        "board_gap_pct": (s.o[i] / pre - 1) * 100 if pre else 0.0,
        "board_streak": streak,
        "is_first_board": streak == 1,
        "board_amount": s.a[i],
    }, None


def vol_rank(s: Series, k: int, days: int) -> int:
    """当日量在近 days 日里的排名（1 = 最大）。"""
    lo = max(0, k - days + 1)
    seg = s.v[lo: k + 1]
    if not seg:
        return 999
    return sum(1 for x in seg if x >= s.v[k])


def stage2_gun(s: Series, i: int, k: int, s1: dict) -> tuple:
    """第二阶段：放量出枪 + 阳枪/阴枪分级。"""
    if k >= len(s):
        return None, Fail(2, "枪日-尚未出现")
    if s.v[k] < s.v[i] * GUN_VOL_VS_BOARD:
        return None, Fail(2, "枪日-未放量（低于板日量）")
    ma5_prev = ma_at(s.v, 5, k - 1)
    if ma5_prev and s.v[k] < ma5_prev * GUN_VOL_VS_MA5:
        return None, Fail(2, "枪日-低于 5 日均量 1.2 倍")
    if GUN_BREAK_BOARD_HIGH and s.h[k] < s.h[i]:
        return None, Fail(2, "枪日-未创新高")

    rng = s.h[k] - s.l[k]
    if rng <= 0:
        return None, Fail(2, "枪日-振幅为 0")
    shadow = s.h[k] - max(s.o[k], s.c[k])
    shadow_ratio = shadow / rng
    shadow_pct = shadow / s.c[k] if s.c[k] else 0.0
    close_pos = (s.c[k] - s.l[k]) / rng
    yang = s.c[k] > s.o[k]

    # 长上影是「出枪」的核心特征；另一种合法形态是「高量柱 + 收盘在高位」的强枪。
    # 仅「小涨 2%」不算出枪——那是没有冲高动作的普通上涨。
    has_shadow = (shadow_ratio >= GUN_SHADOW_MIN and shadow_pct >= GUN_SHADOW_PCT)
    rank60 = vol_rank(s, k, 60)
    strong_bar = (rank60 <= GUN_VOL_TOP_RANK and close_pos >= 0.70)
    if not (has_shadow or strong_bar):
        return None, Fail(2, "枪日-无出枪形态")

    if shadow_ratio > GUN_SHADOW_C and close_pos < GUN_CLOSE_POS_C:
        return None, Fail(2, "枪日-C 级冲高回落")

    if yang and close_pos >= GUN_CLOSE_POS_A and s.v[k] >= s.v[i] * 1.20:
        grade = "A"
    elif yang or s.c[k] >= s.c[i]:
        grade = "B"
    elif s.c[k] >= s.o[i]:
        grade = "B"
    else:
        return None, Fail(2, "枪日-C 级阴枪破板日开盘")

    rank250 = vol_rank(s, k, HIGHVOL_DAYS)
    return {
        "gun_type": "阳枪" if yang else "阴枪",
        "gun_grade": grade,
        "gun_shadow_ratio": shadow_ratio,
        "gun_shadow_pct": shadow_pct,
        "gun_close_pos": close_pos,
        "gun_vol_mult": s.v[k] / s.v[i] if s.v[i] else 0.0,
        "gun_vol_vs_ma5": (s.v[k] / ma5_prev) if ma5_prev else 0.0,
        "gun_vol_rank60": rank60,
        "gun_vol_rank250": rank250,
        "gun_high": s.h[k],
        "gun_close": s.c[k],
        "gun_low": s.l[k],
        "gun_amount": s.a[k],
        "high_vol_500": rank250 <= 3,       # 「500 里高量」代理：250 日量能前 3
    }, None


def stage3_pullback(s: Series, i: int, k: int, end: int, s1: dict, s2: dict) -> tuple:
    """第三阶段：缩量回调（策略核心过滤）。"""
    if end <= k:
        return None, Fail(3, "回调-尚未开始")
    pull_days = end - k
    if pull_days > MAX_PULLBACK_DAYS:
        return None, Fail(3, "回调-超过 5 个交易日")

    first = k + 1
    if s.v[first] > s.v[k] * SHRINK_RATIO:
        return None, Fail(3, "回调-首日未缩量 1/3")

    # 量能总体收缩：允许 ≤25% 的单日反弹，不允许持续放量
    rebounds = 0
    for t in range(first + 1, end + 1):
        if s.v[t] > s.v[t - 1] * VOL_STEP_TOL:
            rebounds += 1
    if rebounds >= 2:
        return None, Fail(3, "回调-量能未收缩（多次放量）")

    window = s.l[first: end + 1]
    pull_low = min(window)
    pull_low_i = first + window.index(pull_low)
    rise = s2["gun_high"] - s.o[i]
    if rise <= 0:
        return None, Fail(3, "回调-上涨幅度为 0")
    ratio = (s2["gun_high"] - pull_low) / rise

    if pull_low < s.o[i] - 1e-9:
        return None, Fail(3, "回调-跌破板日开盘（结构失效）")
    if ratio < MIN_RATIO:
        return None, Fail(3, "回调-回撤比过低")

    deep = ratio >= RATIO_OK
    floor = s.o[i] if deep else s.c[i]
    support_ok = pull_low >= floor - 1e-9
    if not support_ok:
        return None, Fail(3, "回调-失守关键支撑")

    big_yin = 0
    consec = 0
    max_consec = 0
    for t in range(first, end + 1):
        drop = (s.c[t] / s.c[t - 1] - 1) * 100 if s.c[t - 1] else 0.0
        if drop <= -DROP_VETO_PCT:
            return None, Fail(3, "回调-单日暴跌")
        if s.c[t] < s.o[t] and drop <= -BIG_YIN_PCT:
            big_yin += 1
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
    if max_consec >= 2:
        return None, Fail(3, "回调-连续大阴线")

    shrink_first = 1 - s.v[first] / s.v[k] if s.v[k] else 0.0
    avg_ratio = (sum(s.v[first: end + 1]) / (end - first + 1)) / s.v[k] if s.v[k] else 0.0

    # 止跌信号：缩量十字星 / 长下影 / 阳包阴 / 阳线反包上影
    stop_signal = "无"
    last_rng = s.h[end] - s.l[end]
    if last_rng > 0:
        lower_shadow = (min(s.o[end], s.c[end]) - s.l[end]) / last_rng
        body = abs(s.c[end] - s.o[end]) / last_rng
        if lower_shadow >= 0.45:
            stop_signal = "长下影"
        elif body <= 0.25 and s.v[end] <= s.v[first]:
            stop_signal = "缩量十字星"
    if s.c[end] > s.o[end] and end - 1 >= first and s.c[end] >= s.o[end - 1] and s.c[end - 1] < s.o[end - 1]:
        stop_signal = "阳包阴"
    if end - 1 >= 0 and s.c[end] > s.o[end] and s.c[end] > s.h[end - 1]:
        stop_signal = "阳线反包上影"

    return {
        "pull_days": pull_days,
        "pull_low": pull_low,
        "pull_low_date": s.d[pull_low_i],
        "ratio": ratio,
        "deep": deep,
        "support_ok": support_ok,
        "support_price": floor,
        "shrink_first": shrink_first,
        "pull_vol_avg_ratio": avg_ratio,
        "rebounds": rebounds,
        "big_yin": big_yin,
        "stop_signal": stop_signal,
    }, None


def chip_profile(s: Series, end: int, cur: float) -> dict:
    """用日线「成交均价 × 成交量」估算筹码（不是真实筹码分布）。"""
    lo = max(0, end - CHIP_DAYS + 1)
    buckets: dict = {}
    total = 0.0
    for t in range(lo, end + 1):
        vol = s.v[t]
        if vol <= 0 or s.a[t] <= 0:
            continue
        px = s.a[t] / vol                    # 当日成交均价
        if px <= 0:
            continue
        step = 0.05 if px < 5 else 0.2       # 低价股更细分桶
        key = round(px / step)
        bucket_px = key * step
        buckets[bucket_px] = buckets.get(bucket_px, 0.0) + vol
        total += vol
    if total <= 0:
        return {"chip_ok": False, "chip_note": "成交额缺失，筹码不可估算"}

    above = sum(w for px, w in buckets.items() if px > cur * 1.01)
    below = sum(w for px, w in buckets.items() if px < cur * 0.99)
    vwap = sum(px * w for px, w in buckets.items()) / total
    dense_px = max(buckets.items(), key=lambda kv: kv[1])[0]
    avg_w = total / max(1, len(buckets))
    resist = sorted(px for px, w in buckets.items() if px > cur * 1.01 and w >= avg_w * 1.5)
    support = sorted((px for px, w in buckets.items() if px < cur * 0.99 and w >= avg_w * 1.5),
                     reverse=True)
    return {
        "chip_ok": True,
        "trap_ratio": above / total,        # 上方套牢盘占比
        "profit_ratio": below / total,      # 下方获利盘占比
        "vwap": vwap,
        "dense_price": dense_px,
        "cost_gap_pct": (cur / vwap - 1) * 100 if vwap else 0.0,
        "resist_price": resist[0] if resist else None,
        "chip_support": support[0] if support else None,
        "chip_note": f"近 {CHIP_DAYS} 日 成交均价×成交量 估算",
    }


def stage4_stage_chip(s: Series, i: int, k: int, end: int) -> dict:
    """第四阶段：黄金阶梯位置 + 四维筹码（全部是日线代理口径）。"""
    cur = s.c[end]

    # ---------- A. 黄金阶梯（台阶计数代理）----------
    steps = 0
    step_points = []
    lo = max(6, end - STEP_DAYS + 1)
    for t in range(lo, end + 1):
        if t < 1 or s.v[t - 1] <= 0:
            continue
        prev_high = max(s.h[max(0, t - 5): t]) if t > 0 else 0.0
        is_step = (s.c[t] > s.o[t]
                   and s.v[t] >= s.v[t - 1] * STEP_VOL_MULT
                   and s.h[t] > prev_high)
        if is_step:
            steps += 1
            step_points.append(s.d[t])
    step_level = max(STEP_MIN, min(STEP_MAX, steps))
    if step_level >= STEP_VETO:
        step_tier = "高位（第七级以上）"
    elif step_level >= STEP_WARN:
        step_tier = "谨慎（第五级附近）"
    elif step_level in range(STEP_GOOD[0], STEP_GOOD[1] + 1):
        step_tier = "优先（第三级附近）"
    else:
        step_tier = "底部/起步阶梯"

    # ---------- B. 凹间峰（左侧平台高点）----------
    lo_gap = max(0, end - GAP_PEAK_DAYS)
    hi_gap = max(0, end - GAP_PEAK_RECENT)
    seg = s.h[lo_gap: hi_gap + 1]
    gap_peak = max(seg) if seg else None
    gap_peak_i = (lo_gap + seg.index(gap_peak)) if seg else None
    gap_state = "不可判定"
    if gap_peak:
        if cur > gap_peak:
            cross_i = None
            for t in range(end, lo_gap, -1):
                if s.c[t] <= gap_peak:
                    cross_i = t + 1 if t + 1 <= end else end
                    break
            if cross_i is None:
                cross_i = lo_gap
            gap_state = "刚过" if (end - cross_i) <= 10 else "已过"
        elif (gap_peak - cur) / cur * 100 <= GAP_NEAR_PCT:
            gap_state = "接近"
        else:
            gap_state = "未过"

    chip = chip_profile(s, end, cur)
    return {
        "step_level": step_level,
        "step_tier": step_tier,
        "step_points": step_points[-6:],
        "gap_peak": gap_peak,
        "gap_peak_date": s.d[gap_peak_i] if gap_peak_i is not None else None,
        "gap_state": gap_state,
        "gap_crossed": gap_state == "刚过",
        "gap_near": gap_state == "接近",
        **chip,
    }


def stage5_buy(s: Series, i: int, k: int, end: int, s3: dict) -> dict:
    """第五阶段：二次启动买点（深调→阳盖阴 / 浅调→过枪的高量顶）。"""
    gun_high = max(s.h[k], s.h[i])          # 板枪高量顶 = 板日与枪日的最高价
    prev = end - 1
    yang = s.c[end] > s.o[end]
    vol_better = s.v[end] >= s.v[prev] if prev >= 0 else False

    if s3["deep"]:
        buy_type = "阳盖阴"
        if prev >= 0 and s.c[prev] >= s.o[prev]:
            trigger = s.h[prev]             # 前一日阳线：需反包其上影
            cover = yang and s.c[end] >= trigger
        else:
            trigger = max(s.o[prev], s.c[prev]) if prev >= 0 else s.c[end]
            cover = yang and s.c[end] >= trigger
        triggered = bool(cover)
        need = (trigger - s.c[end]) / trigger * 100 if trigger else 0.0
        near = bool(yang and 0 <= need <= 1.0)
        cond = f"收阳且收盘 ≥ {trigger:.2f}（盖住前一日实体/上影）"
    else:
        buy_type = "过枪的高量顶"
        trigger = gun_high
        triggered = bool(s.c[end] > trigger and vol_better)
        near = bool(trigger and 0 <= (trigger - s.c[end]) / trigger <= 0.02)
        cond = f"收盘 > {trigger:.2f} 且量能不缩"

    return {
        "buy_type": buy_type,
        "buy_trigger_price": r2(trigger),
        "buy_triggered": triggered,
        "buy_near": near,
        "buy_cond": cond,
        "buy_vol_better": vol_better,
        "gun_top": r2(gun_high),
    }


def targets(s: Series, i: int, k: int, end: int, s3: dict, buy: float) -> dict:
    """目标位与止损。三个目标都来自真实日线，不是估算出来的。"""
    cur = s.c[end]
    t1 = r2(max(s.h[i], s.h[k]))            # 第一目标：板枪顶部

    # 第二目标：左侧大阴实顶（现价上方最近一根跌幅 ≥5% 大阴线的开盘价）
    t2 = None
    lo = max(1, end - TARGET2_LOOKBACK)
    for t in range(end, lo - 1, -1):
        drop = (s.c[t] / s.c[t - 1] - 1) * 100 if s.c[t - 1] else 0.0
        if drop <= -TARGET2_BIG_YIN and s.c[t] < s.o[t] and s.o[t] > cur:
            t2 = r2(s.o[t])
            break
    if t2 is None:
        t2 = t1

    # 第三目标：500 里高量位置（250 日最大量柱的最高价）；该位置已在下方说明高量顶
    # 早被突破，退化用 250 日最高价，仍不高于现价则不出目标
    lo250 = max(0, end - HIGHVOL_DAYS + 1)
    seg_v = s.v[lo250: end + 1]
    seg_h = s.h[lo250: end + 1]
    t3 = None
    if seg_v:
        mi = lo250 + seg_v.index(max(seg_v))
        if seg_h[mi - lo250] > cur:
            t3 = r2(seg_h[mi - lo250])
    if t3 is None:
        mx = max(seg_h) if seg_h else None
        if mx and mx > cur:
            t3 = r2(mx)

    stop_base = s3["pull_low"]
    stop = r2(stop_base * (1 - STOP_LOSS_PCT))
    return {
        "target1": t1, "target2": t2, "target3": t3,
        "stop": stop, "stop_base": r2(stop_base),
        "buy": r2(buy),
        # 买点已触发时参考买价就是当日收盘；若收盘已越过板枪顶部，目标 1 会落到现价下方，
        # 此时盈亏比必然是负的——照实给，并在界面上写明「目标 1 已在现价下方」，不改口径
        "target1_valid": t1 > buy,
        "risk_pct": (buy - stop) / buy * 100 if buy else 0.0,
        "deep_floor": r2(s.o[i]),
        "reward_pct": (t1 - buy) / buy * 100 if buy else 0.0,
        "rr": ((t1 - buy) / (buy - stop)) if buy > stop else 0.0,
        "invalidate": f"收盘跌破 {stop:.2f}（回调低点下 {STOP_LOSS_PCT * 100:.0f}%）",
    }


# ==========================================================================
# 四、百分制评分（每个维度都给「数据 + 得分 + 扣分原因」，保证可解释）
# ==========================================================================

def grade_of(total: float) -> str:
    for bar, g in GRADE_BANDS:
        if total >= bar:
            return g
    return "D"


def _dim(key: str, items: list) -> dict:
    cap = DIM_MAX[key]
    got = round(sum(min(p, cap) for _, _, p, _ in items), 1)
    got = min(got, cap)
    return {
        "label": DIM_LABEL[key],
        "max": cap,
        "score": round(got, 1),
        "items": [{"name": name, "data": data, "points": round(p, 1), "note": note}
                  for name, data, p, note in items],
    }


def score_all(ctx: dict) -> dict:
    """返回 {total, grade, dims, veto, notes}。dims 的键见 DIM_LABEL。"""
    s1, s2, s3, s4, s5 = ctx["s1"], ctx["s2"], ctx["s3"], ctx["s4"], ctx["s5"]
    dims: dict = {}

    # ---- D1 首板质量与涨停强度（15）----
    items = [("真实涨停（收盘=涨停价）", f"{ctx['board_date']} 收盘 {s1['limit_price']:.2f}", 5.0,
              "" if s1["is_first_board"] else "非首板")]
    streak = s1["board_streak"]
    items.append(("首板属性", f"{streak} 连板" + ("（首板）" if streak == 1 else "（非首板）"),
                  4.0 if streak == 1 else (2.5 if streak == 2 else 1.0),
                  "" if streak == 1 else "文档优先首板，连板高度越高位置风险越大"))
    vm = s1["board_vol_mult"]
    lo, hi = BOARD_SMALL_DOUBLE
    items.append(("量价配合（小倍阳）", f"板日量 = 前一日 {vm:.2f} 倍",
                  4.0 if lo <= vm <= hi else (2.5 if vm <= 5.0 else 1.5),
                  "" if lo <= vm <= hi else "非小倍阳区间（1.5~3.0 倍）"))
    gap = s1["board_gap_pct"]
    items.append(("封板强度（日线代理）", f"开盘涨幅 {gap:+.2f}%，收盘封板",
                  2.0 if gap >= 3 else (1.5 if gap >= 1 else 1.0),
                  "封单额/开板次数/首封时间日线不可得，仅用开盘涨幅代理"))
    dims["board"] = _dim("board", items)

    # ---- D2 第二日放量出枪质量（15）----
    items = [("枪型", f"{s2['gun_type']}（{s2['gun_grade']} 级）",
              5.0 if s2["gun_type"] == "阳枪" else (3.5 if s2["gun_close"] >= ctx["s1_close"] else 2.0),
              "" if s2["gun_type"] == "阳枪" else "阴枪需要更严格的承接确认")]
    gm = s2["gun_vol_mult"]
    items.append(("量能（对板日）",
                  f"枪日量 = 板日 {gm:.2f} 倍 / 前 5 日均量 {s2['gun_vol_vs_ma5']:.2f} 倍",
                  4.0 if gm >= 1.5 else (3.0 if gm >= 1.2 else 2.0), ""))
    cp = s2["gun_close_pos"]
    items.append(("收盘位置",
                  f"收盘位于当日振幅 {cp * 100:.0f}% 处，上影占振幅 {s2['gun_shadow_ratio'] * 100:.0f}%",
                  4.0 if cp >= 0.7 else (2.5 if cp >= 0.5 else 1.0),
                  "" if cp >= 0.7 else "收盘位置偏低，冲高回落痕迹"))
    r250, r60 = s2["gun_vol_rank250"], s2["gun_vol_rank60"]
    items.append(("高量柱属性", f"近 250 日量能第 {r250} 名 / 近 60 日第 {r60} 名",
                  2.0 if r250 <= 3 else (1.5 if r60 <= 3 else (1.0 if r60 <= 10 else 0.5)),
                  "「500 里高量」按 250 日量能前 3 代理"))
    dims["gun"] = _dim("gun", items)

    # ---- D3 第三日缩量回调质量（20）----
    sh = s3["shrink_first"]
    items = [("回调首日缩量", f"较枪日缩量 {sh * 100:.0f}%（要求 ≥ 33%）",
              8.0 if sh >= 0.5 else (6.5 if sh >= 0.4 else 5.0), "")]
    items.append(("量能递减趋势",
                  f"回调期均量 / 枪日量 = {s3['pull_vol_avg_ratio']:.2f}，放量反弹 {s3['rebounds']} 次",
                  5.0 if s3["rebounds"] == 0 else 3.5, ""))
    pd_ = s3["pull_days"]
    items.append(("回调时间", f"{pd_} 个交易日（上限 {MAX_PULLBACK_DAYS}）",
                  4.0 if pd_ <= 3 else (3.0 if pd_ == 4 else 2.0), ""))
    items.append(("回调 K 线形态", f"大阴线 {s3['big_yin']} 根，止跌信号：{s3['stop_signal']}",
                  3.0 if s3["big_yin"] == 0 and s3["stop_signal"] != "无" else
                  (2.0 if s3["big_yin"] == 0 else 1.0),
                  "" if s3["big_yin"] == 0 else "回调期出现大阴线"))
    dims["pull"] = _dim("pull", items)

    # ---- D4 回调支撑及底部结构（15）----
    ra = s3["ratio"]
    lo_r, hi_r = RATIO_SWEET
    if ra < RATIO_OK:
        pts = 3.0                      # 浅调变体：回撤不足一半
    elif lo_r <= ra <= hi_r:
        pts = 6.0
    elif ra <= RATIO_WEAK:
        pts = 4.5
    else:
        pts = 2.0
    items = [("回撤比", f"(枪顶-回调低点)/(枪顶-板日开盘) = {ra:.2f}", pts,
              "" if ra >= RATIO_OK else "回撤不足上涨幅度一半（浅调变体，文档要求 ≥0.50）")]
    items.append(("关键支撑", f"回调低点 {s3['pull_low']:.2f} vs 支撑 {s3['support_price']:.2f}"
                              f"（{'深调守板日开盘' if s3['deep'] else '浅调守板日收盘'}）",
                  5.0, ""))
    ss = s3["stop_signal"]
    items.append(("止跌信号", ss, 4.0 if ss in ("阳包阴", "阳线反包上影") else
                  (3.0 if ss in ("长下影", "缩量十字星") else 1.5),
                  "" if ss != "无" else "尚未出现明确止跌信号"))
    dims["support"] = _dim("support", items)

    # ---- D5 黄金阶梯及凹间峰位置（10）----
    lv = s4["step_level"]
    if STEP_GOOD[0] <= lv <= STEP_GOOD[1]:
        pts = 6.0            # 第三级附近：最优换挡位
    elif lv == 1:
        pts = 4.5            # 刚起步，需高量打底确认
    elif lv < STEP_VETO:
        pts = 3.5            # 第四~六级：谨慎
    else:
        pts = 2.0            # 第七级以上：高位接力风险
    items = [("黄金阶梯（台阶计数代理）", f"近 {STEP_DAYS} 日台阶 {lv} 级 · {s4['step_tier']}", pts,
              "" if STEP_GOOD[0] <= lv <= STEP_GOOD[1] else f"{s4['step_tier']}，非最优阶梯位置")]
    gs = s4.get("gap_state", "未过")
    gp = s4.get("gap_peak")
    if gp:
        if gs == "刚过":
            items.append(("凹间峰", f"近 10 日内刚上穿左侧平台高点 {gp:.2f}", 4.0, ""))
        elif gs == "已过":
            items.append(("凹间峰", f"已在左侧平台高点 {gp:.2f} 上方运行", 2.5,
                          "突破已不是新鲜事，不算「刚过凹间峰」"))
        elif gs == "接近":
            items.append(("凹间峰", f"距左侧平台高点 {gp:.2f} 不足 {GAP_NEAR_PCT:.0f}%", 2.5,
                          "尚未有效突破"))
        else:
            items.append(("凹间峰", f"左侧平台高点 {gp:.2f}，当前仍有距离", 1.0, "未突破凹间峰"))
    else:
        items.append(("凹间峰", "窗口内无有效左侧高点", 1.0, "不可判定"))
    dims["step"] = _dim("step", items)

    # ---- D6 四维筹码及上方压力（10）----
    if s4.get("chip_ok"):
        tr = s4["trap_ratio"]
        resist = s4.get("resist_price")
        items = [("上方套牢盘",
                  (f"现价上方筹码占比 {tr * 100:.0f}%（压力位 {resist:.2f}）" if resist
                   else f"现价上方筹码占比 {tr * 100:.0f}%"),
                  6.0 if tr < 0.35 else (4.5 if tr < 0.55 else (3.0 if tr < 0.70 else 1.5)),
                  "" if tr < 0.55 else "上方套牢盘偏重，突破难度提高")]
        pr = s4["profit_ratio"]
        items.append(("下方获利盘",
                      f"下方筹码占比 {pr * 100:.0f}%，估算成本 {s4['vwap']:.2f}"
                      f"（现价偏离 {s4['cost_gap_pct']:+.1f}%）",
                      4.0 if 0.40 <= pr <= 0.70 else (3.0 if pr <= 0.85 else 2.0),
                      "" if 0.40 <= pr <= 0.70 else "获利盘集中，存在兑现压力"))
    else:
        items = [("上方套牢盘", "成交额缺失", 3.0, "不可估算"),
                 ("下方获利盘", "成交额缺失", 2.0, "不可估算")]
    dims["chip"] = _dim("chip", items)

    # ---- D7 板块热度及市场人气（5）----
    sec = ctx.get("sector") or {}
    rank = sec.get("rank")
    if rank:
        pts = 5.0 if rank <= 10 else (3.5 if rank <= 30 else (2.5 if rank <= 80 else 1.0))
        items = [("板块排名",
                  f"{sec.get('name') or '未分类'} 当日板块涨幅榜第 {rank} 名"
                  + (f" / 共 {sec.get('total')} 个" if sec.get("total") else "")
                  + (f"（5 日 {sec.get('ret5'):+.2f}%）" if sec.get("ret5") is not None else ""),
                  pts, "" if rank <= 30 else "板块不在前列")]
    else:
        items = [("板块排名", f"所属 {sec.get('name')}，但当日板块强度不可得" if sec.get("name")
                  else "板块数据不可得", 1.0,
                  "板块不热，按最低分处理" if sec.get("name") else "板块热度不可判定")]
    dims["sector"] = _dim("sector", items)

    # ---- D8 分时资金及二次启动信号（10）----
    if s5["buy_triggered"]:
        pts = 6.0
    elif s5["buy_near"]:
        pts = 3.0
    else:
        pts = 1.5
    items = [("二次启动信号", f"{s5['buy_type']}：{s5['buy_cond']}", pts,
              "" if s5["buy_triggered"] else "买点条件尚未满足")]
    items.append(("分时资金", "分时/逐笔数据不可得（本地日线无分钟明细）", 2.0,
                  "按中性基准给分，未用日线冒充分时"))
    dims["buy"] = _dim("buy", items)

    total = int(round(sum(d["score"] for d in dims.values())))

    # ---- 一票否决 ----
    veto = []
    if s4["step_level"] >= STEP_VETO and ctx.get("pos", 0) >= POS_MAX:
        veto.append(f"高位接力风险：阶梯 {s4['step_level']} 级且价格分位 {ctx['pos']:.2f}")
    if s3["ratio"] > 0.95:
        veto.append("回撤几乎吞没全部涨幅，结构已弱化")

    # ---- 评级降档（文档 3.3.8 的硬要求，在评分层面体现）----
    grade = grade_of(total)
    notes = []
    if not ctx.get("ratio_ok", True):
        order = ["S", "A", "B", "C", "D"]
        grade = order[min(order.index(grade) + 1, len(order) - 1)]
        notes.append(f"回撤比 {s3['ratio']:.2f} 未达 1/2（文档 3.3.8），评级降一档")
    if veto:
        notes.append("存在一票否决项，评级降到 D")
        grade = "D"
    return {"total": total, "grade": grade, "dims": dims, "veto": veto, "notes": notes}


# ==========================================================================
# 五、位置 / 均线 / 状态机
# ==========================================================================

def position_pct(s: Series, end: int, days: int = POS_DAYS) -> float:
    lo = max(0, end - days + 1)
    hi = max(s.h[lo: end + 1])
    low = min(s.l[lo: end + 1])
    if hi <= low:
        return 0.5
    return (s.c[end] - low) / (hi - low)


def ma_state(s: Series, end: int, pull_low: float) -> dict:
    ma1, ma2 = ma_at(s.c, MA_FAST_1, end), ma_at(s.c, MA_FAST_2, end)
    ma3, ml = ma_at(s.c, MA_FAST_3, end), ma_at(s.c, MA_LONG, end)
    ml_prev = ma_at(s.c, MA_LONG, end - MA_SLOPE_DAYS)
    if None in (ma1, ma2, ma3, ml, ml_prev):
        return {"ok": False, "note": "均线数据不足"}
    ok = (ma1 > ma2 > ma3) and (ml > ml_prev) and (pull_low >= ml)
    return {
        "ok": ok, "ma5": ma1, "ma10": ma2, "ma20": ma3, "ma60": ml,
        "ma60_up": ml > ml_prev, "above_ma60": pull_low >= ml,
        "note": f"MA5 {ma1:.2f} / MA10 {ma2:.2f} / MA20 {ma3:.2f} / MA60 {ml:.2f}"
                f"（{'向上' if ml > ml_prev else '走平或向下'}）",
    }


def state_of(ctx: dict) -> str:
    s3, s5 = ctx["s3"], ctx["s5"]
    if s5["buy_triggered"]:
        return "买入观察"
    if s5["buy_near"]:
        return "买点临近"
    if s3["pull_days"] <= 1:
        return "形态形成中"
    return "缩量回调中"


def build_checks(ctx: dict) -> dict:
    """逐条核对五阶段 + 均线：键 -> {ok, value}，供界面「✓/✗」与 tooltip 实测值。"""
    s1, s2, s3, s5 = ctx["s1"], ctx["s2"], ctx["s3"], ctx["s5"]
    board_high = ctx.get("board_high")
    return {
        "board_limit": {"ok": True,
                        "value": f"涨停价 {s1['limit_price']:.2f}，收盘 {ctx['s1_close']:.2f}"},
        "board_yang": {"ok": True, "value": "收盘 > 开盘"},
        "board_vol": {"ok": True, "value": f"板日量 = 前一日 {s1['board_vol_mult']:.2f} 倍"},
        "gun_vol": {"ok": True,
                    "value": f"枪日量 = 板日 {s2['gun_vol_mult']:.2f} 倍 / 前 5 日均量 "
                             f"{s2['gun_vol_vs_ma5']:.2f} 倍"},
        "gun_high": {"ok": True,
                     "value": f"枪日最高 {s2['gun_high']:.2f} ≥ 板日最高 "
                              f"{(board_high if board_high is not None else s2['gun_high']):.2f}"},
        "gun_shape": {"ok": True,
                      "value": f"{s2['gun_type']}，上影占振幅 {s2['gun_shadow_ratio'] * 100:.0f}%，"
                               f"收盘位置 {s2['gun_close_pos'] * 100:.0f}%"},
        "pull_shrink": {"ok": True, "value": f"首日缩量 {s3['shrink_first'] * 100:.0f}%"},
        "pull_vol": {"ok": True,
                     "value": f"放量反弹 {s3['rebounds']} 次，均量 / 枪日量 "
                              f"{s3['pull_vol_avg_ratio']:.2f}"},
        "pull_support": {"ok": bool(s3["support_ok"]),
                         "value": f"回调低点 {s3['pull_low']:.2f} vs 支撑 {s3['support_price']:.2f}"},
        "pull_ratio": {"ok": s3["ratio"] >= MIN_RATIO,
                       "value": f"回撤比 {s3['ratio']:.2f}"
                                + ("（达标 ≥0.50）" if s3["ratio"] >= RATIO_OK else "（浅调变体）")},
        "pull_days": {"ok": s3["pull_days"] <= MAX_PULLBACK_DAYS,
                      "value": f"{s3['pull_days']} 个交易日"},
        "buy_trigger": {"ok": bool(s5["buy_triggered"]),
                        "value": f"{s5['buy_type']}，触发价 {s5['buy_trigger_price']:.2f}"
                                 f"（{'已触发' if s5['buy_triggered'] else '未触发'}）"},
        "ma": {"ok": bool((ctx.get("ma") or {}).get("ok")),
               "value": (ctx.get("ma") or {}).get("note") or "均线数据不足"},
    }


# ==========================================================================
# 六、主入口：给定板日，产出完整结构
# ==========================================================================

def analyze(s: Series, i_board: int, end: int, code: str, name: str) -> tuple:
    """在日线序列上识别以 i_board 为板日、end 为最后可见交易日的板枪结构。

    返回 (ctx, 失败原因)。ctx 里 s1~s5 对应五阶段，score 由 score_all() 单独算。
    """
    n = len(s)
    if i_board < 1 or end >= n or end <= i_board:
        return None, str(Fail(1, "数据不足"))
    if end - i_board > BOARD_LOOKBACK + MAX_PULLBACK_DAYS:
        return None, str(Fail(1, "板日过期"))

    rate = limit_rate_of(code, name)
    s1, fail = stage1_board(s, i_board, rate)
    if not s1:
        return None, str(fail)

    k = i_board + 1
    if k > end:
        # 板日刚成立，枪日未出：只作为「形态形成中」的极早期形态
        return {
            "code": code, "name": name,
            "board_i": i_board, "gun_i": None, "signal_i": end,
            "board_date": s.d[i_board], "gun_date": None, "signal_date": s.d[end],
            "s1": s1, "s2": None, "s3": None, "s4": None, "s5": None,
            "early": True, "state": "形态形成中",
        }, ""

    s2, fail = stage2_gun(s, i_board, k, s1)
    if not s2:
        return None, str(fail)

    s3, fail = stage3_pullback(s, i_board, k, end, s1, s2)
    if not s3:
        return None, str(fail)

    s4 = stage4_stage_chip(s, i_board, k, end)
    s5 = stage5_buy(s, i_board, k, end, s3)
    buy = s.c[end] if s5["buy_triggered"] else s5["buy_trigger_price"]
    tg = targets(s, i_board, k, end, s3, buy)
    ctx = {
        "code": code, "name": name,
        "board_i": i_board, "gun_i": k, "signal_i": end,
        "board_date": s.d[i_board], "gun_date": s.d[k], "signal_date": s.d[end],
        "close": s.c[end], "pct": (s.c[end] / s.c[end - 1] - 1) * 100 if end >= 1 and s.c[end - 1] else 0.0,
        "board_high": s.h[i_board],
        "s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5,
        "s1_close": s.c[i_board],
        "buy_price": r2(buy),
        "early": False,
        "ratio_ok": s3["ratio"] >= RATIO_OK,
        "tg": tg,
        "pos": position_pct(s, end),
        "ma": ma_state(s, end, s3["pull_low"]),
        "state": "缩量回调中",
    }
    ctx["state"] = state_of(ctx)
    ctx["checks"] = build_checks(ctx)
    return ctx, ""


def scan_one(s: Series, end: int, code: str, name: str) -> tuple:
    """一只股票在 end 日的主判定：遍历最近板日，取最先成立（最新鲜）的一条结构。"""
    rate = limit_rate_of(code, name)
    reason_best = ""
    for i in board_days(s, end, rate):
        ctx, reason = analyze(s, i, end, code, name)
        if ctx:
            return ctx, ""
        if not reason_best:
            reason_best = reason
    return None, reason_best or str(Fail(1, "板日-最近 5 日无涨停"))


def funnel_key(reason: str) -> str:
    """把 '2|枪日-未放量（低于板日量）' 归并成 '枪日-未放量'，用于漏斗统计。"""
    text = (reason or "").split("|", 1)[-1]
    for sep in ("（", "("):
        text = text.split(sep)[0]
    return text.strip() or "未知"
