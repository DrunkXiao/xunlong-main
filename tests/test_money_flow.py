"""资金流（主力净额）适配器验收测试：注入假 reader，不联网、不依赖引擎。

覆盖：
- `资金流` 记录 → MoneyFlowRow 的字段映射（尤其是 main_net 不再二次加工）
- 脏数据只丢当天，不影响整段；同日重复记录后者覆盖
- 服务端切片：请求多少条由 days 决定，不在 Python 里全量下载再切
- 缓存：同一只票在 TTL 内不重复请求；cached() 能给切周期回来的场景用
- 空表 → not_found（数据没同步）；引擎连不上 → network（引擎没起）；
  配置关闭 → disabled。三种情况必须分开，否则用户不知道该修哪里
- 日期解析：8 位以外一律判非法，不猜

为什么不用假 HTTP 服务：这个表是 stockdb 本地库，读法由 `rd` 的 K-V 语义定义，
跟传输层无关；把测试锁在 reader 上，才能覆盖「引擎根本没起」这类真实故障。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.adapters.base import DataSourceError
from app.adapters.money_flow import (
    MoneyFlowAdapter, MoneyFlowRow, SdkReader, parse_day, rows_from_records,
    stockdb_code,
)


# ---------------------------------------------------------------- 测试工具
def record(day: int, main_net: float, **extra) -> dict:
    """一条引擎原始记录（字段与实测回包一致）。"""
    row = {"code": "600519", "date": day, "main_net": main_net,
           "jumbo_net": 0.0, "big_net": 0.0, "mid_net": 0.0, "small_net": 0.0}
    row.update(extra)
    return row


class FakeReader:
    """假 reader：记录每次调用参数，用来断言「请求了几次、要了多少条」。"""

    def __init__(self, records=None, error: Exception | None = None) -> None:
        self._records = list(records or [])
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def records(self, code: str, limit: int) -> list[dict]:
        self.calls.append((code, limit))
        if self._error is not None:
            raise self._error
        return self._records[-limit:]


def make_config(**cfg) -> dict:
    return {
        "money_flow": {"enabled": True, "days": 120, "table": "资金流",
                       "cache_sec": 0, **cfg},
        "intraday": {"free_stockdb": {"sdk_dir": "G:/stock/stockdb/pybao"}},
    }


def adapter_with(reader, **cfg) -> MoneyFlowAdapter:
    return MoneyFlowAdapter(make_config(**cfg), reader=reader)


# ---------------------------------------------------------------- 字段解析
def test_parse_day_accepts_only_8_digits():
    assert parse_day(20261113) == date(2026, 11, 13)
    assert parse_day("20261113") == date(2026, 11, 13)
    assert parse_day(20261113.0) == date(2026, 11, 13)
    # 14 位时间戳不是日K键，宁可丢也不猜（猜错会挂到错误的那根K线上）
    assert parse_day("20261113150000") is None
    assert parse_day("2026-11-13") is None
    assert parse_day(None) is None
    assert parse_day("") is None


def test_rows_map_fields_without_recomputing_main_net():
    """main_net 是数据源给的权威值：实测 main_net == jumbo + big ± 1 元舍入。

    这里故意给一组「jumbo+big != main_net」的数：适配器必须原样用 main_net，
    不能自己相加覆盖掉官方口径（否则与行情软件对不上账）。
    """
    rows = rows_from_records([
        record(20261113, 287884467.0, jumbo_net=301860752.0, big_net=-13976286.0,
               mid_net=-287630024.0, small_net=-254443.0),
        record(20261112, -315487438.0),
    ])
    assert [r.day for r in rows] == [date(2026, 11, 12), date(2026, 11, 13)]
    latest = rows[-1]
    assert latest.main_net == 287884467.0
    assert latest.jumbo_net == 301860752.0 and latest.big_net == -13976286.0
    assert latest.mid_net == -287630024.0 and latest.small_net == -254443.0
    assert latest.key == "2026-11-13"      # 与 K 线 bar_time 的对齐键


def test_rows_skip_dirty_but_keep_the_rest():
    """脏数据只丢当天：一个坏字段不该让整只票的资金流消失。"""
    rows = rows_from_records([
        record(20261110, 1000.0),
        {"code": "600519", "date": "bad", "main_net": 1.0},     # 日期非法
        {"code": "600519", "date": 20261111},                    # 缺主力净额
        {"code": "600519", "date": 20261111, "main_net": "-"},    # 缺主力净额（占位符）
        record(20261112, -2000.0, mid_net=None),                 # 单类净额缺失补 0
        "not-a-dict",
    ])
    assert [r.day for r in rows] == [date(2026, 11, 10), date(2026, 11, 12)]
    assert rows[-1].mid_net == 0.0
    assert rows[-1].main_net == -2000.0


def test_same_day_later_record_wins():
    rows = rows_from_records([
        record(20261113, 100.0),
        record(20261113, 999.0),      # 同日重复：以后到的为准（入库覆盖语义）
    ])
    assert len(rows) == 1 and rows[0].main_net == 999.0


def test_stockdb_code_strips_exchange_suffix():
    assert stockdb_code("600519.SH") == "600519"
    assert stockdb_code("000001.sz") == "000001"
    assert stockdb_code("sh600519") == "600519"
    assert stockdb_code("600519") == "600519"


# ---------------------------------------------------------------- 取数
def test_series_requests_only_configured_limit():
    """服务端切片：days=20 就要 20 条，不能在 Python 里全量下载再切。"""
    records = [record(20260101 + i, float(i)) for i in range(30)]
    reader = FakeReader(records)
    rows = adapter_with(reader, days=20).series("600519.SH")
    assert len(rows) == 20
    assert reader.calls == [("600519", 20)]
    assert rows[-1].main_net == 29.0          # 取的是最近的那批


def test_series_reuses_cache_within_ttl():
    reader = FakeReader([record(20261113, 1.0)])
    adapter = adapter_with(reader, cache_sec=60)
    adapter.series("600519.SH")
    adapter.series("600519.SH")
    assert len(reader.calls) == 1             # 第二次走缓存，不重复打引擎
    assert adapter.cached("600519.SH")[-1].main_net == 1.0
    adapter.clear_cache()
    assert adapter.cached("600519.SH") == []


def test_cache_ttl_zero_always_refetches():
    reader = FakeReader([record(20261113, 1.0)])
    adapter = adapter_with(reader, cache_sec=0)
    adapter.series("600519.SH")
    adapter.series("600519.SH")
    assert len(reader.calls) == 2


def test_empty_table_raises_not_found_with_actionable_text():
    """库里有表但这只票没记录：说清是「没同步/新股/退市」，别说成网络故障。"""
    adapter = adapter_with(FakeReader([]))
    with pytest.raises(DataSourceError) as exc:
        adapter.series("600519")
    assert exc.value.kind == "not_found"
    assert "资金流" in exc.value.message


def test_engine_down_raises_network():
    adapter = adapter_with(FakeReader(error=ConnectionRefusedError("拒绝连接")))
    with pytest.raises(DataSourceError) as exc:
        adapter.series("600519")
    assert exc.value.kind == "network"
    assert "stockdb.exe" in exc.value.message


def test_disabled_provider_says_where_to_turn_it_on():
    adapter = adapter_with(FakeReader([record(20261113, 1.0)]), enabled=False)
    with pytest.raises(DataSourceError) as exc:
        adapter.series("600519")
    assert exc.value.kind == "disabled"
    assert "money_flow.enabled" in exc.value.message


def test_latest_returns_none_instead_of_raising():
    """图表只想问「最新多少」时不该抛：取不到就是没有，让它画空状态。"""
    assert adapter_with(FakeReader([])).latest("600519") is None
    ok = adapter_with(FakeReader([record(20261113, 5.0)]))
    assert ok.latest("600519").main_net == 5.0


# ---------------------------------------------------------------- 真实 SDK 边界
def test_sdk_reader_translates_oserror_to_network(monkeypatch):
    """SdkReader 要把底层传输异常统一翻成 network，别让上层看见随机异常类型。"""
    reader = SdkReader("")

    class Boom:
        def vals(self, *a, **kw):
            raise OSError("引擎没启动")

    monkeypatch.setattr("app.adapters.money_flow.sdk_bootstrap.load", lambda *a, **kw: Boom())
    with pytest.raises(DataSourceError) as exc:
        reader.records("600519", 10)
    assert exc.value.kind == "network"
    assert exc.value.retryable is True


def test_sdk_reader_rejects_non_list_payload(monkeypatch):
    """回包不是列表说明协议对不上，必须报出来，不能当成「没数据」。"""
    reader = SdkReader("")

    class Weird:
        def vals(self, *a, **kw):
            class Q:
                def __getitem__(self, item):
                    return self

                def do(self):
                    return {"unexpected": True}
            return Q()

    monkeypatch.setattr("app.adapters.money_flow.sdk_bootstrap.load", lambda *a, **kw: Weird())
    with pytest.raises(DataSourceError) as exc:
        reader.records("600519", 10)
    assert exc.value.kind == "bad_payload"


def test_money_flow_row_key_matches_bar_time_format():
    """副图靠这个键对齐 K 线：必须是 `YYYY-MM-DD`，与 strftime 完全一致。"""
    row = MoneyFlowRow(day=date(2026, 11, 13), main_net=1.0)
    assert row.key == date(2026, 11, 13).strftime("%Y-%m-%d")
