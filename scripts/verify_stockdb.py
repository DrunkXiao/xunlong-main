"""free-stockdb 分时源实机验证：不启动界面，直接验「SDK → 取数 → 解析 → 单位 → 界面数据」。

用法：
    python scripts/verify_stockdb.py                      # 用 config.yaml，验 600519.SH
    python scripts/verify_stockdb.py --symbol 000001.SZ
    python scripts/verify_stockdb.py --sdk-dir G:/stock/stockdb/pybao
    python scripts/verify_stockdb.py --raw                # 打印 SDK 返回的原始记录

检查项与失败含义：
    1. SDK 可导入      → 失败说明 sdk_dir 不对，或 stockdb.pyd 位数/Python 版本不匹配
    2. 引擎可达        → 读不到股票代码表说明 stockdb.exe 没启动，或没同步过数据
    3. 分钟k 有数据    → 空说明这只票/这一天没同步，或代码不在库中
    4. 字段与单位对账  → amount/volume/close 应≈1（股）；≈0.01 说明单位是手
    5. 均价递推        → 末根均价应落在当日价格区间内
    6. 界面数据链路    → 适配器 → MinuteFeed 产出的点数与首末时间，就是分时图上画的东西
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.base import DataSourceError                # noqa: E402
from app.adapters.free_stockdb_intraday import (             # noqa: E402
    FreeStockDbIntradayAdapter, stockdb_code,
)
from app.config import load_config                           # noqa: E402
from app.services.intraday import MinuteFeed                 # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 free-stockdb 分时数据源")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", default="600519.SH")
    parser.add_argument("--sdk-dir", default="", help="覆盖 intraday.free_stockdb.sdk_dir")
    parser.add_argument("--raw", action="store_true", help="打印 SDK 返回的原始记录样本")
    args = parser.parse_args()

    config = load_config(ROOT / args.config)
    fcfg = dict(((config.get("intraday") or {}).get("free_stockdb") or {}))
    if args.sdk_dir:
        fcfg["sdk_dir"] = args.sdk_dir
    config["intraday"] = {"free_stockdb": fcfg}
    adapter = FreeStockDbIntradayAdapter(config)

    print(f"目标服务: {adapter.base_url}  |  配置的 sdk_dir: {adapter.sdk_dir or '（未配置，走默认搜索）'}")
    print(f"分钟表名: {adapter.table}  | 回溯天数: {adapter.backfill_days}  | "
          f"量的单位: {'手' if adapter.volume_in_hand else '股'}")

    # 1) 探活（SDK 导入 + 股票代码表）
    try:
        health = adapter.health()
    except DataSourceError as exc:
        label = "SDK 可导入" if exc.kind != "not_installed" else "SDK 导入"
        print(f"[1/6] {label}      失败  [{exc.kind}] {exc.message}")
        return 2
    print(f"[1/6] SDK 可导入      通过  {health['sdk_dir']}")
    print(f"[2/6] 引擎可达        通过  股票代码 {health['codes']} 个")

    if args.raw:
        from datetime import date
        try:
            rows = adapter.reader.minute_rows(stockdb_code(args.symbol), date.today())
            print(f"      原始记录样本（今天前 2 条）: {rows[:2]}")
        except DataSourceError as exc:
            print(f"      原始记录取样失败: {exc.message}")

    # 3) 取分时
    try:
        trends = adapter.trends(args.symbol)
    except DataSourceError as exc:
        print(f"[3/6] 分钟k 取数     失败  [{exc.kind}] {exc.message}")
        return 3
    print(f"[3/6] 分钟k 取数     通过  {trends.day} 共 {len(trends.bars)} 根，"
          f"来源 {trends.source} / 等级 {trends.data_level}")
    if trends.name or trends.pre_close is not None:
        print(f"      名称 {trends.name or '—'}  昨收 {trends.pre_close}（取自 日k 表）")

    first, last = trends.bars[0], trends.bars[-1]
    print(f"[4/6] 首/末根          {first.time:%H:%M} 收 {first.close} 量 {first.volume} 股  |  "
          f"{last.time:%H:%M} 收 {last.close} 量 {last.volume} 股")

    sample = next((b for b in trends.bars if b.volume and b.amount), None)
    if sample is None:
        print("[5/6] 单位对账         跳过  当日全是零成交分钟")
    else:
        implied = sample.amount / sample.volume
        ratio = implied / sample.close if sample.close else 0
        verdict = "通过（volume 是股）" if 0.9 <= ratio <= 1.1 else \
                  f"可疑（amount/volume={implied:.4f}，收价 {sample.close}）"
        print(f"[5/6] 单位对账         {verdict}")
        if ratio < 0.05:
            print("      → 数值差 100 倍，库里的 volume 可能是手，"
                  "请把 intraday.free_stockdb.volume_unit 设为 hand")

    if last.avg_price:
        ok = min(b.low for b in trends.bars) <= last.avg_price <= max(b.high for b in trends.bars)
        manual = trends.total_amount / trends.total_volume if trends.total_volume else 0
        print(f"      均价递推         {'通过' if ok else '可疑'}  "
              f"末根均价 {last.avg_price:.3f}，全天 VWAP {manual:.3f}")

    # 6) 界面链路：MinuteFeed 就是分时图的数据源
    feed = MinuteFeed(flag=trends.minute_flag)
    series, fresh, replay = feed.update(trends)
    print(f"[6/6] 界面数据链路    通过  分时点 {len(series)} 个"
          f"（首载回放 {len(replay)} 个，可判定 {len(fresh)} 个），"
          f"首点 {series[0].time:%H:%M} 末点 {series[-1].time:%H:%M}")
    meta = feed.meta(args.symbol)
    slots = f"{len(series)}/241" if len(series) <= 241 else f"{len(series)} 点"
    print(f"      界面标题应为: {meta.get('name') or args.symbol} · "
          f"free-stockdb 本地库 1 分钟（非逐笔） · {slots} · 等级 {meta.get('data_level')}")

    print("\n结论: free-stockdb 分时源可用，单股精盯的分时页有数据可画。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
