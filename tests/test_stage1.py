"""第一阶段（MVR）验收测试：冷启动股票池、分组排序持久化、布局恢复、价格预警落库。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.tdx_names import board_of, load_name_index, parse_tnf, resolve_cache_dir
from app.db.models import Quote
from app.db.repo import Repo
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.alert_service import AlertService
from app.services.intraday import IntradaySampler, points_from_sampled_bars
from app.services.market_clock import (
    STATUS_AUCTION, STATUS_CLOSED, STATUS_HOLIDAY, STATUS_LUNCH, STATUS_TRADING, MarketClock,
)
from app.services.universe import UniverseService
from tests.fakes import FakeAdapter

SCHEMA = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"


def make_repo(tmp: Path) -> Repo:
    db = Database(tmp / "test.db", schema_path=SCHEMA)
    db.init_schema()
    db.seed_default_groups()
    return Repo(db)


# ================================================================ 数据源名称表
def test_tdx_tnf_parse():
    cache_dir = resolve_cache_dir("D:/new_tdx64/PYPlugins/user")
    if cache_dir is None:
        pytest.skip("本机无通达信安装目录")
    names = load_name_index(cache_dir)
    if not names:
        pytest.skip("tnf 代码表不可解析")
    assert names.get("600519.SH") == "贵州茅台"
    assert names.get("000001.SZ") == "平安银行"
    assert board_of("688981") == "科创板"
    assert board_of("300750") == "创业板"
    assert board_of("920001") == "北交所"


def test_tnf_bad_file_returns_empty(tmp_path: Path):
    broken = tmp_path / "shs.tnf"
    broken.write_bytes(b"x" * 137)
    assert parse_tnf(broken, "SH") == {}


# ================================================================ 分组与排序
def test_group_crud_and_order_persistence(tmp_path: Path):
    repo = make_repo(tmp_path)
    groups = repo.groups()
    assert len(groups) == 10
    assert groups[0]["name"] == "龙头核心池"

    gid = repo.create_group("测试组")
    assert repo.groups()[-1]["id"] == gid

    repo.upsert_stocks([
        {"symbol": "600000.SH", "name": "浦发银行", "board": "沪市主板"},
        {"symbol": "000001.SZ", "name": "平安银行", "board": "深市主板"},
        {"symbol": "300750.SZ", "name": "宁德时代", "board": "创业板"},
    ])
    assert repo.add_symbols(gid, ["600000.SH", "000001.SZ", "300750.SZ", "600000.SH"]) == 3
    assert repo.group_symbols(gid) == ["600000.SH", "000001.SZ", "300750.SZ"]

    order = repo.move_symbol(gid, "300750.SZ", 0)
    assert order == ["300750.SZ", "600000.SH", "000001.SZ"]

    repo.set_group_symbols(gid, ["000001.SZ", "300750.SZ", "600000.SH"])

    # 重新打开仓库（模拟重启）后顺序不变
    fresh = Repo(Database(tmp_path / "test.db", schema_path=SCHEMA))
    assert fresh.group_symbols(gid) == ["000001.SZ", "300750.SZ", "600000.SH"]

    renamed = repo.copy_group(gid, "测试组副本")
    assert repo.group_symbols(renamed) == fresh.group_symbols(gid)
    assert len(repo.groups()) == 12

    repo.delete_group(renamed)
    assert len(repo.groups()) == 11


def test_group_reorder_persists(tmp_path: Path):
    repo = make_repo(tmp_path)
    ids = [g["id"] for g in repo.groups()]
    reversed_ids = list(reversed(ids))
    repo.reorder_groups(reversed_ids)
    assert [g["id"] for g in Repo(Database(tmp_path / "test.db", schema_path=SCHEMA)).groups()] == reversed_ids


def test_add_symbols_rejects_unknown(tmp_path: Path):
    repo = make_repo(tmp_path)
    gid = repo.groups()[0]["id"]
    repo.upsert_stocks([{"symbol": "600000.SH", "name": "浦发银行"}])
    assert repo.add_symbols(gid, ["999999.SH"]) == 0
    assert repo.group_symbols(gid) == []


# ================================================================ 布局与设置
def test_layout_and_settings_roundtrip(tmp_path: Path):
    repo = make_repo(tmp_path)
    repo.set_setting("ui.mode", "grid")
    repo.set_setting("ui.grid_size", 9)
    repo.save_layout("global", "main", {"window_geometry": "abcd", "splitter": [300, 900, 320]})
    repo.save_layout("global", "grid", {"symbols": ["600000.SH"], "size": 9})

    fresh = Repo(Database(tmp_path / "test.db", schema_path=SCHEMA))
    assert fresh.get_setting("ui.mode") == "grid"
    assert fresh.get_setting("ui.grid_size") == "9"
    assert fresh.get_layout("global", "main")["splitter"] == [300, 900, 320]
    assert fresh.get_layout("global", "grid")["symbols"] == ["600000.SH"]

    # upsert 语义：重复保存是覆盖不是追加
    fresh.save_layout("global", "main", {"splitter": [1, 2, 3]})
    assert fresh.get_layout("global", "main")["splitter"] == [1, 2, 3]


# ================================================================ 股票池与搜索
def test_universe_load_and_search(tmp_path: Path):
    repo = make_repo(tmp_path)
    symbols = [f"{600000 + i}.SH" for i in range(120)]
    adapter = FakeAdapter(symbols=symbols)
    service = UniverseService(repo, adapter, {"datasource": {"user_dir": ""}})
    summary = service.load()
    assert summary["count"] == 120
    assert repo.stock_count() == 120

    rows = service.search("600010")
    assert rows and rows[0]["symbol"] == "600010.SH"

    # 名称来自本地 tnf 表；没有 tnf 时用代码兜底（不编造名称）
    repo.upsert_stocks([{"symbol": "600519.SH", "name": "贵州茅台"}])
    rows = service.search("茅台")
    assert rows and rows[0]["symbol"] == "600519.SH"


def test_pinyin_search(tmp_path: Path):
    pytest.importorskip("pypinyin")
    repo = make_repo(tmp_path)
    repo.upsert_stocks([{"symbol": "600519.SH", "name": "贵州茅台"}])
    service = UniverseService(repo, FakeAdapter(), {"datasource": {}})
    service.pinyin.build(repo.all_stocks())
    rows = service.search("gzmt")
    assert rows and rows[0]["symbol"] == "600519.SH"


# ================================================================ 采样分时
def _quote(price: float, volume: int, amount: float, inside: int, outside: int,
           symbol: str = "600000.SH", moment: datetime | None = None) -> Quote:
    moment = moment or datetime(2026, 11, 6, 10, 0, 0)
    return Quote(symbol=symbol, name="浦发银行", price=price, prev_close=10.0,
                 volume=volume, amount=amount, inside_volume=inside, outside_volume=outside,
                 data_level="A", source="fake", quote_time=moment, received_at=moment)


def test_sampler_first_frame_and_delta():
    sampler = IntradaySampler()
    now = datetime(2026, 11, 6, 10, 0, 0)
    assert sampler.feed(_quote(10.0, 100_000, 1_000_000, 40_000, 60_000, moment=now), now) is None

    point = sampler.feed(
        _quote(10.10, 130_000, 1_303_000, 50_000, 80_000, moment=now + timedelta(seconds=3)),
        now + timedelta(seconds=3))
    assert point is not None
    assert point.interval_volume == 30_000
    assert round(point.interval_amount) == 303_000
    assert point.direction == "buy"
    assert point.quality_flag == "sampled"
    assert round(point.avg_price, 3) == round(1_303_000 / 130_000, 3)


def test_sampler_no_update_and_day_reset():
    sampler = IntradaySampler()
    now = datetime(2026, 11, 6, 10, 0, 0)
    sampler.feed(_quote(10.0, 100_000, 1_000_000, 40_000, 60_000, moment=now), now)
    flat = sampler.feed(_quote(10.0, 100_000, 1_000_000, 40_000, 60_000,
                               moment=now + timedelta(seconds=3)), now + timedelta(seconds=3))
    assert flat.interval_volume == 0
    assert "no_update" in flat.quality_flag

    next_day = now + timedelta(days=1)
    assert sampler.feed(_quote(10.2, 200_000, 2_000_000, 80_000, 120_000, moment=next_day),
                        next_day) is None


def test_points_from_sampled_bars_restores_day():
    from app.db.models import Bar

    bars = [
        # 采样 bar 必须自带 quality_flag：还原函数现在按库里的标记原样返回，
        # 不再一律写死 sampled（否则东财分钟线会被标成快照采样）。
        Bar(symbol="600000.SH", period="1m", bar_time=datetime(2026, 11, 6, 9, 31),
            open=10.0, high=10.05, low=9.99, close=10.02, volume=10_000, amount=100_200,
            quality_flag="sampled"),
        Bar(symbol="600000.SH", period="1m", bar_time=datetime(2026, 11, 6, 9, 32),
            open=10.02, high=10.1, low=10.01, close=10.08, volume=20_000, amount=201_600,
            quality_flag="sampled"),
    ]
    points = points_from_sampled_bars(bars)
    assert len(points) == 2
    assert points[1].cumulative_volume == 30_000
    assert points[0].quality_flag == "sampled"


# ================================================================ 预警
def test_alert_rule_fires_and_persists(tmp_path: Path):
    repo = make_repo(tmp_path)
    service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
    rule_id = service.create_price_rule("600000.SH", "price_above", 10.5, level="P2")
    assert rule_id > 0

    now = datetime(2026, 11, 6, 10, 0, 0)
    below = Quote(symbol="600000.SH", name="浦发银行", price=10.4, prev_close=10.0, data_level="A")
    assert service.evaluate([below], now=now, trading=True) == []

    above = Quote(symbol="600000.SH", name="浦发银行", price=10.6, prev_close=10.0,
                  volume=1000, amount=10600, data_level="A", source="fake")
    fired = service.evaluate([above], now=now, trading=True)
    assert len(fired) == 1
    assert fired[0].event.event_type == "price_above"
    assert "10.6" in fired[0].event.condition
    assert repo.count_alerts() == 1

    # 冷却期内不重复
    assert service.evaluate([above], now=now + timedelta(seconds=30), trading=True) == []
    assert repo.count_alerts() == 1

    events = repo.recent_alerts(10)
    assert events[0]["symbol"] == "600000.SH"
    assert events[0]["level"] == "P2"
    assert events[0]["data_level"] == "A"


def test_alert_skipped_when_market_closed_or_mock(tmp_path: Path):
    repo = make_repo(tmp_path)
    service = AlertService(repo, AlertEngine(), require_trading=True)
    service.create_price_rule("600000.SH", "price_above", 10.0)
    quote = Quote(symbol="600000.SH", name="浦发银行", price=11.0, prev_close=10.0, data_level="A")
    assert service.evaluate([quote], trading=False) == []

    service.require_trading = False
    mock_quote = Quote(symbol="600000.SH", name="浦发银行", price=11.0, prev_close=10.0, data_level="D")
    assert service.evaluate([mock_quote], trading=True) == []


def test_alert_near_limit_up(tmp_path: Path):
    repo = make_repo(tmp_path)
    service = AlertService(repo, AlertEngine(), require_trading=False)
    service.create_price_rule("600000.SH", "near_limit_up", 1.0)
    quote = Quote(symbol="600000.SH", name="浦发银行", price=10.95, prev_close=10.0,
                  limit_up_price=11.0, data_level="A")
    fired = service.evaluate([quote], trading=True)
    assert fired and fired[0].event.event_type == "near_limit_up"
    assert "距涨停" in fired[0].event.condition


def test_alert_rule_toggle_blocks_event(tmp_path: Path):
    repo = make_repo(tmp_path)
    service = AlertService(repo, AlertEngine(), require_trading=False)
    rule_id = service.create_price_rule("600000.SH", "price_above", 10.0)
    repo.set_rule_enabled(rule_id, False)
    service.reload()
    quote = Quote(symbol="600000.SH", name="浦发银行", price=11.0, prev_close=10.0, data_level="A")
    assert service.evaluate([quote], trading=True) == []


# ================================================================ 交易时钟
def test_market_clock_states():
    clock = MarketClock("approximate")
    day = datetime(2026, 11, 6)          # 周五
    assert clock.status(day.replace(hour=9, minute=20)).label == STATUS_AUCTION
    assert clock.status(day.replace(hour=10, minute=0)).label == STATUS_TRADING
    assert clock.status(day.replace(hour=12, minute=0)).label == STATUS_LUNCH
    assert clock.status(day.replace(hour=15, minute=30)).label == STATUS_CLOSED
    weekend = datetime(2026, 11, 7, 10, 0)
    assert clock.status(weekend).label == STATUS_HOLIDAY

    clock.set_trading_dates(["20261106"])
    assert clock.status(day.replace(hour=10)).is_trading is True
    assert clock.status(datetime(2026, 11, 9, 10, 0)).label == STATUS_HOLIDAY


# ================================================================ 界面装配
@pytest.fixture(scope="module")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n"
        f"  url: sqlite:///{(tmp_path / 'ui.db').as_posix()}\n"
        "datasource:\n"
        "  name: tqcenter\n"
        "  poll_interval_sec: 1\n"
        "alerts:\n"
        "  require_trading: false\n",
        encoding="utf-8",
    )
    return config


def test_main_window_state_restore(qapp, tmp_path: Path):
    pytest.importorskip("PySide6")
    from app.ui.main_window import MainWindow

    config = _write_config(tmp_path)
    adapter = FakeAdapter(symbols=["600000.SH", "000001.SZ", "300750.SZ"])
    window = MainWindow(str(config), adapter=adapter)
    try:
        window.repo.upsert_stocks([
            {"symbol": "600000.SH", "name": "浦发银行"},
            {"symbol": "000001.SZ", "name": "平安银行"},
            {"symbol": "300750.SZ", "name": "宁德时代"},
        ])
        groups = window.repo.groups()
        gid = groups[0]["id"]
        window._refresh_groups(gid)
        window._add_symbols(["000001.SZ", "600000.SH"])
        assert window.left.table.symbols()[:2] == ["000001.SZ", "600000.SH"]

        window._select_symbol("600000.SH")
        window._set_mode("grid")
        window.grid_combo.setCurrentIndex(2)      # 6 格
        window._on_period_changed("1w")
        window._save_state()
    finally:
        window.close()

    restored = MainWindow(str(config), adapter=adapter)
    try:
        assert restored.current_symbol == "600000.SH"
        assert restored.mode == "grid"
        assert restored.grid_size == 6
        assert restored.chart_period == "1w"
        assert restored.left.table.symbols() == ["000001.SZ", "600000.SH"]
        assert restored.repo.group_symbols(gid) == ["000001.SZ", "600000.SH"]
    finally:
        restored.close()


def test_quote_table_updates_and_colors(qapp):
    pytest.importorskip("PySide6")
    from app.ui.panels import QuoteTable

    table = QuoteTable()
    table.set_symbols(["600000.SH"], {"600000.SH": "浦发银行"})
    quote = Quote(symbol="600000.SH", name="浦发银行", price=10.5, prev_close=10.0,
                  amount=123_000_000, data_level="A")
    table.update_quote(quote)
    assert table.item(0, 2).text() == "10.50"
    assert table.item(0, 3).text() == "+5.00%"
    from app.ui.theme import COLORS
    assert table.item(0, 3).foreground().color().name().lower() == COLORS["up"].lower()   # 红涨
    assert table.item(0, 4).text() == "1.23亿"


def test_candle_chart_does_not_crash_on_empty(qapp):
    pytest.importorskip("PySide6")
    from app.ui.charts import CandleChart, IntradayChart

    chart = CandleChart()
    chart.resize(600, 300)
    chart.repaint()
    intraday = IntradayChart()
    intraday.resize(600, 300)
    intraday.repaint()
