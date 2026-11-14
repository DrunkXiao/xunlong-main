"""骨架冒烟测试：规范化、分档、建库、去重。"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.large_order import classify_tier, tag_direction
from app.services.tdx_bridge import ingest, normalize_symbol


def test_normalize_symbol():
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300001") == "300001.SZ"
    assert normalize_symbol("688001") == "688001.SH"
    assert normalize_symbol("600000.SH") == "600000.SH"
    assert normalize_symbol("300001.SZ") == "300001.SZ"
    assert normalize_symbol("300001.SH") is None      # 前缀与后缀冲突
    assert normalize_symbol("abc") is None
    assert normalize_symbol("60000") is None


def test_classify_tier():
    # 阈值单位是「元/分钟」：3 秒采样区间由 scale_tiers 折算成 1/20 再进来
    assert classify_tier(5_000_000) is None
    assert classify_tier(25_000_000) == "normal"
    assert classify_tier(70_000_000) == "large"
    assert classify_tier(120_000_000) == "super"
    # 单区间再大也只记「超大额成交」；连续异动必须由连续 N 个区间同向判定
    assert classify_tier(250_000_000) == "super"


def test_scale_tiers_keeps_meaning_across_spans():
    """3 秒采样区间与 1 分钟区间的门槛按跨度折算，经济含义一致。"""
    from app.services.large_order import DEFAULT_TIERS, scale_tiers

    fast = scale_tiers(DEFAULT_TIERS, 3.0)
    slow = scale_tiers(DEFAULT_TIERS, 60.0)
    assert round(fast["normal"]) == 1_000_000      # 3 秒：100 万，与阶段二一致
    assert round(slow["normal"]) == 20_000_000     # 1 分钟：2000 万
    assert round(scale_tiers(DEFAULT_TIERS, 0)["normal"]) == 20_000_000   # 跨度未知按 1 分钟


def test_tag_direction():
    assert tag_direction(900_000, 100_000) == "买入活跃"
    assert tag_direction(100_000, 900_000) == "卖出活跃"
    assert tag_direction(500_000, 500_000) == "买卖分歧"
    assert tag_direction(0, 0) == "资金行为不明确"


def test_database_init_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
        db = Database(Path(tmp) / "test.db", schema_path=schema)
        first = db.init_schema()
        second = db.init_schema()
        assert first == second
        db.seed_default_groups()
        assert "watch_groups" in db.table_names()
        assert "alert_events" in db.table_names()


def test_tdx_ingest_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
        db = Database(Path(tmp) / "test.db", schema_path=schema)
        db.init_schema()
        payload = {
            "source": "tongdaxin", "event": "watchlist_update",
            "timestamp": "2026-11-08T10:30:00",
            "symbols": [{"code": "600000", "name": "示例", "group": "龙头观察"}],
        }
        with db.cursor() as cur:
            result = ingest(cur, payload, channel="test")
        assert result == {"total": 1, "new": 1}
        with db.cursor() as cur:
            again = ingest(cur, payload, channel="test")
        assert again == {"total": 1, "new": 0}


def test_alert_cooldown():
    engine = AlertEngine(cooldown_sec=300)
    now = datetime(2026, 11, 8, 10, 0, 0)
    assert engine.should_fire("600000.SH", "r1", "near_limit_up", 11.0, now) is True
    assert engine.should_fire("600000.SH", "r1", "near_limit_up", 11.0, now) is False


# ---------------------------------------------------------------- tqcenter
def test_tq_symbol_precheck():
    """坏代码会触发 tqcenter 内部 tq.close()，必须先本地拦下。"""
    from app.adapters.tqcenter_adapter import _valid_symbol
    assert _valid_symbol("600000.SH")
    assert _valid_symbol("300001.SZ")
    assert not _valid_symbol("600000")
    assert not _valid_symbol("300001.SH")
    assert not _valid_symbol("")


def test_tq_minute_kline_declared_unsupported():
    from app.adapters.base import DataSourceError
    from app.adapters.tqcenter_adapter import TqCenterAdapter
    adapter = TqCenterAdapter()
    caps = adapter.capabilities
    assert caps.tick is False
    assert caps.minute_kline is False
    assert caps.order_book_levels == 5
    assert caps.subscribe_max == 100
    try:
        adapter.get_kline("600000.SH", "5m")
    except DataSourceError as exc:
        assert exc.kind == "unsupported"
    else:
        raise AssertionError("分钟线必须报 unsupported，不得用日线顶替")


def test_snapshot_sampler_delta():
    """快照差分：手->股、万元->元，方向按内外盘增量推演。"""
    from app.adapters.tqcenter_adapter import SnapshotSampler
    sampler = SnapshotSampler()
    t0 = datetime(2026, 11, 6, 10, 0, 0)
    first = sampler.feed("600000.SH", {
        "Now": "10.00", "Volume": "1000", "Amount": "1000.00",
        "Inside": "400", "Outside": "600", "Average": "10.00",
    }, t0)
    assert first is None, "首帧没有差分基线"
    point = sampler.feed("600000.SH", {
        "Now": "10.10", "Volume": "1300", "Amount": "1030.30",
        "Inside": "500", "Outside": "800", "Average": "10.05",
    }, t0 + timedelta(seconds=3))
    assert point.interval_volume == 30000          # 300 手 * 100
    assert round(point.interval_amount) == 303000  # 30.3 万元 -> 元
    assert point.direction == "buy"                # 外盘增量 200 > 内盘 100
    assert point.quality_flag == "sampled"
