"""涨停四阶段选股：涨停价口径、四阶段判定、门控评分、精选、计划与状态机。

测试数据全部是**人工构造的日线或快照**，不是真实行情。每条断言都对应
`references/strategy.md` 里的一条规则，失败时能直接定位到是哪条算错：

  * 涨停价必须「四舍五入到分」，不能用 `round()`（银行家舍入会把 0.125 变成 0.12）
  * 首板 = 收盘涨停 **且** 前 20 日无涨停，缺一不可
  * 三倍量确认的每一条剔除原因都要能复述给用户
  * 承接 A/B/C/D 四类的分界值
  * 二次启动七项检查，任一不满足都不能说"确认"
  * 评分分母随阶段变化（S1/S2 分母 55、S3 75、S4 100），未发生的阶段不计分
  * 精选不凑数：容量满了就停，淘汰的不进
  * 状态机同日重跑必须幂等，`first_board_*` 不能被覆盖

另外扫描全部源码，禁止出现「主力 / 游资 / 庄家 / 建仓 / 出货 / 洗盘」这类
资金身份推断词——本模块只有日线，没有任何字段能支撑这种结论。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.adapters.tdx_daily import (
    asof, board_of, in_limitup_scope, limit_prices, limit_rate_of, round_half_up,
)
from app.db.repo import Repo
from app.db.session import Database
from app.services.limitup import (
    CAPACITY, DAILY_FIELDS, DIM_LABEL, INTRADAY_FIELDS, LIMITUP_CHECKS, STAGE_VERIFIED,
    WEIGHTS, build_plan, build_snapshot, data_completeness, evaluate_carry,
    evaluate_second_launch, grade_of, limitup_checklist, quality_grade, reason_text,
    review_returns, score, select_final, stage1_filter, stage2_confirm,
)
from app.services.limitup_pool import LimitupPool, REVIEW_CAVEAT, review_stats

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "app" / "db" / "schema.sql"
BANNED = ("主力", "游资", "庄家", "建仓", "出货", "洗盘")


# ==================================================================== 合成数据

def _dates(n: int, start: date = date(2026, 5, 4)) -> list[int]:
    """n 个工作日（只当整数比较用，不参与交易日历）。"""
    out: list[int] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(int(day.strftime("%Y%m%d")))
        day += timedelta(days=1)
    return out


def _bars(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    """按收盘价铺日线：平开、最高=收盘、最低略低于收盘、额=价×量。"""
    volumes = volumes or [1_000_000.0] * len(closes)
    dates = _dates(len(closes))
    out: list[dict] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        open_ = prev
        high = max(open_, close)
        low = round(min(open_, close) * 0.998, 2)
        volume = float(volumes[i])
        out.append({
            "date": dates[i], "open": open_, "high": high, "low": low, "close": close,
            "amount": close * volume, "volume": volume,
            "pct": (close / prev - 1) * 100 if prev else 0.0, "pre_close": prev,
        })
        prev = close
    return out


def _patch(bars: list[dict], index: int, **fields) -> list[dict]:
    """覆盖某一根的价格/量，用来精确构造涨停、缩量、突破等形态。"""
    bars[index].update(fields)
    if "close" in fields and "pre_close" in bars[index]:
        prev = bars[index]["pre_close"]
        bars[index]["pct"] = (fields["close"] / prev - 1) * 100 if prev else 0.0
    return bars


def _snap(**over) -> dict:
    """一份自洽的涨停快照（默认值即「华升股份 2026-11-06 首板三倍量」形态）。"""
    base = {
        "date": 20261106, "code": "600156", "symbol": "600156.SH", "name": "华升股份",
        "market": "SH", "board": "沪主板", "sector": "纺织", "sector_tcode": "T01",
        "sector_index": "880367", "sector_change": 1.2, "sector_change_5d": 2.0,
        "sector_amount": 5.0,
        "open": 6.20, "high": 7.07, "low": 6.17, "close": 7.07, "pre_close": 6.43,
        "pct": 9.95, "volume": 40_360_356.0, "amount": 277_372_640.0,
        "upper_limit": 7.07, "lower_limit": 5.79, "limit_rate": 0.10,
        "is_limit_up": True, "touched_limit": True,
        "vol_ratio5": 4.32, "vol_ratio10": 2.50, "amount_ratio5": 3.10,
        "turnover": 10.04, "ma5": 6.43, "ma10": 6.39, "ma20": 6.61, "ma60": 7.81,
        "pct5": 5.0, "pos20": 0.90, "pos60": 0.80, "atr14": 0.355, "vwap": 6.90,
        "upper_shadow": 0.0, "is_first_board": True, "consecutive_boards": 1,
        "first_touch_time": None, "first_seal_time": None, "open_board_count": None,
        "seal_amount": None, "quality": "A", "quality_note": "高开直线封板（日线代理）",
        "data_flags": list(INTRADAY_FIELDS),
    }
    base.update(over)
    return base


# ==================================================================== 涨停价口径

def test_round_half_up_not_bankers():
    """0.125 必须进位到 0.13；内置 round() 会因银行家舍入给出 0.12。"""
    assert round(0.125, 2) == 0.12, "前提变了：Python 的 round 已不是银行家舍入"
    assert round_half_up(0.125, 2) == 0.13
    assert round_half_up(2.675, 2) == 2.68


def test_limit_prices_match_a_share_rule():
    """实测样本：华升股份前收 6.43 → 涨停 7.07 / 跌停 5.79。"""
    assert limit_prices(6.43, 0.10) == (7.07, 5.79)
    assert limit_prices(10.00, 0.05) == (10.50, 9.50)      # ST 主板
    assert limit_prices(10.00, 0.20) == (12.00, 8.00)      # 创业板
    assert limit_prices(0, 0.10) == (0.0, 0.0), "前收为 0 时不能算出价格"


def test_board_and_rate_by_prefix():
    assert board_of("600156") == "沪主板"
    assert board_of("000595") == "深主板"
    assert board_of("300750") == "创业板"
    assert board_of("688981") == "科创板"
    assert board_of("830799") == "北交所"
    assert board_of("302132") == "创业板", "302 是创业板新段（中航成飞），不能归成「其他」"
    assert limit_rate_of("300750") == 0.20
    assert limit_rate_of("302132") == 0.20, "创业板新段同样 20%，按 10% 算涨停价会全错"
    assert limit_rate_of("600156") == 0.10
    assert limit_rate_of("600156", "ST华升") == 0.05, "ST 必须按 5% 算，否则涨停价全错"
    assert in_limitup_scope("600156") and in_limitup_scope("300750")
    assert in_limitup_scope("302132"), "创业板新段属于选股范围"
    assert not in_limitup_scope("688981"), "科创板不在四阶段选股范围"
    assert not in_limitup_scope("830799"), "北交所不在四阶段选股范围"


def test_asof_never_looks_ahead():
    bars = _bars([10.0, 10.1, 10.2, 10.3, 10.4])
    cut = asof(bars, bars[2]["date"])
    assert len(cut) == 3
    assert cut[-1]["close"] == 10.2, "asof 必须截断到目标日（含），不能带出未来 K 线"
    assert len(asof(bars, 99999999)) == 5


# ==================================================================== 快照

def test_snapshot_rejects_short_or_stale_data():
    meta = {"code": "600156", "market": "SH", "symbol": "600156.SH", "name": "华升股份"}
    short = _bars([10.0] * 24)
    assert build_snapshot(meta, short, short[-1]["date"], None) is None, "不足 25 根不出快照"

    bars = _bars([10.0] * 30)
    assert build_snapshot(meta, bars, 20260101, None) is None, "目标日没有 K 线时不能拿最后一根顶替"


def test_snapshot_leaves_intraday_fields_empty():
    meta = {"code": "600156", "market": "SH", "symbol": "600156.SH", "name": "华升股份"}
    closes = [10.0] * 29 + [11.00]          # 前收 10.00 → 涨停 11.00
    bars = _bars(closes)
    _patch(bars, 29, open=10.50, high=11.00, low=10.40, volume=5e6, amount=5.5e7)
    snap = build_snapshot(meta, bars, bars[-1]["date"], {})
    assert snap is not None
    for name in INTRADAY_FIELDS:
        assert snap[name] is None, f"{name} 本地日线拿不到，必须为 None，不能填 0 猜"
    assert snap["is_limit_up"] is True
    assert snap["is_first_board"] is True
    assert snap["upper_limit"] == 11.00
    assert snap["data_flags"] == list(INTRADAY_FIELDS)
    assert snap["vwap"] == round(5.5e7 / 5e6, 3), "vwap 是额/量的全天均价（日线代理）"
    assert snap["intraday_vwap"] is None, "分时均价线与全天均价是两个东西，不能混用"


def test_completeness_caps_at_71_percent():
    """12 个日线字段 / 17 个总字段 = 0.706，置信最高只能 C。"""
    ratio, missing = data_completeness(_snap())
    assert ratio == round(12 / 17, 3) == 0.706
    assert set(missing) == set(INTRADAY_FIELDS)
    assert len(DAILY_FIELDS) == 12


# ==================================================================== S1 首板

def test_stage1_requires_strict_first_board():
    """收盘涨停 + 前 20 日无涨停，两者缺一不可。"""
    first = _snap()
    assert stage1_filter([first]) == [first]

    repeated = _snap(is_first_board=False, consecutive_boards=2)
    assert stage1_filter([repeated]) == [], "20 日内已涨停过的不算首板"

    not_up = _snap(is_limit_up=False, close=6.90, upper_limit=7.07)
    assert stage1_filter([not_up]) == []


def test_first_board_detected_from_real_bars():
    meta = {"code": "600156", "market": "SH", "symbol": "600156.SH", "name": "华升股份"}

    def build(early_index: int | None = None):
        """30 根平盘 10.00，最后一根涨停；early_index 指定窗口内再造一根涨停。"""
        bars = _bars([10.0] * 29 + [11.00])
        if early_index is not None:
            _patch(bars, early_index, open=10.00, high=11.00, low=10.00, close=11.00)
        _patch(bars, 29, open=10.50, high=11.00, low=10.40, volume=5e6, amount=5.5e7)
        return build_snapshot(meta, bars, bars[-1]["date"], {})

    assert build()["is_limit_up"] and build()["is_first_board"]

    # 前 20 日窗口 = 索引 9..28：索引 10 的涨停在窗口内 → 不算首板
    assert build(early_index=10)["is_first_board"] is False, "20 日内已涨停过，不能报首板"
    # 索引 5 在窗口之外 → 仍是严格首板
    assert build(early_index=5)["is_first_board"] is True, "20 日窗口外的涨停不影响首板判定"


def test_quality_grade_is_daily_proxy():
    """日线代理口径：一字板 A、高开直线 A、上影明显 C，且文案必须注明是代理。"""
    assert quality_grade(_snap(open=7.07, high=7.07, low=7.07, close=7.07))[0] == "A"
    assert "日线代理" in quality_grade(_snap())[1]
    grade, note = quality_grade(_snap(open=6.50, high=7.07, low=6.45, close=7.07,
                                      upper_shadow=0.0))
    assert grade == "B" and "日线代理" in note, "小幅高开收在最高价，只能给 B 待分时确认"


# ==================================================================== S2 三倍量

def test_stage2_pass_and_reject_reasons():
    good = _snap()
    passed, rejected = stage2_confirm([good])
    assert passed == [good] and rejected == []

    weak_vol = _snap(code="000001", vol_ratio5=2.4)
    high_vol = _snap(code="000002", pos60=0.97, vol_ratio5=6.8)
    weak_amount = _snap(code="000003", amount_ratio5=1.4)
    faded = _snap(code="000004", close=7.00, open=7.05)
    bad_quality = _snap(code="000005", quality="C")

    passed, rejected = stage2_confirm([weak_vol, high_vol, weak_amount, faded, bad_quality])
    assert passed == [], "五只都不该通过三倍量确认"
    reasons = {r["code"]: "；".join(r["reasons"]) for r in rejected}
    assert "5日量能倍数" in reasons["000001"]
    assert "高位巨量" in reasons["000002"], "60日顶部 + 6 倍量能是典型的见顶形态"
    assert "成交额倍数" in reasons["000003"]
    assert "收盘低于开盘" in reasons["000004"]
    assert "质量 C 级" in reasons["000005"]
    assert len(rejected) == 5


# ==================================================================== S3 次日承接

def _carry_snap(**over) -> dict:
    """承接日快照：默认平开微涨、支撑有效。"""
    base = dict(pre_close=11.00, open=11.05, high=11.50, low=10.90, close=11.30,
                pct=2.73, vwap=11.20, vol_ratio5=1.2, sector_change=0.5, date=20261109)
    base.update(over)
    return _snap(**base)


def test_carry_four_types():
    record = {"first_board_date": 20261106, "first_board_close": 11.00}

    a = evaluate_carry(record, [], _carry_snap())
    assert a["carry_type"] == "A" and a["ok"] is True

    b = evaluate_carry(record, [], _carry_snap(open=10.98, close=10.95, pct=-0.45,
                                              low=10.60, vwap=10.90))
    assert b["carry_type"] == "B", "小跌但没破 95% 支撑，是平开换手不是失败"

    c = evaluate_carry(record, [], _carry_snap(open=10.50, low=10.40, close=11.10,
                                               pct=0.91, vwap=10.90))
    assert c["carry_type"] == "C", "低开 -4.5% 后收复前收，应判低开反包"

    d = evaluate_carry(record, [], _carry_snap(open=10.20, low=9.90, close=9.95,
                                               pct=-9.55, vwap=10.10))
    assert d["carry_type"] == "D" and d["ok"] is False
    assert d["hold_support"] is False


def test_carry_guards():
    record = {"first_board_date": 20261106, "first_board_close": 11.00}
    pending = evaluate_carry(record, [], _carry_snap(date=20261106))
    assert pending["carry_type"] == "D" and "尚未进入承接日" in pending["detail"]

    missing = evaluate_carry(record, [], None)
    assert missing["carry_type"] == "D" and "停牌或数据缺失" in missing["detail"]


# ==================================================================== S4 二次启动

def _launch_bars(cons_days: int = 5, breakout: bool = True, break_volume: float = 5e6,
                 cons_low: float = 11.00) -> tuple[list[dict], dict]:
    """首板 11.00 → 整理 cons_days 日 → 最后一根放量突破。"""
    closes = [10.0] * 25
    bars = _bars(closes + [11.00])
    _patch(bars, -1, open=10.50, high=11.00, low=10.40, volume=6e6, amount=6.6e7)
    for _ in range(cons_days):
        prev = bars[-1]["close"]
        bars.append({"date": bars[-1]["date"] + 1, "open": prev, "high": 11.30,
                     "low": cons_low, "close": 11.10, "volume": 2.5e6,
                     "amount": 2.5e6 * 11.1, "pct": (11.10 / prev - 1) * 100,
                     "pre_close": prev})
    last = bars[-1]
    close = 11.80 if breakout else 11.20
    bars.append({"date": last["date"] + 1, "open": 11.15, "high": close, "low": 11.10,
                 "close": close, "volume": break_volume,
                 "amount": break_volume * close, "pct": (close / last["close"] - 1) * 100,
                 "pre_close": last["close"]})
    record = {"first_board_date": bars[25]["date"], "first_board_close": 11.00,
              "first_board_volume": 6e6}
    return bars, record


def test_second_launch_confirmed():
    bars, record = _launch_bars()
    result = evaluate_second_launch(record, bars, _snap(sector_change_5d=1.0))
    assert result["confirmed"] is True, result["detail"]
    assert all(result["checks"].values()), result["checks"]
    assert result["cons_days"] == 5
    assert result["second_vol_ratio"] == 2.0          # 5e6 / 2.5e6
    assert result["vol_contract"] == round(2.5e6 / 6e6, 2)  # 0.42


def test_second_launch_checks_block_confirm():
    # 未突破
    bars, record = _launch_bars(breakout=False)
    r = evaluate_second_launch(record, bars, _snap())
    assert r["confirmed"] is False and "等待启动" in r["detail"]
    assert r["checks"]["breakout"] is False

    # 突破了但没放量
    bars, record = _launch_bars(break_volume=2.0e6)
    r = evaluate_second_launch(record, bars, _snap())
    assert r["confirmed"] is False and r["checks"]["vol_expansion"] is False

    # 整理期跌破支撑（首板收盘 × 0.93 = 10.23）
    bars, record = _launch_bars(cons_low=10.10)
    r = evaluate_second_launch(record, bars, _snap())
    assert r["checks"]["hold_support"] is False and r["confirmed"] is False

    # 板块退潮（5 日 < -3%）
    bars, record = _launch_bars()
    r = evaluate_second_launch(record, bars, _snap(sector_change_5d=-4.2))
    assert r["checks"]["sector_ok"] is False and r["confirmed"] is False


def test_second_launch_window_and_pending():
    bars, record = _launch_bars(cons_days=11)
    r = evaluate_second_launch(record, bars, _snap())
    assert r["expired"] is True and "超出 10 日跟踪窗口" in r["detail"]

    short, record = _launch_bars(cons_days=1)
    # 只剩首板 + 1 根整理 → 整理期为 0 日
    r = evaluate_second_launch(record, short[:-1], _snap())
    assert r["waiting"] is True and "尚未形成整理区间" in r["detail"]

    assert evaluate_second_launch({}, short, _snap())["waiting"] is True


# ==================================================================== 门控评分

def test_score_denominator_follows_stage():
    snap = _snap()
    s1 = score(snap, "S1")
    assert s1["max_raw"] == sum(WEIGHTS[k] for k in STAGE_VERIFIED["S1"]) == 55.0
    assert s1["unverified"] == ["carry", "launch"]
    assert s1["detail"]["carry"]["points"] == 0.0, "未发生的阶段不给分"
    assert s1["detail"]["launch"]["points"] == 0.0

    carry = {"carry_type": "A", "above_vwap": True}
    launch = {"confirmed": True, "second_vol_ratio": 1.8, "vol_contract": 0.5,
              "checks": {"hold_support": True}}
    s3 = score(snap, "S3", carry=carry)
    assert s3["max_raw"] == 75.0 and s3["detail"]["carry"]["points"] == 20.0
    assert s3["unverified"] == ["launch"]

    s4 = score(snap, "S4", carry=carry, launch=launch)
    assert s4["max_raw"] == 100.0 and set(s4["unverified"]) == set()
    assert s4["raw"] == round(20 + 18 + 20 + 23 + 5, 1)
    assert s4["normalized"] == round(s4["raw"], 1)
    assert s4["grade"] == "A级"


def test_grade_thresholds():
    """≥90 S级 / ≥80 A级 / ≥70 B级 / ≥60 C级 / 其余淘汰，边界值必须落在高档。"""
    assert grade_of(100) == "S级" and grade_of(90) == "S级"
    assert grade_of(89.9) == "A级" and grade_of(80) == "A级"
    assert grade_of(79.9) == "B级" and grade_of(70) == "B级"
    assert grade_of(69.9) == "C级" and grade_of(60) == "C级"
    assert grade_of(59.9) == "淘汰" and grade_of(0) == "淘汰"


def test_score_is_gated_by_what_actually_happened():
    """S1 只算首板+量能+板块；把承接/启动数据塞进去也不能提分。"""
    snap = _snap()
    carry = {"carry_type": "A", "above_vwap": True}
    launch = {"confirmed": True, "second_vol_ratio": 2.5, "vol_contract": 0.4,
              "checks": {"hold_support": True}}
    bare = score(snap, "S1")
    stuffed = score(snap, "S1", carry=carry, launch=launch)
    assert bare["normalized"] == stuffed["normalized"] == 78.2
    assert stuffed["detail"]["carry"]["points"] == 0.0, "S1 阶段承接还没发生，不能给分"
    assert stuffed["confidence"] == "C", "日线口径置信度只能 C"


def test_confidence_follows_completeness():
    """完整度 ≥0.9 A / ≥0.75 B / ≥0.6 C / 其余 D。"""
    full = _snap(**{name: 1.0 for name in INTRADAY_FIELDS})
    assert score(full, "S1")["confidence"] == "A"
    assert score(_snap(), "S1")["confidence"] == "C"


def test_checklist_and_reason_text():
    snap = _snap()
    checks = limitup_checklist(snap, "S2")
    assert set(checks) == set(LIMITUP_CHECKS)
    assert all(checks.values()), "标准形态七项全过"

    text = reason_text(snap, "S2", None, None)
    assert "三倍量确认" in text and "4.32" in text
    for word in BANNED:
        assert word not in text


# ==================================================================== 精选与计划

def test_select_final_respects_capacity_and_never_pads():
    def cand(code: str, stage: str, normalized: float) -> dict:
        return {"code": code, "stage": stage,
                "score": {"normalized": normalized,
                          "grade": "淘汰" if normalized < 60 else "A级"}}

    s1 = [cand(f"60000{i}", "S1", 90 - i) for i in range(8)]
    picked = select_final(s1, limit=10)
    assert len(picked) == CAPACITY["S1"] == 3, "首板阶段容量 3，多出来的不硬塞"
    assert [p["score"]["normalized"] for p in picked] == [90.0, 89.0, 88.0]

    mixed = [cand("600001", "S4", 95), cand("600002", "S3", 90),
             cand("600003", "S2", 85), cand("600004", "S1", 88)]
    picked = select_final(mixed, limit=10)
    assert [p["stage"] for p in picked] == ["S4", "S3", "S2", "S1"], "优先级 S4>S3>S2>S1"

    assert select_final([cand("600005", "S1", 40)], limit=10) == [], "淘汰的不进精选"
    assert select_final([], limit=10) == [], "一只没有就明说，不凑数"

    dup = [cand("600006", "S1", 90), cand("600006", "S2", 89)]
    assert len(select_final(dup, limit=10)) == 1, "同一代码只出现一次"


def test_plan_price_ordering():
    snap = _snap(close=11.00, high=11.00, pre_close=10.50, atr14=0.30, ma5=10.60)
    plan = build_plan(snap, {"first_board_close": 10.00}, None, "S2")
    assert plan["stop"] < plan["support"] < snap["close"] < plan["target1"] < plan["target2"]
    assert plan["support"] == 10.60, "支撑取 max(首板×0.95=9.5, MA5=10.6)"
    assert plan["stop"] == 10.45
    assert plan["risk_per_share"] == round(11.00 - 10.45, 2)
    assert abs(plan["target1"] - (11.00 + 0.55 * 1.5)) <= 0.01, "目标1 = 现价 + 1.5R"
    assert abs(plan["target2"] - (11.00 + 0.55 * 2.5)) <= 0.01, "目标2 = 现价 + 2.5R"
    assert plan["risk_pct"] == round(plan["risk_per_share"] / 11.00 * 100, 2)
    assert "10.45" in plan["invalidation"]


def test_plan_falls_back_when_stop_above_price():
    """支撑被涨破时不能让止损高于现价，退化为 现价 - 1.2×ATR。"""
    snap = _snap(close=10.00, high=10.00, atr14=0.50, ma5=10.00)
    plan = build_plan(snap, {"first_board_close": 12.00}, None, "S1")
    assert plan["stop"] < snap["close"]
    assert plan["stop"] == round(10.00 - 1.2 * 0.50, 2)
    assert plan["target1"] > snap["close"] > plan["stop"]


def test_plan_uses_consolidation_bounds_for_s4():
    snap = _snap(close=11.80, high=11.80, atr14=0.25, ma5=11.20)
    launch = {"cons_low": 10.90, "cons_high": 11.30, "confirmed": True}
    plan = build_plan(snap, {"first_board_close": 11.00}, launch, "S4")
    assert plan["support"] == 11.20
    assert plan["resistance"] == 11.30
    assert "11.30" in plan["break_confirm"]


# ==================================================================== 复盘收益

def test_review_returns_leave_gaps_empty():
    bars = _bars([10.0, 11.0, 11.5, 12.0, 11.8])
    out = review_returns(bars, bars[0]["date"])
    assert out["first_board_close"] == 10.0
    assert out["t1"] == 10.0 and out["t3"] == 20.0
    assert out["t5"] is None and out["t10"] is None, "不足 N 日必须留 None，不能填 0"
    assert review_returns(bars, 19990101) == {}


# ==================================================================== 状态机

@pytest.fixture()
def pool(tmp_path):
    db = Database(tmp_path / "limitup.db", schema_path=SCHEMA)
    db.init_schema()
    return LimitupPool(Repo(db))


def test_pool_state_persists_and_is_idempotent(pool):
    snap = _snap()
    rec = pool.upsert(snap, "S1", score(snap, "S1"), 20261106)
    assert rec["state"] == "S1" and rec["first_board_date"] == 20261106
    assert pool.flush() == 1

    reloaded = LimitupPool(pool.repo)
    assert reloaded.get("600156.SH")["state"] == "S1", "重启后必须从原状态继续"

    # 同日重跑：状态可以升级到 S2，但首板基准不能被覆盖
    snap2 = _snap(vol_ratio5=5.5, close=7.07)
    rec2 = reloaded.upsert(snap2, "S2", score(snap2, "S2"), 20261106)
    assert rec2["state"] == "S2"
    assert rec2["first_board_date"] == 20261106
    assert rec2["first_board_close"] == 7.07

    # 第二天的新一轮首板才重置基准
    later = _snap(date=20261109, close=7.78)
    rec3 = reloaded.upsert(later, "S1", score(later, "S1"), 20261109)
    assert rec3["first_board_date"] == 20261109 and rec3["first_board_close"] == 7.78
    assert [h["state"] for h in rec3["history"]] == ["S1", "S2", "S1"]


def test_pool_transition_and_history(pool):
    snap = _snap()
    pool.upsert(snap, "S1", score(snap, "S1"), 20261106)
    pool.transition("600156.SH", "S3", 20261109, "等待承接")
    rec = pool.get("600156.SH")
    assert rec["state"] == "S3"
    assert rec["history"][-1]["note"] == "等待承接"
    pool.transition("600156.SH", "S3", 20261109, "重复调用")
    assert len(rec["history"]) == 2, "同日同状态重复转移不应再记一条"
    assert pool.transition("999999.SH", "S3", 20261109) is None


def test_pool_retires_stale_records(pool):
    snap = _snap()
    pool.upsert(snap, "S1", score(snap, "S1"), 20261106)
    trade_dates = [20261106, 20261109, 20261110]
    retired = pool.retire_stale(20261110, max_idle_days=12, trade_dates=trade_dates)
    assert retired == ["600156.SH"]
    assert pool.get("600156.SH")["state"] == "S9"
    assert "未获得三倍量确认" in pool.get("600156.SH")["history"][-1]["note"]


def test_pool_summary_text(pool):
    assert pool.summary_text() == "股票池为空"
    snap = _snap()
    pool.upsert(snap, "S2", score(snap, "S2"), 20261106)
    assert "三倍量确认 1" in pool.summary_text()
    assert pool.summary() == {"S2": 1}


def test_review_stats_warns_on_small_sample():
    rows = [{"state_label": "首板发现", "returns": {"t1": 3.0}},
            {"state_label": "首板发现", "returns": {"t1": -1.0}}]
    stats = review_stats(rows)
    assert stats["total"] == 2 and stats["evaluated"] == 2
    assert stats["overall"]["t1"]["win"] == 50.0
    assert stats["overall"]["t1"]["avg"] == 1.0
    assert stats["overall"]["t5"]["n"] == 0
    assert stats["caveat"] == REVIEW_CAVEAT
    assert "T+1" in stats["caveat"], "必须写明 T+1 不能卖这个约束"


# ==================================================================== 合规

def test_no_identity_inference_words():
    """只有日线数据，任何「主力/游资/庄家」结论都是编的。"""
    files = [ROOT / "app" / "services" / name for name in
             ("limitup.py", "limitup_pool.py", "limitup_scanner.py", "limitup_report.py")]
    files.append(ROOT / "app" / "ui" / "limitup_view.py")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for word in BANNED:
            assert word not in text, f"{path.name} 出现禁用词「{word}」"


def test_stage_labels_are_all_mapped():
    from app.services.limitup import STATE_LABEL, STAGE_LABEL, STAGE_TO_STATE
    assert set(STAGE_LABEL) == set(STAGE_VERIFIED) == set(STAGE_TO_STATE)
    assert set(STAGE_TO_STATE.values()) <= set(STATE_LABEL)
    assert set(DIM_LABEL) == set(WEIGHTS)
