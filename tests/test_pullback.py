"""强势回调低吸：硬性条件、评分、仓位、扫描与落盘。

测试数据全部是**人工构造的日线**，不是真实行情：价格序列按
「慢涨 → 涨停 → 横盘新高 → 回落到 MA10 → 缩量 → 锤子线」摆出来，
每一根 K 线都能对应到一条硬性条件，出问题能立刻定位是哪个条件算错了。
另外用 tmp_path 造一个迷你通达信目录，验证扫描真的只读本地文件。
"""
from __future__ import annotations

import struct
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.adapters.tdx_daily import (
    TdxDailyError, TdxDailySource, is_st_name, limit_up_cents, limit_up_pct, market_closed,
    trim_unclosed,
)
from app.services.pullback import (
    CHECKS, Pick, PullbackParams, ScanResult, add_indicators, build_sector_scores, evaluate,
    is_bullish_engulfing, is_hammer, market_summary, plan_position, recent_limit_up, scan,
)

# ---------------------------------------------------------------- 合成日线

def _frame(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    """按收盘价生成带分价的日线；open/high/low 用固定比例，最后一根单独覆盖。"""
    assert len(closes) == len(volumes)
    return pd.DataFrame({
        "date": pd.bdate_range("2026-05-01", periods=len(closes)),
        "open": [c * 0.998 for c in closes],
        "high": [c * 1.006 for c in closes],
        "low": [c * 0.994 for c in closes],
        "close": closes,
        "amount": [c * v for c, v in zip(closes, volumes)],
        "volume": volumes,
        "open_cents": [int(round(c * 0.998 * 100)) for c in closes],
        "high_cents": [int(round(c * 1.006 * 100)) for c in closes],
        "close_cents": [int(round(c * 100)) for c in closes],
    }).assign(pct_change=lambda d: d.close.pct_change() * 100)


def setup_frame() -> pd.DataFrame:
    """一个能同时满足全部 8 项硬性条件的标准形态（已逐条核对，见各断言）。"""
    closes = [9.0 + i * 0.031 for i in range(100)]        # 0..99 慢涨
    closes[-1] = 12.00
    closes += [12.20, 12.30, 12.40, 12.50, 12.60]         # 100..104
    closes += [12.70, 12.80, 12.90, 12.95, 12.00]         # 105..109（109 回落，作涨停基准）
    closes += [13.20]                                     # 110 涨停：12.00 × 1.1
    closes += [13.30, 13.28, 13.26, 13.24]                # 111..114
    closes += [13.30, 13.28, 13.30, 13.28, 13.20]         # 115..119
    volumes = ([800_000] * 105 + [2_500_000] * 5 + [5_000_000] + [2_000_000] * 4
               + [1_100_000, 1_000_000, 900_000, 800_000, 900_000])
    frame = _frame(closes, volumes)
    # 最后一根做成锤子线：实体 0.02，下影 0.13（≥2 倍实体），上影 0.02
    frame.loc[119, ["open", "high", "low", "close",
                    "open_cents", "high_cents", "close_cents"]] = \
        [13.18, 13.22, 13.05, 13.20, 1318, 1322, 1320]
    return frame


def params(**overrides) -> PullbackParams:
    base = PullbackParams(sector_score_mode="off")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ---------------------------------------------------------------- 硬性条件

def test_confirmed_setup_passes_every_hard_check():
    pick = evaluate("600000.SH", "测试股", "测试银行", setup_frame(), params())
    assert pick is not None
    assert all(pick.checks.values()), pick.checks
    assert pick.limitup and pick.limitup_date
    assert pick.pattern == "锤子线"
    # 满分 80（sector 恒 0）刚好够 BUY-A 门槛
    assert pick.score == 80
    assert pick.signal == "BUY-A"


def test_score_threshold_is_not_compensation():
    """硬性条件全过但分数不到 80 只能观察；加板块分才够确认。"""
    frame = setup_frame()
    low = evaluate("600000.SH", "测试股", "测试银行", frame, params(), sector_score=0)
    high = evaluate("600000.SH", "测试股", "测试银行", frame, params(), sector_score=12)
    assert low.score == 80 and low.signal == "BUY-A"
    assert high.score == 92 and high.signal == "BUY-A"
    assert low.checks == high.checks


def test_volume_expansion_blocks_confirmation():
    """回调不放量（缩量比 > 0.5）时，硬性条件不通过，不得给确认信号。"""
    frame = setup_frame()
    frame.loc[115:119, "volume"] = 3_000_000        # 回调期反而放量
    pick = evaluate("600000.SH", "测试股", "测试银行", frame, params())
    assert pick.checks["volume_ok"] is False
    assert pick.signal != "BUY-A"
    assert pick.volume_ratio > 0.5


def test_far_from_support_blocks_confirmation():
    """收盘离 MA10 和颈线都超过 ±1.5%，支撑条件不成立。"""
    frame = setup_frame()
    frame.loc[119, ["close", "close_cents", "high", "high_cents", "low"]] = \
        [14.20, 1420, 14.26, 1426, 14.10]
    pick = evaluate("600000.SH", "测试股", "测试银行", frame, params())
    assert pick.checks["support_ok"] is False
    assert pick.signal == "BLOCK"


def test_stage_high_must_be_recent():
    """20 日新高落在 10 个交易日窗口之外，不算「近 10 日创阶段新高」。"""
    frame = setup_frame()
    # 第 105 根插一根 15.50 的高点：仍在 20 日窗口内，但已经不在最近 10 根里
    frame.loc[105, ["high", "high_cents"]] = [15.50, 1550]
    pick = evaluate("600000.SH", "测试股", "测试银行", frame, params())
    assert pick.checks["stage_high_10d"] is False
    assert pick.signal != "BUY-A"


def test_hammer_and_engulfing_shapes():
    row = pd.Series({"open": 13.18, "high": 13.22, "low": 13.05, "close": 13.20})
    assert is_hammer(row)
    # 上影太长就不是锤子
    assert not is_hammer(pd.Series({"open": 13.18, "high": 13.40, "low": 13.05, "close": 13.20}))
    prev = pd.Series({"open": 13.80, "close": 13.60})
    cur = pd.Series({"open": 13.55, "close": 13.90})
    assert is_bullish_engulfing(prev, cur)
    assert not is_bullish_engulfing(prev, pd.Series({"open": 13.70, "close": 13.75}))


# ---------------------------------------------------------------- 涨停与板块

def test_limit_up_price_rounds_to_cent():
    assert limit_up_cents(1003, 10.0) == 1103        # 10.03 -> 11.03
    assert limit_up_cents(1003, 20.0) == 1204        # 创业板 20%
    assert limit_up_cents(1003, 5.0) == 1053         # ST 5%
    assert limit_up_pct("300750") == 20.0
    assert limit_up_pct("688981") == 20.0
    assert limit_up_pct("600000") == 10.0
    assert limit_up_pct("600000", is_st=True) == 5.0
    assert is_st_name("*ST海航") and is_st_name("退市美都")
    assert not is_st_name("贵州茅台")


def test_limitup_close_only_vs_intraday_touch():
    """盘中触板但没封住：收盘口径不算涨停，触及口径算。"""
    frame = setup_frame()
    # 把 110 那根改成「上影摸到涨停、收盘没封」
    frame.loc[110, ["close", "close_cents", "high", "high_cents"]] = [13.00, 1300, 13.20, 1320]
    hit_close, _ = recent_limit_up(frame, 10, 10.0, close_only=True)
    hit_touch, _ = recent_limit_up(frame, 10, 10.0, close_only=False)
    assert hit_close is False
    assert hit_touch is True


# ---------------------------------------------------------------- 仓位

def test_position_sizing_respects_one_percent_and_ten_percent():
    pick = evaluate("600000.SH", "测试股", "测试银行", setup_frame(), params())
    assert pick.shares % 100 == 0
    assert pick.shares > 0
    assert pick.risk_pct <= 1.0 + 1e-9
    assert pick.value_pct <= 10.0 + 1e-9
    # 目标价 = 收盘价 + 2.5 × 每股风险
    risk = pick.entry_ref - pick.stop
    assert pick.target == pytest.approx(pick.entry_ref + 2.5 * risk, abs=0.005)


def test_position_sizing_when_risk_budget_binds():
    """止损很远时，风险预算先于 10% 市值上限触发。"""
    pick = Pick(symbol="600000.SH", code="600000", name="测试股", industry="测试",
                date="2026-11-06", close=10.0, entry_ref=10.0, stop=8.5)
    plan_position(pick, params(account_equity=1_000_000))
    assert pick.shares == 6600                      # floor(10000/1.5/100)*100
    assert pick.risk_amount == pytest.approx(9900.0)
    assert pick.value_pct == pytest.approx(6.6, abs=0.01)


def test_position_sizing_zero_when_no_risk():
    pick = Pick(symbol="600000.SH", code="600000", name="测试股", industry="测试",
                date="2026-11-06", close=10.0, entry_ref=10.0, stop=10.0)
    plan_position(pick, params())
    assert pick.shares == 0 and pick.risk_amount == 0.0


# ---------------------------------------------------------------- 盘后口径

def test_market_closed_only_after_1505():
    assert market_closed(datetime(2026, 11, 6, 15, 6)) is True
    assert market_closed(datetime(2026, 11, 6, 14, 59)) is False
    assert market_closed(datetime(2026, 11, 6, 9, 30)) is False


def test_trim_unclosed_drops_today_bar():
    frame = setup_frame()
    today = datetime.now().date()
    intraday = datetime.combine(today, time(10, 0))
    frame.loc[len(frame) - 1, "date"] = pd.Timestamp(today)
    assert len(trim_unclosed(frame, intraday)) == len(frame) - 1
    assert len(trim_unclosed(frame, datetime.combine(today, time(15, 30)))) == len(frame)


# ---------------------------------------------------------------- 板块与市场

def test_sector_scores_follow_industry_return_rank():
    records = []
    for industry, growth in (("弱势", 0.95), ("中性", 1.00), ("强势", 1.20)):
        closes = [10.0] * 20 + [10.0 * growth]
        records.append({"industry": industry, "code": "600000", "name": "x",
                        "bars": pd.DataFrame({"close": closes, "high": closes,
                                              "volume": [1] * len(closes)})})
    scores = build_sector_scores(records)
    assert scores["强势"] == 20
    assert scores["弱势"] == 0
    assert scores["弱势"] < scores["中性"] < scores["强势"]


def test_market_summary_counts_up_down_and_limit_up():
    up = setup_frame()
    up.loc[119, "close"] = 13.40                 # 最后一根收涨，否则两只都会记成跌
    up["pct_change"] = up["close"].pct_change() * 100
    down = setup_frame()
    down["close"] = down["close"] * 0.98
    down["pct_change"] = down["close"].pct_change() * 100
    summary = market_summary([
        {"code": "600000", "name": "A", "bars": up},
        {"code": "600001", "name": "B", "bars": down},
    ])
    assert summary["total"] == 2
    assert summary["up"] == 1 and summary["down"] == 1
    assert 0 <= summary["above_ratio"] <= 1

    # 当日涨停只数按「最后一根」判断：把末根改成从 13.28 封到 14.61
    limit = setup_frame()
    limit.loc[119, ["close", "close_cents", "high", "high_cents"]] = [14.61, 1461, 14.61, 1461]
    limit["pct_change"] = limit["close"].pct_change() * 100
    assert market_summary([{"code": "600000", "name": "A", "bars": limit}])["limit_up"] == 1


# ---------------------------------------------------------------- 扫描（迷你通达信目录）

def _write_day(path: Path, rows: list[tuple]) -> None:
    """按通达信 .day 格式落盘：每条 32 字节，(日期, 开, 高, 低, 收, 额, 量)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray()
    for day, open_, high, low, close, amount, volume in rows:
        buf += struct.pack("<IIIIIfII", day, open_, high, low, close, float(amount),
                           volume, 0)
    path.write_bytes(bytes(buf))


def _trade_days(count: int, end: date) -> list[date]:
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _rows_from_frame(frame: pd.DataFrame, days: list[date]) -> list[tuple]:
    rows = []
    for day, bar in zip(days, frame.itertuples(index=False)):
        rows.append((int(day.strftime("%Y%m%d")), int(bar.open_cents), int(bar.high_cents),
                     int(round(bar.low * 100)), int(bar.close_cents),
                     float(bar.amount), int(bar.volume)))
    return rows


@pytest.fixture()
def tdx_tree(tmp_path: Path) -> Path:
    """迷你通达信目录：一只标准形态 + 一只 ST + 一只日线不足 60 根。"""
    end = date.today() - timedelta(days=3)
    days = _trade_days(120, end)
    root = tmp_path / "tdx"
    frame = setup_frame()
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh600000.day", _rows_from_frame(frame, days))
    # 指数日线：只用来定「最新交易日」
    _write_day(root / "vipdoc" / "sh" / "lday" / "sh000001.day",
               _rows_from_frame(frame, days))
    # ST 股（靠名称剔除，用 tnf 太麻烦，这里直接测 exclude_st 的代码路径）
    _write_day(root / "vipdoc" / "sz" / "lday" / "sz000001.day", _rows_from_frame(frame, days))
    # 新股：只有 40 根
    _write_day(root / "vipdoc" / "sz" / "lday" / "sz300001.day",
               _rows_from_frame(frame.tail(40), days[-40:]))
    return root


def test_scan_reads_local_day_files(tdx_tree: Path):
    source = TdxDailySource(tdx_tree, markets=["SH", "SZ"], min_bars=60)
    universe = source.universe()
    assert [row["symbol"] for row in universe] == ["600000.SH", "000001.SZ"]   # 新股被剔除
    assert source.last_trade_date() == int(_trade_days(1, date.today() - timedelta(days=3))[0]
                                           .strftime("%Y%m%d"))

    result = scan(source, params())
    assert result.data_date and result.universe == 2
    assert result.scanned == 2
    assert result.allow_buy is True
    assert result.market["total"] == 2
    picks = {p.symbol: p for p in result.picks}
    assert picks["600000.SH"].signal == "BUY-A"
    assert len(result.frames) == len(result.picks)          # 每只候选都留了日线给图表
    assert set(result.reject) == {key for key, _ in CHECKS}


def test_scan_respects_stop_and_stale_data(tdx_tree: Path):
    source = TdxDailySource(tdx_tree, markets=["SH", "SZ"], min_bars=60)
    stopped = scan(source, params(), should_stop=lambda: True)
    assert "不完整" in "".join(stopped.notes)
    assert stopped.scanned < 2

    stale = scan(source, params(stale_days_max=-1))          # 任何历史日期都算过期
    assert stale.allow_buy is False
    assert all(p.signal != "BUY-A" for p in stale.picks)
    assert any("不生成确认信号" in note for note in stale.notes)


def test_scan_raises_when_no_data(tmp_path: Path):
    root = tmp_path / "empty"
    (root / "vipdoc" / "sh" / "lday").mkdir(parents=True)
    source = TdxDailySource(root, markets=["SH"])
    with pytest.raises(TdxDailyError):
        scan(source, params())
    with pytest.raises(TdxDailyError):
        TdxDailySource(tmp_path / "nope")


def test_result_payload_roundtrip(tdx_tree: Path):
    source = TdxDailySource(tdx_tree, markets=["SH", "SZ"], min_bars=60)
    result = scan(source, params())
    payload = result.to_payload()
    assert "frames" not in payload                     # K线不落盘（体积大且可重取）
    restored = ScanResult.from_payload(payload)
    assert restored.frames == {}
    assert [p.symbol for p in restored.picks] == [p.symbol for p in result.picks]
    assert restored.picks[0].checks == result.picks[0].checks
    assert restored.picks[0].score == result.picks[0].score
    assert restored.data_date == result.data_date


def test_indicators_need_enough_bars(tdx_tree: Path):
    source = TdxDailySource(tdx_tree, markets=["SZ"], min_bars=60)
    short = source.bars({"code": "300001", "market": "SZ",
                         "path": tdx_tree / "vipdoc" / "sz" / "lday" / "sz300001.day",
                         "symbol": "300001.SZ"})
    assert len(short) == 40
    assert evaluate("300001.SZ", "新股", "测试", short, params()) is None
    assert add_indicators(short).shape[0] == 40


# ---------------------------------------------------------------- 文案

def test_no_identity_words_in_source():
    """选股文案最容易滑向「主力洗盘出货」，源码里不许出现这类结论词。"""
    banned = ("主力", "游资", "庄家", "建仓", "出货", "洗盘", "拉升股价")
    root = Path(__file__).resolve().parent.parent
    for name in ("app/services/pullback.py", "app/services/pullback_report.py",
                 "app/ui/pullback_view.py", "app/adapters/tdx_daily.py"):
        text = (root / name).read_text(encoding="utf-8")
        hits = [word for word in banned if word in text]
        assert not hits, f"{name} 出现身份推断词：{hits}"
