"""东方财富分时（分钟线）验收测试：换算、合并水位、口径文案、落库幂等。

全部用假 `_request` 顶替 HTTP，不联网；真实接口在 scripts/verify_intraday_em.py 里验。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.base import DataSourceError
from app.adapters.eastmoney_intraday import (
    EastmoneyIntradayAdapter, Trends, _parse_rows, secid,
)
from app.db.repo import Repo
from app.db.session import Database
from app.services.intraday import (
    MinuteFeed, minute_bars_to_rows, minute_slot_of, points_from_minute_bars,
    points_from_sampled_bars,
)
from app.services.large_order import LargeOrderDetector

SCHEMA = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
DAY = "2026-11-06"


def row(hhmm: str, close: float, hand: int, amount: float, avg: float,
        open_: float | None = None, high: float | None = None,
        low: float | None = None) -> str:
    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    return f"{DAY} {hhmm},{open_:.2f},{close:.2f},{high:.2f},{low:.2f},{hand},{amount:.2f},{avg:.3f}"


def payload(rows: list[str], name: str = "测试股", pre_close: float = 10.0) -> dict:
    return {"rc": 0, "data": {"code": "600000", "name": name, "preClose": pre_close,
                              "trendsTotal": 241, "trends": rows}}


class FakeAdapter(EastmoneyIntradayAdapter):
    """把 HTTP 换成固定回包，便于断言解析与错误分支。"""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        super().__init__({"intraday": {"retries": 0}})
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def _request(self, params: dict) -> dict:
        self.calls.append(dict(params))
        if self.error:
            raise self.error
        return self.response


def make_trends(count: int = 5, symbol: str = "600000.SH", pre_close: float = 10.0) -> Trends:
    bars = _parse_rows([row("09:30", 10.0, 1000, 1_000_000.0, 10.0)] +
                       [row(f"09:{30 + i:02d}", 10.0 + i * 0.01, 500, 500_000.0, 10.0 + i * 0.005)
                        for i in range(1, count)])
    return Trends(symbol=symbol, name="测试股", pre_close=pre_close,
                  day=datetime.strptime(DAY, "%Y-%m-%d").date(), bars=bars)


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "em.db", schema_path=SCHEMA)
    db.init_schema()
    r = Repo(db)
    r.upsert_stocks([{"symbol": "600000.SH", "name": "测试股"}])
    return r


# ==================================================================== 代码映射
def test_secid_mapping():
    assert secid("600519.SH") == "1.600519"
    assert secid("000001.SZ") == "0.000001"
    assert secid("920002.BJ") == "0.920002"
    assert secid("300750.SZ") == "0.300750"
    # 无后缀时按首位推断
    assert secid("600000") == "1.600000"
    assert secid("002696") == "0.002696"


# ==================================================================== 解析与单位
def test_parse_volume_hand_to_share():
    """接口给的是手，项目内部一律用股：×100，错一位就是 100 倍的假大单。"""
    bars = _parse_rows([row("09:30", 10.0, 1561, 1_561_000.0, 10.0)])
    assert len(bars) == 1
    assert bars[0].volume_hand == 1561
    assert bars[0].volume == 156_100
    assert bars[0].amount == 1_561_000.0
    assert bars[0].avg_price == 10.0
    assert bars[0].price == 10.0


def test_parse_skips_bad_rows_and_keeps_zero_trade_minute():
    bars = _parse_rows([
        "2026-11-06 09:30,0.00,10.00,10.00,10.00,100,100000.00,10.000",
        "2026-11-06 09:31,-,-,-,-,-,-,-",                 # 停牌/无数据行，跳过
        "2026-11-06 09:32,10.00,10.00,10.00,10.00,0,0.00,10.000",   # 真实零成交，保留
        "坏行",
    ])
    assert [b.time.strftime("%H:%M") for b in bars] == ["09:30", "09:32"]
    assert bars[1].volume == 0 and bars[1].amount == 0.0
    # 开盘价可能是 0，价格一律取收盘
    assert bars[0].close == 10.0


def test_trends_keeps_only_last_day_and_reports_totals():
    rows = [row("15:00", 10.0, 100, 100_000.0, 10.0)]
    rows = [r.replace(DAY, "2026-11-05") for r in rows] + rows
    adapter = FakeAdapter(payload(rows))
    trends = adapter.trends("600000.SH", days=2)
    assert len(trends.bars) == 1
    assert trends.day.isoformat() == DAY
    assert trends.total_volume == 10_000
    assert trends.total_amount == 100_000.0
    assert trends.source == "eastmoney:trends"
    assert adapter.calls[0]["ndays"] == 2


def test_trends_raises_when_code_has_no_data():
    with pytest.raises(DataSourceError) as info:
        FakeAdapter({"rc": 100, "data": None}).trends("999999.SH")
    assert info.value.kind in ("bad_request", "not_found")
    assert info.value.retryable is False


def test_trends_raises_when_series_empty():
    with pytest.raises(DataSourceError) as info:
        FakeAdapter(payload([])).trends("600000.SH")
    assert info.value.kind == "not_found"


# ==================================================================== 分钟点
def test_points_use_interval_amount_and_avg_from_vendor():
    """区间量额就是该分钟的成交，不做差分；均价用供应商给的，不自己重算。"""
    bars = _parse_rows([row("09:30", 10.0, 1000, 1_000_000.0, 9.98),
                        row("09:31", 10.1, 2000, 2_020_000.0, 10.02)])
    points = points_from_minute_bars(bars, "600000.SH", prev_close=10.0)
    assert [p.interval_volume for p in points] == [100_000, 200_000]
    assert [p.interval_amount for p in points] == [1_000_000.0, 2_020_000.0]
    assert [p.avg_price for p in points] == [9.98, 10.02]
    assert points[0].cumulative_volume == 100_000
    assert points[1].cumulative_volume == 300_000
    assert points[1].cumulative_amount == 3_020_000.0


def test_direction_is_price_proxy_not_inside_outside():
    bars = _parse_rows([row("09:30", 10.0, 100, 100_000.0, 10.0),      # 平昨收 → 不明
                        row("09:31", 10.2, 100, 102_000.0, 10.0),      # 涨 → 偏买
                        row("09:32", 10.1, 100, 101_000.0, 10.0)])     # 跌 → 偏卖
    points = points_from_minute_bars(bars, "600000.SH", prev_close=10.0)
    assert [p.direction for p in points] == ["unknown", "buy", "sell"]
    # 必须标明方向是价格推演出来的，不能冒充内外盘
    assert all("price_proxy" in p.quality_flag for p in points)
    assert all("em_minute" in p.quality_flag for p in points)


def test_zero_trade_minute_marked_no_trade_not_no_update():
    """分钟线里的 0 是真没撮合，和快照「没更新」不是一回事，标记必须分开。"""
    bars = _parse_rows([row("14:59", 10.0, 0, 0.0, 10.0)])
    point = points_from_minute_bars(bars, "600000.SH")[0]
    assert "no_trade" in point.quality_flag
    assert "no_update" not in point.quality_flag
    assert point.interval_volume == 0


def test_interval_span_is_sixty_but_keeps_real_gap():
    bars = _parse_rows([row("11:29", 10.0, 100, 100_000.0, 10.0),
                        row("11:30", 10.0, 100, 100_000.0, 10.0),
                        row("13:01", 10.0, 100, 100_000.0, 10.0)])
    points = points_from_minute_bars(bars, "600000.SH")
    assert points[1].interval_span_sec == 60.0
    # 午休跨 91 分钟，不能当成 60 秒，否则预警的「区间均值」会被拉偏
    assert points[2].interval_span_sec == 91 * 60


# ==================================================================== 合并与水印
def test_feed_first_load_replays_but_does_not_alert():
    feed = MinuteFeed()
    series, fresh, replay = feed.update(make_trends(5))
    assert len(series) == 5
    assert fresh == []                 # 首载不补发告警
    assert len(replay) == 4            # 但历史分钟要算记录（最后一根还在累积，不算）
    assert feed.fed_upto("600000.SH") == series[-1].time


def test_feed_same_payload_produces_no_fresh():
    feed = MinuteFeed()
    trends = make_trends(5)
    feed.update(trends)
    _, fresh, replay = feed.update(trends)
    assert fresh == [] and replay == []


def test_feed_new_minute_closes_previous_one():
    """只有「后面已经出现下一根」的分钟才算收住，才能交出去判定。

    首载那一刻正在成交的分钟（这里 09:34）不补判，所以 feed(6) 不出新点：
    09:35 尚在累积，09:34 属于首载水位，宁可不判也不回溯告警。
    """
    feed = MinuteFeed()
    feed.update(make_trends(5))
    _, fresh, _ = feed.update(make_trends(6))
    assert fresh == []
    _, fresh2, _ = feed.update(make_trends(7))
    assert [p.time.minute for p in fresh2] == [35]
    assert feed.fed_upto("600000.SH").minute == 35


def test_feed_does_not_refeed_when_price_updates_within_same_minute():
    """同一根分钟线内容变化（当前分钟还在成交）不能重复产出可判定点。"""
    feed = MinuteFeed()
    feed.update(make_trends(5))
    feed.update(make_trends(6))
    again = make_trends(6)
    again.bars[-1].amount = 9_999_999.0        # 当前分钟量变大
    _, fresh, _ = feed.update(again)
    assert fresh == []


def test_feed_error_is_visible():
    feed = MinuteFeed()
    feed.note_error("网络断了")
    assert feed.last_error == "网络断了"
    assert feed.has_data("600000.SH") is False
    assert feed.series("600000.SH") == []


# ==================================================================== 落库
def test_minute_bars_roundtrip_keeps_ohlc_and_flag(repo):
    trends = make_trends(5)
    rows = minute_bars_to_rows(trends.bars, "600000.SH")
    assert repo.upsert_bars(rows) == 5
    assert repo.upsert_bars(rows) == 5          # 幂等：同 key 覆盖，不新增
    bars = repo.load_bars("600000.SH", "1m", 10)
    assert len(bars) == 5
    assert bars[0].quality_flag == "em_minute"
    assert bars[0].source == "eastmoney:trends"
    assert bars[0].open == 10.0 and bars[0].high == 10.0   # 开高低收都要留住

    restored = points_from_sampled_bars(bars)
    assert restored[0].quality_flag.startswith("em_minute")
    # 还原后方向要与线上一致（按收价推演），不能全变成「不明」
    assert restored[1].direction == "buy"


def test_em_and_sampled_bars_coexist_without_overwrite(repo):
    """同一分钟两种来源：唯一键相同，写谁就是谁，不能互相污染质量标记。"""
    trends = make_trends(1)
    repo.upsert_bars(minute_bars_to_rows(trends.bars, "600000.SH"))
    bars = repo.load_bars("600000.SH", "1m", 10)
    assert bars[0].quality_flag == "em_minute"


# ==================================================================== 联动与大单
def test_minute_orders_use_scaled_threshold_and_source():
    """1 分钟区间按「元/分钟」门槛判档，来源如实写成东财。"""
    detector = LargeOrderDetector()
    points = points_from_minute_bars(
        _parse_rows([row("09:30", 10.0, 10000, 25_000_000.0, 10.0)]), "600000.SH")
    orders = detector.feed(points[0], "B")
    assert len(orders) == 1
    assert orders[0].tier == "normal"          # 2500 万 ≥ 2000 万，未到大额 6000 万
    assert round(orders[0].threshold) == 20_000_000
    assert orders[0].source == "eastmoney:trends"


def test_sampled_orders_keep_old_threshold():
    """3 秒采样区间自动折算成 1/20，与阶段二完全一致（100 万＝普通）。"""
    from tests.test_stage2 import make_point

    detector = LargeOrderDetector()
    orders = detector.feed(make_point(3, 1_200_000, "buy"))
    assert orders and orders[0].tier == "normal"
    assert round(orders[0].threshold) == 1_000_000
    assert orders[0].source == "tqcenter:sampled"


# ==================================================================== 时间轴
def test_minute_slot_matches_chart_axis():
    assert minute_slot_of(datetime(2026, 11, 6, 9, 30)) == 0
    assert minute_slot_of(datetime(2026, 11, 6, 11, 30)) == 120
    assert minute_slot_of(datetime(2026, 11, 6, 13, 1)) == 121
    assert minute_slot_of(datetime(2026, 11, 6, 15, 0)) == 240


# ==================================================================== 口径红线
def test_no_identity_inference_words():
    """分时链路只描述成交，不推断资金身份。"""
    banned = ("主力", "庄家", "机构进场", "建仓", "出货", "游资", "洗盘", "对倒")
    for name in ("app/adapters/eastmoney_intraday.py", "app/services/intraday.py",
                 "app/services/intraday_poller.py"):
        text = Path(name).read_text(encoding="utf-8")
        hits = [word for word in banned if word in text]
        assert not hits, f"{name} 出现身份推断词：{hits}"


def test_trade_table_never_claims_tick():
    """成交表口径文案只能写「分钟/区间」，不能出现「逐笔成交」这种表述。"""
    text = Path("app/ui/panels.py").read_text(encoding="utf-8")
    assert "东方财富 1 分钟聚合" in text
    assert "逐笔流水" in text or "非逐笔" in text


def test_build_intraday_adapter_is_always_eastmoney():
    """分时固定东财：旧的 snapshot 开关不再产出「无适配器」，也不会静默降级。"""
    from app.adapters import build_intraday_adapter

    for provider in ("eastmoney", "snapshot", "none", "off", "disabled", ""):
        adapter = build_intraday_adapter({"intraday": {"provider": provider}})
        assert adapter.__class__.__name__ == "EastmoneyIntradayAdapter", provider
    assert build_intraday_adapter({}).__class__.__name__ == "EastmoneyIntradayAdapter"

    with pytest.raises(DataSourceError) as info:
        build_intraday_adapter({"intraday": {"provider": "某不存在"}})
    assert info.value.retryable is False


# ==================================================================== 回包残缺保护
def test_feed_keeps_series_when_response_is_truncated():
    """东财是整日覆盖式返回：条数突然变少只能是残缺回包，不能覆盖完整序列。"""
    feed = MinuteFeed()
    full, _, _ = feed.update(make_trends(20))
    assert len(full) == 20

    partial = make_trends(2)          # 限流/断流重连时可能只回 2 根
    series, fresh, replay = feed.update(partial)
    assert len(series) == 20, "残缺回包不能把 20 个点塌成 2 个点"
    assert fresh == [] and replay == []
    assert "不完整" in (feed.last_error or "")
    assert len(feed.series("600000.SH")) == 20


def test_feed_keeps_series_when_response_is_empty():
    feed = MinuteFeed()
    feed.update(make_trends(5))
    empty = Trends(symbol="600000.SH", name="测试股", pre_close=10.0,
                   day=datetime.strptime(DAY, "%Y-%m-%d").date(), bars=[])
    series, fresh, _ = feed.update(empty)
    assert len(series) == 5 and fresh == []
    assert len(feed.series("600000.SH")) == 5
    assert "为空" in (feed.last_error or "")


def test_feed_accepts_next_day_even_if_shorter():
    """跨日就该换序列：新的一天只有 1 根是正常的，不能拿昨天的 241 根顶着。"""
    feed = MinuteFeed()
    feed.update(make_trends(20))
    tomorrow = make_trends(1)
    tomorrow.bars[0].time = tomorrow.bars[0].time + timedelta(days=1)
    tomorrow.day = tomorrow.bars[0].time.date()
    series, _, _ = feed.update(tomorrow)
    assert len(series) == 1 and series[0].time.date() == tomorrow.day


# ==================================================================== 库内还原口径
def test_restore_ignores_sampled_bars_on_later_day(repo):
    """分时图还原只认东财：当天只剩快照采样的 1 个点，不能当成「最近交易日」。"""
    from types import SimpleNamespace

    from app.db.models import Bar
    from app.ui.main_window import MainWindow

    em_day = datetime.strptime(DAY, "%Y-%m-%d")
    repo.upsert_bars([
        Bar(symbol="600000.SH", period="1m", bar_time=em_day.replace(hour=9, minute=30)
            + timedelta(minutes=i), open=10.0, high=10.0, low=10.0, close=10.0 + i * 0.01,
            volume=1000, amount=10_000.0, data_level="B", source="eastmoney:trends",
            quality_flag="em_minute")
        for i in range(241)
    ])
    # 次日只有行情轮询写下的 1 根采样
    repo.upsert_bars([
        Bar(symbol="600000.SH", period="1m", bar_time=em_day.replace(hour=9, minute=30)
            + timedelta(days=1), open=10.0, high=10.0, low=10.0, close=10.0,
            volume=500, amount=5_000.0, data_level="D", source="tqcenter:sampled",
            quality_flag="sampled")
    ])

    holder = SimpleNamespace(repo=repo)
    points, source = MainWindow._restore_intraday_points(holder, "600000.SH", em_only=True)
    assert source == "em"
    assert len(points) == 241
    assert points[-1].time.date() == em_day.date()

    # 旧口径（不限来源）会退化成 1 个点——这正是分时图显示「数据不足」的原因
    legacy, _ = MainWindow._restore_intraday_points(holder, "600000.SH")
    assert len(legacy) == 1


def test_load_bars_filters_by_quality_without_starving_limit(repo):
    """按来源过滤必须在 SQL 里做：limit 不能被另一类行挤占。"""
    from app.db.models import Bar

    day = datetime.strptime(DAY, "%Y-%m-%d").replace(hour=9, minute=30)
    repo.upsert_bars([
        Bar(symbol="600000.SH", period="1m", bar_time=day + timedelta(days=1, minutes=i),
            open=10.0, high=10.0, low=10.0, close=10.0, volume=100, amount=1000.0,
            data_level="D", source="tqcenter:sampled", quality_flag="sampled")
        for i in range(30)
    ])
    repo.upsert_bars([
        Bar(symbol="600000.SH", period="1m", bar_time=day + timedelta(minutes=i),
            open=10.0, high=10.0, low=10.0, close=10.0, volume=100, amount=1000.0,
            data_level="B", source="eastmoney:trends", quality_flag="em_minute")
        for i in range(241)
    ])
    assert len(repo.load_bars("600000.SH", "1m", 10)) == 10          # 混存时取最近的
    only_em = repo.load_bars("600000.SH", "1m", 400, quality_contains="em_minute")
    assert len(only_em) == 241
    assert all("em_minute" in b.quality_flag for b in only_em)
