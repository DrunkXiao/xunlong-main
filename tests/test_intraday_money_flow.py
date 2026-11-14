"""分时资金流验收测试：字段映射、累计口径、异常分支与绘制。

不联网：HTTP 全部用假 `_request` 顶替。断言的重点不是「能画出来」，而是
**没有把累计净额再累加一次**、**五档字段没有错位**、**失败不返回空列表冒充没数据**。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.adapters.base import DataSourceError
from app.adapters.eastmoney_money_flow import (
    EastmoneyMoneyFlowAdapter, MinuteFlowPoint, points_from_klines,
)
from app.ui.charts import (
    FLOW_SERIES, IntradayMoneyFlowChart, _fmt_compact_signed, _fmt_compact_yuan,
)

DAY = "2026-11-13"


def row(hhmm: str, main: float, small: float, mid: float,
        big: float, jumbo: float, day: str = DAY) -> str:
    return f"{day} {hhmm},{main},{small},{mid},{big},{jumbo}"


# ==================================================================== 字段映射
def test_fields_map_to_f52_to_f56_in_order():
    """f52 主力 / f53 小单 / f54 中单 / f55 大单 / f56 超大单：顺序错一位就是两档对调。"""
    points = points_from_klines([
        row("09:31", 15_657_696.0, 0.0, -15_517_945.0, 5_707_962.0, 9_949_734.0),
    ])
    assert len(points) == 1
    point = points[0]
    assert point.main == 15_657_696.0
    assert point.small == 0.0
    assert point.mid == -15_517_945.0
    assert point.big == 5_707_962.0
    assert point.jumbo == 9_949_734.0
    assert point.time.strftime("%H:%M") == "09:31"


def test_values_are_used_as_is_not_accumulated_again():
    """接口回的是累计净额：解析层不许再加一遍，否则曲线会变成指数型。"""
    points = points_from_klines([
        row("09:31", 100.0, -30.0, -70.0, 40.0, 60.0),
        row("09:32", 150.0, -50.0, -100.0, 60.0, 90.0),
    ])
    assert [p.main for p in points] == [100.0, 150.0]


def test_bad_rows_dropped_without_losing_the_rest():
    points = points_from_klines([
        "坏行",
        row("09:31", 100.0, 0.0, -100.0, 40.0, 60.0),
        f"{DAY} 09:32,-,-,-,-,-",                 # 五档全空：没有画图价值
        row("09:33", 120.0, 0.0, -120.0, 50.0, 70.0),
    ])
    assert [p.time.strftime("%H:%M") for p in points] == ["09:31", "09:33"]


def test_partial_values_keep_point_and_leave_gap():
    """只有一档有数也要保留这个点：其余为 None，图上断线，不补 0。"""
    points = points_from_klines([f"{DAY} 09:31,100.0,-,-,-,-"])
    assert len(points) == 1
    assert points[0].main == 100.0
    assert points[0].small is None and points[0].jumbo is None


def test_rows_are_sorted_and_deduped_by_minute():
    points = points_from_klines([
        row("09:33", 3.0, 0, 0, 1.0, 2.0),
        row("09:31", 1.0, 0, 0, 1.0, 0.0),
        row("09:33", 30.0, 0, 0, 10.0, 20.0),     # 同一分钟覆盖为最新
    ])
    assert [p.time.strftime("%H:%M") for p in points] == ["09:31", "09:33"]
    assert points[1].main == 30.0


def test_time_only_rows_fall_back_to_previous_day():
    """上游若改成只回 `09:31`，也要能用上一行日期补全，不能让整条序列为空。"""
    points = points_from_klines([
        f"{DAY} 09:31,1.0,0,0,0,1.0",
        "09:32,2.0,0,0,0,2.0",
    ])
    assert [p.time.date() for p in points] == [date(2026, 11, 13)] * 2


# ==================================================================== 适配器
class FakeAdapter(EastmoneyMoneyFlowAdapter):
    def __init__(self, response=None, error: Exception | None = None, cfg: dict | None = None):
        super().__init__({"money_flow_intraday": {"retries": 0, "cache_sec": 60, **(cfg or {})}})
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def _request(self, params: dict) -> dict:
        self.calls.append(dict(params))
        if self.error:
            raise self.error
        return self.response


def payload(rows: list[str], name: str = "测试股") -> dict:
    return {"rc": 0,
            "data": {"code": "600519", "market": 1, "name": name, "klines": rows}}


def test_request_params_match_the_documented_endpoint():
    adapter = FakeAdapter(payload([row("09:31", 1.0, 0, 0, 0, 1.0)]))
    adapter.series("600519.SH")
    params = adapter.calls[0]
    assert params["klt"] == 1
    assert params["lmt"] == 0
    assert params["secid"] == "1.600519"          # 与分时图共用同一套 secid
    assert params["fields2"] == "f51,f52,f53,f54,f55,f56"


def test_series_keeps_only_last_day():
    adapter = FakeAdapter(payload([
        row("15:00", 1.0, 0, 0, 0, 1.0, day="2026-11-12"),
        row("09:31", 2.0, 0, 0, 0, 2.0),
        row("15:00", 3.0, 0, 0, 0, 3.0),
    ]))
    series = adapter.series("600519.SH")
    assert series.day.isoformat() == DAY
    assert len(series.points) == 2
    assert series.name == "测试股"


def test_series_raises_not_found_on_empty_klines():
    with pytest.raises(DataSourceError) as info:
        FakeAdapter(payload([])).series("600519.SH")
    assert info.value.kind == "not_found"
    assert info.value.retryable is False


def test_series_raises_on_rc_100():
    with pytest.raises(DataSourceError) as info:
        FakeAdapter({"rc": 100, "data": None}).series("999999.SH")
    assert info.value.kind in ("bad_request", "not_found")
    assert info.value.retryable is False


def test_disabled_adapter_says_so_instead_of_empty_chart():
    adapter = FakeAdapter(payload([]), cfg={"enabled": False})
    with pytest.raises(DataSourceError) as info:
        adapter.series("600519.SH")
    assert info.value.kind == "disabled"
    assert "enabled" in info.value.message


def test_cache_avoids_repeat_request_but_force_bypasses():
    adapter = FakeAdapter(payload([row("09:31", 1.0, 0, 0, 0, 1.0)]))
    adapter.series("600519.SH")
    adapter.series("600519.SH")
    assert len(adapter.calls) == 1
    adapter.series("600519.SH", force=True)
    assert len(adapter.calls) == 2


def test_network_error_is_retryable_and_has_original_text():
    adapter = FakeAdapter(error=TimeoutError("timed out"))
    with pytest.raises(DataSourceError) as info:
        adapter.series("600519.SH")
    assert info.value.retryable is True
    assert "timed out" in info.value.message


def test_latest_values_expose_all_five_series():
    adapter = FakeAdapter(payload([row("09:31", 1.0, -1.0, 0.0, 0.0, 1.0)]))
    series = adapter.series("600519.SH")
    assert series.latest_values() == {"main": 1.0, "small": -1.0, "mid": 0.0,
                                      "big": 0.0, "jumbo": 1.0}


# ==================================================================== 金额格式
def test_compact_money_keeps_units_readable():
    assert _fmt_compact_yuan(79_300_000) == "7930万"
    assert _fmt_compact_yuan(-5_281_000) == "-528万"
    assert _fmt_compact_yuan(288_000_000) == "2.88亿"
    assert _fmt_compact_yuan(0) == "0"
    assert _fmt_compact_yuan(None) == "--"
    assert _fmt_compact_signed(1_200_000) == "+120万"
    assert _fmt_compact_signed(-1_200_000) == "-120万"


# ==================================================================== 绘制
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def trading_minutes(count: int = 240) -> list[datetime]:
    """09:31–11:30 + 13:01–15:00 的真实分钟序列（与接口一致，共 240 个）。"""
    day = datetime(2026, 11, 13)
    morning = [day.replace(hour=9, minute=31) + timedelta(minutes=i) for i in range(120)]
    afternoon = [day.replace(hour=13, minute=1) + timedelta(minutes=i) for i in range(120)]
    return (morning + afternoon)[:count]


def make_points(count: int = 240) -> list[MinuteFlowPoint]:
    points = []
    for i, moment in enumerate(trading_minutes(max(2, count))):
        main = 1.0e6 * i
        jumbo = 6.0e5 * i
        big = 4.0e5 * i
        mid = -2.0e5 * i
        small = -(main + mid)
        points.append(MinuteFlowPoint(time=moment, main=main, jumbo=jumbo,
                                      big=big, mid=mid, small=small))
    return points[:count]


def test_five_series_are_distinct_colors(qapp):
    """五条线必须五种颜色：两档同色就分不清谁在流入。"""
    colors = [color for _key, _label, color in FLOW_SERIES]
    assert len(set(colors)) == 5
    assert [key for key, _label, _color in FLOW_SERIES] == \
        ["main", "jumbo", "big", "mid", "small"]


def test_paint_with_data_does_not_crash(qapp):
    chart = IntradayMoneyFlowChart()
    chart.resize(1000, 168)
    chart.set_points(make_points(), "东方财富分时资金流", "B")
    assert not chart.grab().isNull()
    assert chart.day == date(2026, 11, 13)


def test_paint_with_hover_card_does_not_crash_near_edges(qapp):
    chart = IntradayMoneyFlowChart()
    chart.resize(900, 168)
    chart.set_points(make_points(), "东方财富分时资金流", "B")
    for slot in (0, 120, 240):
        chart._hover_slot = slot
        assert not chart.grab().isNull()


def test_paint_empty_loading_and_error_states(qapp):
    chart = IntradayMoneyFlowChart()
    chart.resize(700, 168)
    assert not chart.grab().isNull()                    # 初始空态
    chart.clear_points("资金流加载中…")
    assert chart.empty_reason == "资金流加载中…"
    assert not chart.grab().isNull()
    chart.clear_points("东方财富分时资金流接口不可达：" + "x" * 120)
    assert not chart.grab().isNull()                    # 超长错误原文也不能崩


def test_single_point_shows_data_insufficient(qapp):
    """刚开盘只有 1 个点：说明了「至少 2 个点」，而不是画一条不存在的线。"""
    chart = IntradayMoneyFlowChart()
    chart.resize(800, 168)
    chart.set_points(make_points(1))
    assert not chart.grab().isNull()


def test_legend_skips_series_that_have_no_line(qapp):
    """整段空的档位不画线，也不该出现在图例里（否则像一直加载不出来）。

    注意：键在、但最新值为 None 不算「没有这条线」——线画到了前一分钟，
    只是这一刻缺数，图例应显示 `--` 而不是整条删掉。
    """
    chart = IntradayMoneyFlowChart()
    items = chart._legend_items({"main": 1.0}, plain=False)
    assert [text for text, _color in items] == ["主力:1"]
    assert [text for text, _color in chart._legend_items({"main": 1.0, "jumbo": None},
                                                          plain=False)] == \
        ["主力:1", "超大单:--"]


def test_narrow_width_falls_back_to_compact_legend(qapp):
    """窄画布放不下「名称:数值」时退化成只写名称，不压到标题或数据等级上。"""
    chart = IntradayMoneyFlowChart()
    chart.resize(430, 168)
    chart.set_points(make_points(), "东方财富分时资金流", "B")
    assert not chart.grab().isNull()


def test_clear_points_drops_previous_symbol_data(qapp):
    """换股时必须清掉上一只票的曲线，否则副图会显示别人的资金流。"""
    chart = IntradayMoneyFlowChart()
    chart.set_points(make_points(20), "东方财富分时资金流", "B")
    chart.clear_points("资金流加载中…")
    assert chart.points == []
    assert chart.day is None


# ==================================================================== 整页接线
def test_main_window_fills_intraday_flow(qapp, tmp_path):
    """切到「分时」并把后台结果接上副图：验证 job 标签、股票校验与填充。

    网络全部替身：行情用 FakeAdapter，资金流用 StubFlow；分时轮询线程在选股前停掉，
    否则它会真的去请求东财（测试不该联网）。
    """
    import time

    from app.ui.main_window import MainWindow
    from tests.fakes import FakeAdapter

    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n"
        f"  url: sqlite:///{(tmp_path / 'flow.db').as_posix()}\n"
        "datasource:\n"
        "  name: tqcenter\n"
        "  poll_interval_sec: 1\n"
        "intraday:\n"
        "  provider: snapshot\n"
        "alerts:\n"
        "  require_trading: false\n",
        encoding="utf-8",
    )

    class StubFlow:
        enabled = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def cached(self, symbol: str):
            return None

        def series(self, symbol: str):
            self.calls.append(symbol)
            return SimpleNamespace(points=make_points(30), data_level="B")

    window = MainWindow(str(config), adapter=FakeAdapter(symbols=["600000.SH"]))
    try:
        if window.intraday_poller is not None:
            window.intraday_poller.stop()
            window.intraday_poller = None
        stub = StubFlow()
        window.intraday_flow_adapter = stub

        window.repo.upsert_stocks([{"symbol": "600000.SH", "name": "浦发银行"}])
        window._refresh_groups(window.repo.groups()[0]["id"])
        window._add_symbols(["600000.SH"])
        window._on_period_changed("intraday")

        deadline = time.time() + 5
        while time.time() < deadline and not window.single_view.intraday_flow.points:
            window._on_ui_tick()
            qapp.processEvents()
            time.sleep(0.02)

        assert stub.calls == ["600000.SH"]
        chart = window.single_view.intraday_flow
        assert len(chart.points) == 30
        assert chart.day == date(2026, 11, 13)
        assert not chart.grab().isNull()
    finally:
        window.close()
