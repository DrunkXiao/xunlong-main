"""free-stockdb 分时适配器验收测试：注入假 reader，不联网、不依赖引擎。

为什么改成注入 reader：
引擎的 HTTP 回包是**二进制协议**（不是 JSON），上一版拿假 HTTP 服务模拟
「JSON 回包」，恰恰把这个真实缺陷测成了通过。现在适配器只依赖
`stock_sdk.rd` 的三个调用（最新分钟键 / 整日分钟 / 日线），
测试就锁在这三个调用上——形状由官方文档定义，跟传输层无关。

覆盖：
- `分钟k` 记录 → MinuteBar 的字段映射、单位、均价递推、去重与脏数据丢弃
- 最近交易日定位：优先用最新分钟键，取不到才逐日回溯
- 盘中数据非当天 → stale，而不是拿昨天冒充今天
- 引擎连不上 → network；SDK 缺失 → not_installed（两条文案都要能直接指导操作）
- 与现有来源标记体系兼容：落库后能用 MINUTE_FLAGS 和东财一起筛出来
- 主源失败回落东财、冷却期内不重复撞墙
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.adapters import build_intraday_adapter
from app.adapters.base import DataSourceError
from app.adapters.fallback import FallbackIntradayAdapter
from app.adapters.free_stockdb_intraday import (
    FreeStockDbIntradayAdapter, SdkReader, day_from_key, parse_minute_time, stockdb_code,
)
from app.adapters.minute import MINUTE_FLAGS, STOCKDB_FLAG, Trends
from app.db.repo import Repo
from app.db.session import Database
from app.services.intraday import MinuteFeed, minute_bars_to_rows

SCHEMA = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"


# ---------------------------------------------------------------- 测试工具
def weekdays_back(count: int) -> list[date]:
    """从今天往回取 count 个工作日（含今天，若今天是工作日）。"""
    out: list[date] = []
    cursor = date.today()
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return out


def minute_rows(day: date, prices: list[float], volumes: list[int] | None = None) -> list[dict]:
    """造 day 当天 09:30 起的若干分钟记录，字段与官方 `rd.get_data` 一致（date 是 int）。"""
    volumes = volumes or [10_000] * len(prices)
    rows = []
    for idx, price in enumerate(prices):
        moment = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
        moment += timedelta(minutes=idx)
        rows.append({
            "code": "600519",
            "date": int(moment.strftime("%Y%m%d%H%M%S")),
            "open": price, "high": price + 0.1, "low": price - 0.1, "close": price,
            "volume": volumes[idx],
            "amount": round(volumes[idx] * price, 2),
        })
    return rows


class FakeReader:
    """假 reader：只实现适配器真正用到的那几个方法。

    `minute` 是 `{date: rows}`；`latest` 是最新分钟键（空串表示引擎不支持/无数据）。
    """

    def __init__(self, minute=None, daily=None, latest: str = "",
                 latest_error: Exception | None = None,
                 minute_error: Exception | None = None,
                 groups=None) -> None:
        self.minute = minute or {}
        self.daily = daily or []
        self.latest = latest
        self.latest_error = latest_error
        self.minute_error = minute_error
        self.groups = groups if groups is not None else {"0": ["000001"], "6": ["600519"]}
        self.calls: list[tuple[str, str]] = []

    def latest_minute_key(self, code: str) -> str:
        self.calls.append(("latest", code))
        if self.latest_error is not None:
            raise self.latest_error
        return self.latest

    def minute_rows(self, code: str, day: date) -> list[dict]:
        self.calls.append(("minute", f"{code}:{day:%Y%m%d}"))
        if self.minute_error is not None:
            raise self.minute_error
        return list(self.minute.get(day, []))

    def daily_rows(self, code: str, start: date, end: date) -> list[dict]:
        self.calls.append(("daily", code))
        return list(self.daily)

    def code_groups(self) -> dict:
        return dict(self.groups)


def make_config(**overrides) -> dict:
    cfg = {
        "intraday": {
            "provider": "free_stockdb",
            "free_stockdb": {
                "days": 5,
                "strict_today": True,
                "volume_unit": "share",
            },
        }
    }
    cfg["intraday"]["free_stockdb"].update(overrides)
    return cfg


def adapter_with(reader, **overrides) -> FreeStockDbIntradayAdapter:
    return FreeStockDbIntradayAdapter(make_config(**overrides), reader=reader)


# ---------------------------------------------------------------- 纯函数
def test_stockdb_code_normalizes_symbols():
    assert stockdb_code("600519.SH") == "600519"
    assert stockdb_code("000001.SZ") == "000001"
    assert stockdb_code("sh600519") == "600519"
    assert stockdb_code(" 300750 ") == "300750"


def test_parse_minute_time_accepts_12_13_14_digits_and_rejects_junk():
    assert parse_minute_time(20260625145200) == datetime(2026, 6, 25, 14, 52, 0)
    assert parse_minute_time("20260625145200") == datetime(2026, 6, 25, 14, 52, 0)
    # 官方示例里的 13 位（少一位秒），按补 0 处理
    assert parse_minute_time("2026062310000") == datetime(2026, 6, 23, 10, 0, 0)
    assert parse_minute_time("20260625145") is None
    assert parse_minute_time("not-a-time") is None
    assert parse_minute_time(None) is None


def test_day_from_key_reads_the_date_part():
    assert day_from_key("分钟k:600519:20261113150000") == date(2026, 11, 13)
    assert day_from_key("") is None
    assert day_from_key("分钟k:600519:garbage") is None


# ---------------------------------------------------------------- 取数
def test_trends_maps_fields_units_and_running_average():
    day = weekdays_back(1)[0]
    reader = FakeReader(
        minute={day: minute_rows(day, [10.0, 10.2, 10.1], [10_000, 20_000, 0])},
        latest=f"分钟k:600519:{day:%Y%m%d}150000",
    )
    trends = adapter_with(reader).trends("600519.SH")

    assert trends.day == day
    assert trends.source == "stockdb:minute"
    assert trends.data_level == "C"                    # 本地同步数据，不是实时授权
    assert trends.minute_flag == STOCKDB_FLAG
    assert [b.volume for b in trends.bars] == [10_000, 20_000, 0]
    # 均价 = 当日累计额 / 当日累计量（与东财 f58 同口径）
    assert trends.bars[0].avg_price == pytest.approx(10.0, abs=1e-6)
    assert trends.bars[1].avg_price == pytest.approx(
        (10_000 * 10.0 + 20_000 * 10.2) / 30_000, abs=1e-6)
    # 零成交分钟保留但量额为 0，均价沿用累计值
    assert trends.bars[2].volume == 0 and trends.bars[2].amount == 0.0
    assert trends.bars[2].avg_price == trends.bars[1].avg_price


def test_trends_drops_duplicate_and_invalid_rows():
    day = weekdays_back(1)[0]
    rows = minute_rows(day, [10.0, 10.1])
    bad = dict(rows[0], close=0)                        # 收价 0：非法
    dup = dict(rows[1], close=99.0)                     # 同一分钟重复，后到的覆盖先到的
    reader = FakeReader(
        minute={day: [bad, rows[1], dup]},
        latest=f"分钟k:600519:{day:%Y%m%d}150000",
    )
    trends = adapter_with(reader).trends("600519")
    assert len(trends.bars) == 1
    assert trends.bars[0].close == 99.0


def test_volume_unit_hand_multiplies_by_100():
    day = weekdays_back(1)[0]
    reader = FakeReader(minute={day: minute_rows(day, [10.0], [123])},
                        latest=f"分钟k:600519:{day:%Y%m%d}093000")
    trends = adapter_with(reader, volume_unit="hand").trends("600519")
    assert trends.bars[0].volume == 12_300            # 123 手 → 12300 股
    assert trends.bars[0].volume_hand == 123


def test_pre_close_and_name_come_from_daily_table():
    day = weekdays_back(1)[0]
    prev = day - timedelta(days=1)
    reader = FakeReader(
        minute={day: minute_rows(day, [10.0])},
        latest=f"分钟k:600519:{day:%Y%m%d}150000",
        daily=[
            {"code": "600519", "date": int(prev.strftime("%Y%m%d")), "name": "贵州茅台",
             "close": 9.88},
            {"code": "600519", "date": int(day.strftime("%Y%m%d")), "name": "贵州茅台",
             "close": 10.0, "pre_close": 9.88},
        ],
    )
    trends = adapter_with(reader).trends("600519")
    assert trends.name == "贵州茅台"
    assert trends.pre_close == pytest.approx(9.88)     # 优先用日线的 pre_close 字段


def test_pre_close_falls_back_to_previous_close_when_field_missing():
    day = weekdays_back(1)[0]
    prev = day - timedelta(days=1)
    reader = FakeReader(
        minute={day: minute_rows(day, [10.0])},
        latest=f"分钟k:600519:{day:%Y%m%d}150000",
        daily=[
            {"code": "600519", "date": int(prev.strftime("%Y%m%d")), "close": 9.50},
            {"code": "600519", "date": int(day.strftime("%Y%m%d")), "close": 10.0},
        ],
    )
    assert adapter_with(reader).trends("600519").pre_close == pytest.approx(9.50)


def test_uses_latest_minute_key_to_locate_day(monkeypatch):
    """最新分钟键能直接定位交易日：只发 1 次 latest + 1 次 minute，不做逐日回溯。"""
    old = weekdays_back(2)[1]
    if old == weekdays_back(1)[0]:
        pytest.skip("需要一个与今天不同的最近交易日")
    reader = FakeReader(minute={old: minute_rows(old, [10.0])},
                        latest=f"分钟k:600519:{old:%Y%m%d}150000")

    from app.adapters import free_stockdb_intraday as mod
    monkeypatch.setattr(mod, "is_live", lambda now=None: False)   # 非盘中：允许回放上一交易日

    trends = adapter_with(reader).trends("600519")
    assert trends.day == old
    kinds = [call[0] for call in reader.calls]
    assert kinds.count("minute") == 1                 # 没有逐日试探


def test_probes_backwards_when_latest_key_unavailable(monkeypatch):
    """引擎不支持 keys 查询（老版本）时，退回逐日回溯。"""
    old = weekdays_back(2)[1]
    if old == weekdays_back(1)[0]:
        pytest.skip("需要一个与今天不同的最近交易日")
    reader = FakeReader(minute={old: minute_rows(old, [10.0])})

    from app.adapters import free_stockdb_intraday as mod
    monkeypatch.setattr(mod, "is_live", lambda now=None: False)

    trends = adapter_with(reader).trends("600519")
    assert trends.day == old


def test_stale_when_intraday_minutes_are_not_today(monkeypatch):
    days = weekdays_back(2)
    older = days[1]
    if days[0] == older:
        pytest.skip("需要一个与今天不同的最近交易日")
    reader = FakeReader(minute={older: minute_rows(older, [10.0])},
                        latest=f"分钟k:600519:{older:%Y%m%d}150000")

    from app.adapters import free_stockdb_intraday as mod
    monkeypatch.setattr(mod, "is_live", lambda now=None: True)   # 盘中：绝不能拿昨日冒充今日

    with pytest.raises(DataSourceError) as exc:
        adapter_with(reader).trends("600519")
    assert exc.value.kind == "stale"
    assert "同步" in exc.value.message


def test_strict_today_can_be_disabled(monkeypatch):
    days = weekdays_back(2)
    older = days[1]
    if days[0] == older:
        pytest.skip("需要一个与今天不同的最近交易日")
    reader = FakeReader(minute={older: minute_rows(older, [10.0])},
                        latest=f"分钟k:600519:{older:%Y%m%d}150000")

    from app.adapters import free_stockdb_intraday as mod
    monkeypatch.setattr(mod, "is_live", lambda now=None: True)

    trends = adapter_with(reader, strict_today=False).trends("600519")
    assert trends.day == older


def test_engine_down_raises_actionable_network_error():
    """reader 把「连不上」翻译成 network；文案要直接告诉用户去启动 stockdb.exe。"""
    down = DataSourceError(
        kind="network",
        message="free-stockdb 引擎不可达：请先启动 stockdb.exe 并确认 127.0.0.1:7899 在监听",
        retryable=True,
    )
    reader = FakeReader(latest_error=down, minute_error=down)
    with pytest.raises(DataSourceError) as exc:
        adapter_with(reader).trends("600519")
    assert exc.value.kind == "network"
    assert "stockdb.exe" in exc.value.message


def test_sdk_reader_translates_connection_errors():
    """SdkReader 要把底层 OSError 统一翻成 network，别让上层看到随机异常类型。"""
    reader = SdkReader(sdk_dir="")
    reader._rd = object()          # 跳过真实导入，只验证异常翻译
    with pytest.raises(DataSourceError) as exc:
        reader._guard(lambda rd: (_ for _ in ()).throw(ConnectionRefusedError("拒绝连接")),
                      "测试查询")
    assert exc.value.kind == "network"
    assert exc.value.retryable is True


def test_missing_sdk_raises_not_installed(monkeypatch):
    """没装 SDK 时不能假装「没数据」，要明确告诉用户去哪儿配路径。"""
    import sys

    from app.adapters import sdk_bootstrap
    monkeypatch.setattr(sdk_bootstrap, "DEFAULT_SDK_DIRS", ())
    monkeypatch.delenv("STOCKDB_SDK_DIR", raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if "stockdb" not in p.lower()])
    monkeypatch.delitem(sys.modules, "stock_sdk", raising=False)
    monkeypatch.setitem(sdk_bootstrap._STATE, "rd", None)      # 清掉可能已缓存的 rd

    adapter = FreeStockDbIntradayAdapter(make_config(sdk_dir="G:/definitely/not/here"),
                                         reader=SdkReader(sdk_dir="G:/definitely/not/here"))
    with pytest.raises(DataSourceError) as exc:
        adapter.trends("600519")
    assert exc.value.kind == "not_installed"
    assert "sdk_dir" in exc.value.message


def test_bootstrap_loads_sdk_from_configured_dir(tmp_path):
    """预加载器要认配置里的目录，并且第二次调用走缓存（不再 import）。"""
    import sys

    from app.adapters import sdk_bootstrap

    (tmp_path / "stock_sdk.py").write_text(
        "class _Rd:\n    tag = 'fake'\n\n\nrd = _Rd()\n", encoding="utf-8")
    saved = sdk_bootstrap._STATE.get("rd")
    sdk_bootstrap._STATE.update(rd=None, dir="")
    try:
        rd = sdk_bootstrap.load(tmp_path)
        assert rd.tag == "fake"
        assert str(tmp_path) in sdk_bootstrap.loaded_dir()
        assert sdk_bootstrap.load(tmp_path) is rd          # 命中缓存
    finally:
        sdk_bootstrap._STATE.update(rd=saved, dir="")
        sys.modules.pop("stock_sdk", None)


def test_bootstrap_bypasses_shiboken_import_hook(tmp_path, monkeypatch):
    """Qt 先加载时 builtins.__import__ 被换成 __feature_import__，必须能绕过去。

    这正是「分时页空白」的第二个成因：打包后的 exe 里 PyInstaller 的 PySide6
    运行时钩子比入口脚本更早，预加载来不及，只能临时换回 importlib.__import__。
    """
    import builtins
    import sys

    from app.adapters import sdk_bootstrap

    (tmp_path / "stock_sdk.py").write_text(
        "class _Rd:\n    tag = 'bypass'\n\n\nrd = _Rd()\n", encoding="utf-8")
    real_import = builtins.__import__

    def fake_feature_import(name, *args, **kwargs):
        if name == "stockdb":
            raise ValueError("wrapper loop when unwrapping <module 'stockdb'>")
        return real_import(name, *args, **kwargs)

    fake_feature_import.__name__ = "__feature_import__"      # 模拟 shiboken 的替换
    monkeypatch.setattr(builtins, "__import__", fake_feature_import)
    saved = sdk_bootstrap._STATE.get("rd")
    sdk_bootstrap._STATE.update(rd=None, dir="")
    sys.modules.pop("stock_sdk", None)
    try:
        rd = sdk_bootstrap.load(tmp_path)
        assert rd.tag == "bypass"
        assert builtins.__import__ is fake_feature_import       # 用完必须换回去
    finally:
        sdk_bootstrap._STATE.update(rd=saved, dir="")
        sys.modules.pop("stock_sdk", None)


def test_empty_table_raises_not_found():
    with pytest.raises(DataSourceError) as exc:
        adapter_with(FakeReader()).trends("600519")
    assert exc.value.kind == "not_found"


def test_health_reports_code_count_and_sdk_dir():
    adapter = adapter_with(FakeReader(groups={"0": ["000001"], "6": ["600519", "600036"]}))
    health = adapter.health()
    assert health["ok"] is True and health["codes"] == 3


# ---------------------------------------------------------------- 兼容性
def test_downstream_feed_and_db_keep_source_flag(tmp_path):
    """端到端：适配器 → MinuteFeed → 落库 → 按 MINUTE_FLAGS 连东财一起筛出来。"""
    day = weekdays_back(1)[0]
    reader = FakeReader(minute={day: minute_rows(day, [10.0, 10.1])},
                        latest=f"分钟k:600519:{day:%Y%m%d}150000")

    trends = adapter_with(reader).trends("600519.SH")
    feed = MinuteFeed()
    series, _, _ = feed.update(trends)
    assert series and all(p.quality_flag.startswith(STOCKDB_FLAG) for p in series)

    db = Database(tmp_path / "t.db", schema_path=SCHEMA)
    db.init_schema()
    repo = Repo(db)
    repo.upsert_stocks([{"symbol": "600519.SH", "name": "贵州茅台"}])   # kline_history 的外键
    repo.upsert_bars(minute_bars_to_rows(
        trends.bars, "600519.SH",
        source=trends.source, data_level=trends.data_level, flag=trends.minute_flag))

    bars = repo.load_bars("600519.SH", "1m", 100, quality_contains=MINUTE_FLAGS)
    assert len(bars) == 2
    assert bars[0].quality_flag == STOCKDB_FLAG
    assert bars[0].data_level == "C" and bars[0].source == "stockdb:minute"

    # 采样点不在这批里：按分钟线标记筛不出采样行
    assert all(b.quality_flag != "sampled" for b in bars)


def test_feed_meta_carries_data_level_for_the_ui():
    """界面标题要显示「C 级 · free-stockdb」，等级必须能从 feed 读出来。"""
    day = weekdays_back(1)[0]
    reader = FakeReader(minute={day: minute_rows(day, [10.0])},
                        latest=f"分钟k:600519:{day:%Y%m%d}150000")
    trends = adapter_with(reader).trends("600519")
    feed = MinuteFeed()
    feed.update(trends)
    meta = feed.meta("600519")
    assert meta["data_level"] == "C"
    assert meta["source"] == "stockdb:minute"
    assert meta["minute_flag"] == STOCKDB_FLAG


def test_build_intraday_adapter_wraps_fallback():
    adapter = build_intraday_adapter(make_config())
    assert isinstance(adapter, FallbackIntradayAdapter)
    assert adapter.minute_flag == STOCKDB_FLAG

    raw = build_intraday_adapter(make_config(fallback_eastmoney=False))
    assert isinstance(raw, FreeStockDbIntradayAdapter)


def test_fallback_uses_secondary_and_then_skips_primary():
    """主源挂了：立刻走备源，冷却期内不再反复连主源。"""
    day = weekdays_back(1)[0]

    class Boom:
        minute_flag = STOCKDB_FLAG
        source_label = "坏掉的主源"
        calls = 0

        def trends(self, symbol, days=None):
            type(self).calls += 1
            raise DataSourceError(kind="network", message="未启动", retryable=True)

    class Ok:
        minute_flag = "em_minute"
        source_label = "备源"

        def trends(self, symbol, days=None):
            return Trends(symbol=symbol, day=day, bars=[], source="eastmoney:trends",
                          minute_flag="em_minute")

    boom = Boom()
    wrapper = FallbackIntradayAdapter(boom, Ok(), cooldown_sec=60)
    assert wrapper.trends("600519").minute_flag == "em_minute"
    assert boom.calls == 1
    assert wrapper.last_reason == "未启动"

    # 冷却期内：不再调用主源
    assert wrapper.trends("600519").minute_flag == "em_minute"
    assert boom.calls == 1
