"""东方财富分时（分钟线）真实验收：直接打接口，不造数据。

跑法：python scripts/verify_intraday_em.py [symbol]
默认用 600519.SH（贵州茅台）。落库检查用临时库 data/verify_intraday.db，跑完删除，
不污染主库 data/dragon_hunter.db。
"""
from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.base import DataSourceError                       # noqa: E402
from app.adapters.eastmoney_intraday import (                        # noqa: E402
    EastmoneyIntradayAdapter, secid,
)
from app.config import load_config                                   # noqa: E402
from app.db.repo import Repo                                         # noqa: E402
from app.db.session import Database                                  # noqa: E402
from app.services.intraday import (                                   # noqa: E402
    MinuteFeed, minute_bars_to_rows, minute_slot_of, points_from_minute_bars,
    points_from_sampled_bars,
)
from app.services.large_order import LargeOrderDetector               # noqa: E402

VERIFY_DB = ROOT / "data" / "verify_intraday.db"
SCHEMA = ROOT / "app" / "db" / "schema.sql"
RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
    cfg = load_config(str(ROOT / "config.yaml"))
    adapter = EastmoneyIntradayAdapter(cfg)

    print(f"=== 东方财富分时验收 · {symbol} ===")
    print(f"secid = {secid(symbol)}")

    try:
        trends = adapter.trends(symbol)
    except DataSourceError as exc:
        print(f"取数失败：{exc.kind} {exc.message}")
        return 2

    bars = trends.bars
    first, last = bars[0], bars[-1]
    print(f"名称 {trends.name} · 昨收 {trends.pre_close} · {trends.day} · {len(bars)} 个分钟点")
    print(f"首 {first.time:%H:%M} 收 {first.close} 量 {first.volume} 股 额 {first.amount:.0f}")
    print(f"末 {last.time:%H:%M} 收 {last.close} 量 {last.volume} 股 额 {last.amount:.0f}")

    check(len(bars) == 241, "分钟点数量为 241",
          f"实际 {len(bars)}，09:30–11:30 121 个 + 13:00–15:00 120 个")
    check(first.time.time() == time(9, 30) and last.time.time() == time(15, 0),
          "首末时点为 09:30 / 15:00", f"{first.time:%H:%M} → {last.time:%H:%M}")
    check(trends.day == last.time.date(), "整日序列同日", str(trends.day))

    # 手 → 股：量与额必须自洽（|amount/(volume×price) − 1| 很小）
    ratios = [b.amount / (b.volume * b.close) for b in bars if b.volume and b.close]
    worst = max(abs(r - 1) for r in ratios) if ratios else 1
    check(bool(ratios) and worst < 0.02, "成交量手→股换算与成交额自洽",
          f"{len(ratios)} 个有量分钟，最大偏差 {worst * 100:.2f}%（>2% 说明单位换错）")

    # 均价用供应商字段，且与累计口径接近
    vwap = trends.total_amount / trends.total_volume if trends.total_volume else 0
    check(last.avg_price is not None and abs(vwap - last.avg_price) / last.avg_price < 0.005,
          "末点均价 = 供应商均价（与累计口径一致）",
          f"供应商 {last.avg_price:.3f} · 累计 {vwap:.3f}")

    points = points_from_minute_bars(bars, symbol, trends.pre_close)
    # 午休那一段跨度是真跨不过去的（11:30 → 13:01 共 91 分钟），必须如实记，
    # 否则预警按「区间均值」折算时会把一个半天的量当成 1 分钟。
    lunch = [p for p in points[1:] if p.time.time() == time(13, 1)]
    span_bad = [p for p in points[1:]
                if p.interval_span_sec != 60 and p not in lunch]
    check(not span_bad, "除午休外区间跨度固定 60 秒",
          f"异常 {len(span_bad)} 个；午休跨度 {lunch[0].interval_span_sec / 60:.0f} 分钟" if lunch
          else f"异常 {len(span_bad)} 个")
    check(points[-1].cumulative_volume == trends.total_volume,
          "累计量与逐分钟加总一致",
          f"{points[-1].cumulative_volume} vs {trends.total_volume}")
    check(all("em_minute" in p.quality_flag for p in points),
          "每个点都标 em_minute", points[0].quality_flag)
    check(any("no_trade" in p.quality_flag for p in points) or
          all(p.interval_volume > 0 for p in points),
          "零成交分钟标 no_trade（与快照的 no_update 区分）",
          f"零量分钟 {sum(1 for p in points if p.interval_volume == 0)} 个")

    slots = [minute_slot_of(p.time) for p in points]
    check(slots[0] == 0 and slots[-1] == 240 and slots == sorted(slots),
          "241 槽时间轴单调映射", f"首槽 {slots[0]} 末槽 {slots[-1]}")

    # 合并 / 水位
    feed = MinuteFeed()
    series, fresh, replay = feed.update(trends)
    check(len(series) == 241 and fresh == [] and len(replay) == 240,
          "首载只补记录、不补告警", f"序列 {len(series)} · 可告警 {len(fresh)} · 回放 {len(replay)}")
    _, fresh2, replay2 = feed.update(trends)
    check(fresh2 == [] and replay2 == [], "重复拉取不产生新点", "同一份数据第二次不判定")

    # 落库幂等（临时库）
    if VERIFY_DB.exists():
        VERIFY_DB.unlink()
    db = Database(VERIFY_DB, schema_path=SCHEMA)
    db.init_schema()
    repo = Repo(db)
    repo.upsert_stocks([{"symbol": symbol, "name": trends.name}])
    rows = minute_bars_to_rows(bars, symbol)
    first_write = repo.upsert_bars(rows)
    second_write = repo.upsert_bars(rows)
    stored = repo.load_bars(symbol, "1m", 300)
    check(len(stored) == 241 and second_write == 241 and len(stored) == 241,
          "分钟线落库幂等", f"写入 {first_write}/{second_write}，库内 {len(stored)} 行")
    check(stored[0].quality_flag == "em_minute" and stored[0].source == "eastmoney:trends",
          "库内保留来源与质量标记", f"{stored[0].source} / {stored[0].quality_flag}")
    restored = points_from_sampled_bars(stored)
    same_dir = sum(1 for a, b in zip(points, restored) if a.direction == b.direction)
    check(same_dir >= len(points) - 2, "库内还原的方向与线上一致",
          f"{same_dir}/{len(points)} 相同（首点无前值，允许不同）")

    # 大单：按 1 分钟门槛判档
    detector = LargeOrderDetector(
        tiers=(cfg.get("large_order") or {}).get("tiers"),
        streak_count=int((cfg.get("large_order") or {}).get("streak_count", 3)),
        streak_multiplier=float((cfg.get("large_order") or {}).get("streak_amount_multiplier", 2)),
    )
    counts: dict[str, int] = {}
    for point in points[1:]:
        for order in detector.feed(point, "B"):
            counts[order.tier] = counts.get(order.tier, 0) + 1
    total = sum(counts.values())
    check(total > 0, "分钟线能产出区间大单记录",
          f"{total} 条 · {counts}（门槛 {detector.tiers['normal'] / 1e4:.0f}万/分钟起）")
    check(all(o.source == "eastmoney:trends" for o in detector.orders(symbol, 400)),
          "大单来源记为 eastmoney:trends", detector.orders(symbol, 1)[0].source if total else "-")
    check(all(o.tier in ("normal", "large", "super", "continuous")
              for o in detector.orders(symbol, 400)), "档位只取四档",
          str(sorted({o.tier for o in detector.orders(symbol, 400)})))

    # 非法代码
    try:
        adapter.trends("999999.SH")
        check(False, "非法代码抛错而非空表", "没有抛异常")
    except DataSourceError as exc:
        check(exc.retryable is False, "非法代码抛错而非空表", f"{exc.kind}: {exc.message[:40]}")

    db.close() if hasattr(db, "close") else None
    if VERIFY_DB.exists():
        VERIFY_DB.unlink()

    print("\n==== 汇总 ====")
    passed = 0
    for ok, name, detail in RESULTS:
        print(f"{'通过' if ok else '失败'}\t{name}\t{detail}")
        passed += ok
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
