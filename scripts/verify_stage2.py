"""第二阶段验收脚本：采样成交、区间大单、三向联动、量能预警。

运行：cd F:/workcn/寻龙盯盘 && python scripts/verify_stage2.py

验收项（与 SKILL.md 第二阶段一致）：
  1. 采样区间量额 == 两次快照差分（真实 tqcenter 数据）
  2. 采样点间隔与轮询配置一致
  3. 无新增成交标 no_update，不产零成交点
  4. 逐笔能力如实声明为不支持（界面据此显示原因，不渲染空壳）
  5. 区间大单四档分档正确
  6. 事件标签只能取六种，且不含身份结论
  7. 连续异动只在成段时报一次
  8. 价格位置来自近 20 日真实区间
  9. 事件落库幂等、可按交易日聚合给 K 线画标记
 10. 量能预警触发一次后进入冷却，同值不重复
 11. 分时时间槽映射（多股共享时间轴用同一套坐标）

注意：3 之后的「构造用例」用合成采样点验证判定逻辑，不冒充行情。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters import build_adapter
from app.config import load_config, sqlite_path_from_url
from app.db.repo import Repo
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.alert_service import AlertService
from app.services.intraday import IntradaySampler
from app.services.large_order import ALLOWED_TAGS, LargeOrderDetector, LargeOrder

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []

BANNED_WORDS = ("主力", "机构", "庄家", "建仓", "出货", "游资", "接盘")
VERIFY_SOURCE = "verify_stage2:sampled"


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


def make_point(seconds: int, amount: float, direction: str = "buy",
               price: float = 10.0, symbol: str = "600519.SH",
               quality: str = "sampled") -> "object":
    from app.services.intraday import SamplePoint
    return SamplePoint(
        symbol=symbol, time=datetime(2026, 11, 6, 9, 30) + timedelta(seconds=seconds),
        price=price, avg_price=price,
        interval_volume=int(amount / max(price, 0.01)), interval_amount=amount,
        cumulative_volume=0, cumulative_amount=0, direction=direction,
        quality_flag=quality, interval_span_sec=3.0,
    )


def main() -> int:
    config = load_config(ROOT / "config.yaml")
    db_path = sqlite_path_from_url(config["database"]["url"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db = Database(db_path)
    db.init_schema()
    db.seed_default_groups()
    repo = Repo(db)
    adapter = build_adapter(config)
    caps = adapter.capabilities
    poll = float((config.get("datasource") or {}).get("poll_interval_sec", 3))

    print(f"数据库：{db_path}")
    print(f"数据源：{config['datasource']['name']} · {caps.data_level} 级 · {caps.summary()}\n")

    symbol = "600519.SH"
    alert_before = repo.count_alerts()
    orders_before = repo.count_large_orders()
    rule_ids: list[int] = []

    try:
        # ---------------------------------------------------- 1/2/3 真实数据差分
        sampler = IntradaySampler()
        first = adapter.get_quote(symbol)
        t1 = datetime.now()
        assert sampler.feed(first, t1) is None
        time.sleep(max(1.0, poll))
        second = adapter.get_quote(symbol)
        t2 = datetime.now()
        point = sampler.feed(second, t2)
        interval_span = point.interval_span_sec
        d_volume = (second.volume or 0) - (first.volume or 0)
        d_amount = (second.amount or 0.0) - (first.amount or 0.0)
        record("采样区间量额 = 快照差分",
               point.interval_volume == d_volume and abs(point.interval_amount - d_amount) < 1.0,
               f"区间量 {point.interval_volume} 股 / 区间额 {point.interval_amount:.0f} 元，"
               f"与快照差一致 {d_volume} 股 / {d_amount:.0f} 元")
        record("采样间隔与轮询配置一致", abs(interval_span - (t2 - t1).total_seconds()) < 0.5,
               f"实测 {interval_span:.2f}s（配置 {poll:.0f}s），质量标记 {point.quality_flag}")
        no_update_ok = (point.interval_volume > 0) or ("no_update" in point.quality_flag)
        record("无新增成交不产零成交点", no_update_ok,
               f"区间量 {point.interval_volume}，标记 {point.quality_flag}"
               + ("（当前非交易时段，差分应为 0 并标 no_update）" if point.interval_volume == 0 else ""))

        # ---------------------------------------------------- 4 逐笔能力声明
        record("逐笔能力如实声明为不支持",
               caps.tick is False and caps.minute_kline is False,
               f"逐笔={'支持' if caps.tick else '不支持'}，分钟K={'支持' if caps.minute_kline else '不支持'}"
               " → 界面第三页签写「逐笔(不支持)」并给出替代口径")

        # ---------------------------------------------------- 5 四档分档
        detector = LargeOrderDetector()
        tiers = [("normal", 1_200_000), ("large", 3_400_000), ("super", 5_600_000)]
        got = []
        for idx, (expected, amount) in enumerate(tiers):
            events = detector.feed(make_point(idx * 3 + 3, amount, "buy"))
            got.append(events[0].tier if events else None)
        record("四档分档（区间口径）", got == ["normal", "large", "super"],
               "100万→普通 / 300万→大额 / 500万→超大额，" f"实测 {got}")

        # ---------------------------------------------------- 6 标签合规
        seq = [(30, 1_500_000, "buy", 10.00), (33, 2_000_000, "sell", 9.98),
               (36, 6_000_000, "buy", 10.02), (39, 1_100_000, "unknown", 10.01)]
        tags = []
        for seconds, amount, direction, price in seq:
            for event in detector.feed(make_point(seconds, amount, direction, price)):
                tags.append(event.tag)
        bad = [tag for tag in tags
               if tag not in ALLOWED_TAGS or any(word in tag for word in BANNED_WORDS)]
        record("标签只取六种且无身份推断", bool(tags) and not bad, f"产出标签 {tags}")

        # ---------------------------------------------------- 7 连续异动
        streak = LargeOrderDetector(streak_count=3, streak_multiplier=2.0)
        streak.feed(make_point(3, 4_000_000, "buy"))
        streak.feed(make_point(6, 4_000_000, "buy"))
        third = streak.feed(make_point(9, 4_000_000, "buy"))
        fourth = streak.feed(make_point(12, 4_000_000, "buy"))
        record("连续异动只报一次",
               third and third[0].tier == "continuous"
               and all(event.tier != "continuous" for event in fourth),
               f"累计 {third[0].amount / 1e4:.0f}万、跨度 {third[0].window_sec:.0f}s，"
               f"第 4 个区间不再重复报连续")

        # ---------------------------------------------------- 8 价格位置（真实日K）
        bars = adapter.get_kline(symbol, "1d", None, None, 40)
        live = adapter.get_quote(symbol)
        if bars and live.price:
            detector.set_daily_range_from_bars(symbol, bars, 20)
            position = detector.price_position(symbol, live.price)
            window = bars[-20:]
            record("价格位置取真实近 20 日区间", position != "unknown",
                   f"区间 {min(b.low for b in window):.2f}~{max(b.high for b in window):.2f}，"
                   f"现价 {live.price:.2f} → {position}")
        else:
            record("价格位置取真实近 20 日区间", False, "日K或现价缺失")

        # ---------------------------------------------------- 9 落库与标记
        event = LargeOrder(symbol=symbol, time=datetime.now(), tier="super",
                           direction="buy", price=live.price or 0.0, volume=500_000,
                           amount=6_000_000.0, threshold=5_000_000.0,
                           price_position="mid", tag="买入活跃", source=VERIFY_SOURCE)
        first_write = repo.insert_large_orders([event])
        second_write = repo.insert_large_orders([event])
        marks = repo.large_orders_by_date(symbol, 3650)
        day = event.time.strftime("%Y-%m-%d")
        record("大单落库幂等且可按日聚合", first_write == 1 and second_write == 0
               and marks.get(day, {}).get("count") == 1,
               f"首次写 {first_write} 行、重复写 {second_write} 行，"
               f"{day} 聚合 {marks.get(day)}")

        # ---------------------------------------------------- 10 量能预警冷却
        service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
        rule_ids.append(service.create_intraday_rule(symbol, "amount_spike", 3, cooldown_sec=300))
        series = [make_point(i * 3, 10_000.0, symbol=symbol) for i in range(1, 21)]
        series.append(make_point(63, 150_000.0, symbol=symbol))
        fired = service.evaluate_intraday({symbol: series}, trading=True)
        again = service.evaluate_intraday({symbol: series}, trading=True)
        record("量能预警触发一次后进入冷却", len(fired) == 1 and again == [],
               f"首次 {len(fired)} 条、再次 {len(again)} 条，"
               f"条件：{fired[0].event.condition if fired else '无'}")

        # ---------------------------------------------------- 11 时间槽映射
        from app.ui.charts import intraday_slot
        base_day = datetime(2026, 11, 6)
        slots = [
            intraday_slot(base_day.replace(hour=9, minute=30)),
            intraday_slot(base_day.replace(hour=11, minute=30)),
            intraday_slot(base_day.replace(hour=13, minute=0)),
            intraday_slot(base_day.replace(hour=15, minute=0)),
        ]
        record("分时时间槽映射（多股共享时间轴）", slots == [0, 120, 120, 240],
               f"09:30/11:30/13:00/15:00 → {slots}（241 槽，午休压平）")

    finally:
        # 清理：只删本次验收造的规则、事件与大单
        for rule_id in rule_ids:
            repo.delete_rule(rule_id)
        with db.cursor() as cur:
            cur.execute("DELETE FROM large_order_events WHERE source = ?", (VERIFY_SOURCE,))
            extra = repo.count_alerts() - alert_before
            if extra > 0:
                cur.execute(
                    "DELETE FROM alert_events WHERE id IN "
                    "(SELECT id FROM alert_events ORDER BY id DESC LIMIT ?)", (extra,))
        print(f"\n已清理验收用规则/事件/大单（大单表 {orders_before} → {repo.count_large_orders()}）")

    print("\n==== 汇总 ====")
    ok_count = sum(1 for _, status, _ in results if status == PASS)
    for name, status, detail in results:
        print(f"{status}\t{name}\t{detail}")
    print(f"\n{ok_count}/{len(results)} 项通过")
    try:
        adapter.disconnect()
    except Exception:
        pass
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
