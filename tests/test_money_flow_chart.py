"""资金流副图（主力资金）：累计口径、缺日处理与绘制不崩。

覆盖三件事：
1. `_cumulative` 的累计口径——首日之前为空、缺日沿用前值而不是断线或补 0；
2. `set_flow` 把主力净额与超大单净额分开存，副图才能画两条独立的累计线；
3. 有数据 / 无数据 / 没有超大单三条路径都能画完（离屏 grab），不抛异常。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.db.models import Bar
from app.ui.charts import CandleChart, MoneyFlowChart, _cumulative, _fmt_signed, _nice_step


# ---------------------------------------------------------------- 纯函数
def test_cumulative_carries_missing_day_without_breaking():
    """缺日沿用前值：既不补 0（凭空造一天没资金进出），也不断线。"""
    assert _cumulative([None, 100.0, None, 50.0]) == [None, 100.0, 100.0, 150.0]


def test_cumulative_all_missing_stays_empty():
    assert _cumulative([None, None, None]) == [None, None, None]


def test_cumulative_negative_flows_subtract():
    assert _cumulative([-10.0, 4.0, -1.0]) == [-10.0, -6.0, -7.0]


def test_fmt_signed_marks_inflow_only():
    assert _fmt_signed(2.88e8) == "+2.88亿"
    assert _fmt_signed(-3.02e8) == "-3.02亿"
    assert _fmt_signed(0) == "0"


def test_nice_step_is_round_number():
    """刻度步长只允许 1/2/2.5/5×10^n，否则右侧会出现 3.7亿 这种读不动的数。"""
    assert _nice_step(3e8, 5) == 1e8          # 6e7 不取 5e7：宁可略粗，也不留半格
    assert _nice_step(2.4e8, 5) == 5e7
    assert _nice_step(1.0e9, 5) == 2.0e8
    assert _nice_step(0, 5) == 1.0


# ---------------------------------------------------------------- 绘制
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def make_bars(count: int = 6) -> list[Bar]:
    return [
        Bar(symbol="600519.SH", period="1d",
            bar_time=datetime(2026, 11, 2 + i), open=10.0 + i, high=10.5 + i,
            low=9.8 + i, close=10.2 + i, volume=1_000_000)
        for i in range(count)
    ]


def test_set_flow_keeps_main_and_jumbo_separate(qapp):
    chart = MoneyFlowChart()
    chart.set_flow({"2026-11-02": 1.0}, jumbo={"2026-11-02": 0.4})
    assert chart._flow == {"2026-11-02": 1.0}
    assert chart._jumbo == {"2026-11-02": 0.4}


def test_set_flow_without_jumbo_draws_only_main(qapp):
    """没有超大单时不拿主力净额顶替，否则两条线会完全重合、图例骗人。"""
    chart = MoneyFlowChart()
    chart.set_flow({"2026-11-02": 1.0})
    assert chart._jumbo == {}


def test_paint_with_data_does_not_crash(qapp):
    candles = CandleChart()
    candles.set_bars(make_bars(), "日K", "C")
    chart = MoneyFlowChart()
    chart.bind(candles)
    chart.resize(900, 150)
    chart.set_flow(
        {bar.bar_time.strftime("%Y-%m-%d"): (1e7 if i % 2 else -8e6)
         for i, bar in enumerate(make_bars())},
        "stockdb 资金流", "C",
        jumbo={bar.bar_time.strftime("%Y-%m-%d"): 3e6 for bar in make_bars()},
    )
    assert not chart.grab().isNull()


def test_paint_empty_and_no_candles_does_not_crash(qapp):
    chart = MoneyFlowChart()
    chart.resize(600, 150)
    assert not chart.grab().isNull()          # 没有主图：空白态
    candles = CandleChart()
    chart.bind(candles)
    chart.set_flow({"2026-11-02": 1.0})       # 有资金流但主图没有K线
    assert not chart.grab().isNull()
