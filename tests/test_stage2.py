"""第二阶段验收测试：采样成交、区间大单、三向联动、量能预警。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db.models import Quote
from app.db.repo import Repo
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.alert_service import AlertService
from app.services.intraday import IntradaySampler, SamplePoint, points_from_sampled_bars
from app.services.large_order import ALLOWED_TAGS, LargeOrderDetector

SCHEMA = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
BASE = datetime(2026, 11, 6, 9, 30, 0)     # 一个交易日的开盘时刻，测试内固定


def make_quote(symbol: str = "600000.SH", price: float = 10.0, volume: int = 100_000,
               amount: float = 1_000_000.0, inside: int = 40_000,
               outside: int = 60_000, prev_close: float = 9.9) -> Quote:
    return Quote(
        symbol=symbol, name="测试股", price=price, prev_close=prev_close,
        volume=volume, amount=amount, inside_volume=inside, outside_volume=outside,
        data_level="A", source="fake", received_at=BASE,
    )


def make_point(seconds: int, amount: float, direction: str = "buy",
               price: float = 10.0, volume: int | None = None,
               quality: str = "sampled", span: float = 3.0,
               symbol: str = "600000.SH") -> SamplePoint:
    return SamplePoint(
        symbol=symbol, time=BASE + timedelta(seconds=seconds), price=price,
        avg_price=price,
        interval_volume=volume if volume is not None else int(amount / max(price, 0.01)),
        interval_amount=amount, cumulative_volume=0, cumulative_amount=0,
        direction=direction, quality_flag=quality, interval_span_sec=span,
    )


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "stage2.db", schema_path=SCHEMA)
    db.init_schema()
    r = Repo(db)
    r.upsert_stocks([{"symbol": "600000.SH", "name": "测试股"}])
    return r


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ==================================================================== 采样口径
def test_sampler_interval_equals_quote_diff():
    sampler = IntradaySampler()
    assert sampler.feed(make_quote(volume=100_000, amount=1_000_000.0), BASE) is None
    point = sampler.feed(make_quote(volume=103_000, amount=1_030_000.0),
                         BASE + timedelta(seconds=3))
    assert point is not None
    assert point.interval_volume == 3_000
    assert point.interval_amount == 30_000.0
    assert point.interval_span_sec == pytest.approx(3.0)
    assert point.quality_flag.startswith("sampled")


def test_sampler_no_update_is_not_zero_trade():
    sampler = IntradaySampler()
    sampler.feed(make_quote(volume=100_000), BASE)
    point = sampler.feed(make_quote(volume=100_000), BASE + timedelta(seconds=3))
    assert point.interval_volume == 0
    assert "no_update" in point.quality_flag


def test_sampler_skips_interval_after_empty_baseline():
    """首帧是空快照（volume/amount 都是 0）时，第一次做差不能把当天累计量当成一个区间。"""
    sampler = IntradaySampler()
    assert sampler.feed(make_quote(volume=0, amount=0.0), BASE) is None
    assert sampler.feed(make_quote(volume=42_704, amount=6_179_000_000.0),
                        BASE + timedelta(seconds=3)) is None, "空基线后的第一帧只能当新基线"
    point = sampler.feed(make_quote(volume=45_000, amount=6_185_000_000.0),
                         BASE + timedelta(seconds=6))
    assert point is not None
    assert point.interval_volume == 2_296, "区间量只算真实增量，不能吃到当天累计"


def test_sampled_bars_restore_span():
    from app.db.models import Bar
    bars = [
        Bar(symbol="600000.SH", period="1m", bar_time=BASE + timedelta(minutes=i),
            open=10.0, high=10.1, low=9.9, close=10.0, volume=1000, amount=10_000.0,
            data_level="B", source="tqcenter:sampled", quality_flag="sampled")
        for i in range(2)
    ]
    bars.append(Bar(symbol="600000.SH", period="1m", bar_time=BASE + timedelta(minutes=2),
                    open=10.0, high=10.1, low=9.9, close=10.0, volume=2000, amount=20_000.0,
                    data_level="B", source="tqcenter:sampled", quality_flag="sampled"))
    points = points_from_sampled_bars(bars)
    assert points[0].interval_span_sec == 0.0
    assert points[1].interval_span_sec == 60.0
    assert points[-1].cumulative_volume == sum(b.volume for b in bars)


# ==================================================================== 大单分档
def test_tier_by_interval_amount():
    detector = LargeOrderDetector()
    orders = detector.feed(make_point(3, 1_200_000, "buy"))
    assert orders and orders[0].tier == "normal"
    orders = detector.feed(make_point(6, 3_400_000, "buy"))
    assert orders[0].tier == "large"
    orders = detector.feed(make_point(9, 5_600_000, "sell"))
    assert orders[0].tier == "super"
    assert orders[0].tag in ALLOWED_TAGS


def test_no_update_produces_no_order():
    detector = LargeOrderDetector()
    orders = detector.feed(make_point(3, 0.0, "unknown", volume=0, quality="no_update"))
    assert orders == []


def test_streak_is_continuous_once_only():
    detector = LargeOrderDetector(streak_count=3, streak_multiplier=2.0)
    first = detector.feed(make_point(3, 4_000_000, "buy"))
    detector.feed(make_point(6, 4_000_000, "buy"))
    third = detector.feed(make_point(9, 4_000_000, "buy"))
    assert first[0].tier == "large"
    assert third[0].tier == "continuous"
    assert third[0].amount == pytest.approx(12_000_000)
    assert third[0].window_sec == pytest.approx(6.0)
    fourth = detector.feed(make_point(12, 4_000_000, "buy"))
    assert all(order.tier != "continuous" for order in fourth), "同一段连续行情只报一次"


def test_buy_sell_divergence_tag():
    detector = LargeOrderDetector()
    detector.feed(make_point(3, 1_200_000, "buy"))
    orders = detector.feed(make_point(6, 1_300_000, "sell"))
    assert orders[0].tag == "买卖分歧"


def test_price_position_from_daily_range():
    detector = LargeOrderDetector()
    assert detector.set_daily_range("600000.SH", 12.0, 8.0) is True
    assert detector.price_position("600000.SH", 11.5) == "relative_high"
    assert detector.price_position("600000.SH", 10.0) == "mid"
    assert detector.price_position("600000.SH", 8.2) == "relative_low"
    assert detector.price_position("000001.SZ", 8.2) == "unknown"


def test_tags_never_infer_identity():
    banned = ("主力", "机构", "庄家", "建仓", "出货", "游资")
    detector = LargeOrderDetector()
    sequence = [
        (3, 1_500_000, "buy", 10.00), (6, 2_000_000, "sell", 9.98),
        (9, 6_000_000, "buy", 10.02), (12, 1_100_000, "unknown", 10.01),
        (15, 4_000_000, "buy", 10.05),
    ]
    for seconds, amount, direction, price in sequence:
        for order in detector.feed(make_point(seconds, amount, direction, price=price)):
            assert order.tag in ALLOWED_TAGS
            text = f"{order.tier_label}{order.tag}{order.position_label}{order.direction_label}"
            assert not any(word in text for word in banned)


# ==================================================================== 落库
def test_large_orders_persist_idempotent(repo):
    detector = LargeOrderDetector()
    orders = detector.feed(make_point(3, 1_500_000, "buy", price=10.5))
    assert repo.insert_large_orders(orders) == 1
    assert repo.insert_large_orders(orders) == 0, "重复写入必须被忽略"
    rows = repo.recent_large_orders("600000.SH")
    assert rows and rows[0]["tier"] == "normal"
    assert rows[0]["source"] == "tqcenter:sampled"

    marks = repo.large_orders_by_date("600000.SH", 3650)
    day = BASE.strftime("%Y-%m-%d")
    assert marks[day]["count"] == 1
    assert marks[day]["max_tier"] == "normal"
    assert marks[day]["buy"] == pytest.approx(1_500_000)


# ==================================================================== 量能预警
def test_amount_spike_fires_once(repo):
    service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
    service.create_intraday_rule("600000.SH", "amount_spike", 3, cooldown_sec=300)
    points = [make_point(i * 3, 10_000.0) for i in range(1, 21)]
    points.append(make_point(63, 120_000.0))
    fired = service.evaluate_intraday({"600000.SH": points}, trading=True)
    assert len(fired) == 1
    assert fired[0].event.event_type == "amount_spike"
    assert "近20区间均值" in fired[0].event.condition
    assert fired[0].event.data_level == "B"

    again = service.evaluate_intraday({"600000.SH": points}, trading=True)
    assert again == [], "冷却期内不得重复触发"
    assert repo.count_alerts() == 1


def test_surge_rule_uses_sampled_window(repo):
    service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
    service.create_intraday_rule("600000.SH", "surge", 1.5, cooldown_sec=60)
    points = [make_point(0, 10_000.0, "buy", price=10.00),
              make_point(120, 10_000.0, "buy", price=10.05),
              make_point(180, 10_000.0, "buy", price=10.21)]
    fired = service.evaluate_intraday({"600000.SH": points}, trading=True)
    assert len(fired) == 1
    assert "分钟内涨" in fired[0].event.condition


def test_large_order_rule_matches_continuous(repo):
    service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
    service.create_large_order_rule("600000.SH", "large_order_streak", 10_000_000)
    detector = LargeOrderDetector()
    orders = []
    for seconds in (3, 6, 9):
        orders += detector.feed(make_point(seconds, 4_000_000, "buy"))
    fired = service.evaluate_large_orders(orders, names={"600000.SH": "测试股"}, trading=True)
    assert len(fired) == 1
    assert "区间累计" in fired[0].event.condition
    assert "单笔" not in fired[0].event.condition
    assert fired[0].event.name == "测试股"


def test_trading_gate_blocks_intraday_alerts(repo):
    service = AlertService(repo, AlertEngine(), require_trading=True)
    service.create_intraday_rule("600000.SH", "amount_above", 100_000)
    points = [make_point(0, 10_000.0), make_point(3, 900_000.0)]
    assert service.evaluate_intraday({"600000.SH": points}, trading=False) == []
    assert len(service.evaluate_intraday({"600000.SH": points}, trading=True)) == 1


# ==================================================================== 界面联动
def test_trade_panel_append_and_locate(qapp):
    from app.ui.panels import TradePanel

    panel = TradePanel()
    points = [make_point(i * 3, 1_000_000.0) for i in range(1, 6)]
    for point in points:
        panel.append_point(point)
    assert panel.trades.rowCount() == 5
    panel.select_time(points[2].time)
    assert panel.trades.currentRow() == 2

    detector = LargeOrderDetector()
    orders = detector.feed(make_point(20, 1_200_000, "buy"))
    panel.set_orders(orders)
    assert panel.orders.rowCount() == 1
    assert panel.orders.item(0, 3).text() in ALLOWED_TAGS


def test_append_point_ignores_duplicate_time(qapp):
    from app.ui.panels import SampledTradeTable

    table = SampledTradeTable()
    point = make_point(3, 1_000_000.0)
    table.append_point(point)
    table.append_point(point)
    assert table.rowCount() == 1


def test_no_update_row_shows_dash_not_zero(qapp):
    """快照没更新时，成交列表必须写 --，不能写 0（0 会被读成零成交）。"""
    from app.ui.panels import SampledTradeTable

    table = SampledTradeTable()
    table.append_point(make_point(3, 0.0, "unknown", volume=0, quality="no_update"))
    assert table.item(0, 1).text() == "--"
    assert table.item(0, 2).text() == "--"
    assert table.item(0, 3).text() == "无更新"


def test_intraday_chart_markers_and_focus(qapp):
    from PySide6.QtGui import QPixmap
    from app.ui.charts import IntradayChart

    chart = IntradayChart()
    chart.resize(700, 400)
    points = [make_point(i * 30, 1_000_000.0, price=10 + i * 0.01) for i in range(8)]
    chart.set_series(points, 9.9, 10.9, 8.9, "测试 · 采样分时", "A")
    detector = LargeOrderDetector()
    orders = []
    for seconds in (30, 60):
        orders += detector.feed(make_point(seconds, 6_000_000, "buy", price=10.05))
    chart.set_markers(orders)
    chart.set_focus(points[3].time)
    pixmap = QPixmap(700, 400)
    chart.render(pixmap)      # 渲染不应抛异常
    assert not pixmap.isNull()


def test_candle_chart_event_marks(qapp):
    from PySide6.QtGui import QPixmap
    from app.db.models import Bar
    from app.ui.charts import CandleChart

    chart = CandleChart()
    chart.resize(700, 400)
    bars = [Bar(symbol="600000.SH", period="1d", bar_time=BASE + timedelta(days=i),
                open=10, high=10.2, low=9.8, close=10.1, volume=1000, amount=10_000.0)
            for i in range(20)]
    chart.set_bars(bars, "日K", "A")
    chart.set_event_marks({bars[-1].bar_time.strftime("%Y-%m-%d"):
                           {"count": 3, "max_tier": "super", "buy": 5e6, "sell": 1e6}})
    chart.set_focus_date(bars[-1].bar_time.date())
    pixmap = QPixmap(700, 400)
    chart.render(pixmap)
    assert not pixmap.isNull()
