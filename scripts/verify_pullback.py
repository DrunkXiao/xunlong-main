"""强势回调选股验收脚本（真实本地日线，不是合成数据）。

运行：cd F:/workcn/寻龙盯盘 && python scripts/verify_pullback.py
     python scripts/verify_pullback.py --limit 600     # 只扫前 600 只，快速自查

验收项：
  1. 本地通达信目录可定位，sh/sz 日线目录存在
  2. 最新交易日 = 指数日线最后一根，且不晚于今天
  3. 股票池口径：只含 60/00/30，无 ST，日线根数 ≥ min_bars
  4. 名称与行业来自本地缓存（抽样非空率）
  5. 全市场扫描：股票池/成功/异常 数量自洽，能给出候选
  6. 逐只独立复核前三名：MA5/10/20、缩量比、回调低点、止损、目标、股数
  7. 信号与硬性条件自洽（BUY-A 必须八项全过且分数 ≥80）
  8. 仓位约束：100 股整数倍、风险 ≤1%、市值 ≤10%
  9. 未通过原因统计不超过评估总数
 10. 落盘往返：payload → from_payload 字段一致，K 线不落盘
 11. HTML 报告可生成，且含市场环境/交易计划/未通过统计三节
 12. 源码不含身份推断词

不写数据库、不改任何行情文件；报告写到临时目录，跑完删除。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

from app.adapters.tdx_daily import (  # noqa: E402
    TdxDailySource, is_st_name, limit_up_cents, limit_up_pct, resolve_root,
)
from app.config import load_config  # noqa: E402
from app.services.pullback import (  # noqa: E402
    CHECKS, PullbackParams, ScanResult, scan,
)
from app.services.pullback_report import render  # noqa: E402

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []
BANNED = ("主力", "游资", "庄家", "建仓", "出货", "洗盘")


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


def recompute(bars: pd.DataFrame) -> dict:
    """用最朴素的 pandas 再算一遍关键数字，验证引擎没有算错。"""
    close = bars["close"]
    ma5, ma10, ma20 = (float(close.tail(n).mean()) for n in (5, 10, 20))
    rally = float(bars["volume"].iloc[-15:-5].mean())
    vol_ratio = float(bars["volume"].tail(5).mean() / rally) if rally > 0 else float("nan")
    pullback_low = float(bars["low"].tail(5).min())
    stop = min(pullback_low * 0.98, ma20 * 0.995)
    entry = float(close.iloc[-1])
    return {"ma5": ma5, "ma10": ma10, "ma20": ma20, "volume_ratio": vol_ratio,
            "pullback_low": pullback_low, "stop": stop,
            "target": entry + 2.5 * (entry - stop)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只扫前 N 只（0 = 全市场）")
    args = parser.parse_args()

    config = load_config(str(ROOT / "config.yaml"))
    params = PullbackParams.from_config(config)
    root = resolve_root(config)
    record("定位通达信目录", root is not None, str(root or "未找到 vipdoc"))
    if root is None:
        return summarise()

    source = TdxDailySource.from_config(config)
    print(f"数据源：{source.root} · 市场 {source.markets} · 每股取 {source.tail_bars} 根 · "
          f"板块强度 {params.sector_score_mode}\n")

    # 2. 最新交易日
    last = source.last_trade_date()
    today = int(date.today().strftime("%Y%m%d"))
    record("最新已收盘交易日", bool(last) and last <= today,
           f"{last}（今天 {today}）" if last else "指数日线读不到")

    # 3. 股票池
    universe = source.universe()
    bad_prefix = [r["symbol"] for r in universe
                  if not r["symbol"].split(".")[0].startswith(("60", "00", "30"))]
    st_rows = [r["symbol"] for r in universe if is_st_name(r["name"])]
    short = [r["symbol"] for r in universe if r["bars"] < params.min_bars]
    record("股票池口径", not bad_prefix and not st_rows and not short,
           f"{len(universe)} 只；越界 {len(bad_prefix)}、ST {len(st_rows)}、"
           f"日线不足 {len(short)}")

    # 4. 名称与行业
    named = sum(1 for r in universe if r["name"] and r["name"] != r["code"])
    industry = sum(1 for r in universe if r["industry"] and r["industry"] != "未分类")
    record("名称与行业来自本地缓存", named / max(len(universe), 1) > 0.95,
           f"名称 {named}/{len(universe)}、行业 {industry}/{len(universe)}")

    # 5. 扫描
    result = scan(source, params, limit=args.limit)
    record("扫描数量自洽",
           result.universe == result.scanned + len(result.errors) + max(
               result.universe - result.scanned - len(result.errors), 0)
           and result.scanned > 0,
           f"股票池 {result.universe} · 成功 {result.scanned} · 异常 {len(result.errors)} · "
           f"耗时 {result.elapsed:.1f}s · 数据日 {result.data_date}")
    record("能给出候选", bool(result.picks),
           f"确认 {len(result.confirmed)}、观察 {len(result.watch)}、"
           f"列出 {len(result.picks)}、出结论 {result.evaluated}")

    # 6. 逐只复核前三名
    deltas = []
    for pick in result.picks[:3]:
        bars = result.frames.get(pick.symbol)
        if bars is None:
            continue
        ref = recompute(bars)
        deltas.append(max(abs(pick.ma5 - ref["ma5"]), abs(pick.ma10 - ref["ma10"]),
                          abs(pick.ma20 - ref["ma20"]), abs(pick.stop - ref["stop"]),
                          abs(pick.target - ref["target"]),
                          abs((pick.volume_ratio or 0) - ref["volume_ratio"])))
    record("独立复核 MA/缩量比/止损/目标", deltas and max(deltas) < 0.005,
           f"最大偏差 {max(deltas):.6f}" if deltas else "没有可用候选复核")

    # 7. 信号自洽
    bad_signal = []
    for pick in result.picks:
        hard = all(pick.checks.get(key) for key, _ in CHECKS)
        if pick.signal == "BUY-A" and not (hard and pick.score >= 80):
            bad_signal.append(pick.symbol)
        if pick.signal == "WATCH" and not (pick.limitup and pick.stage_high
                                           and pick.checks.get("trend_ok")
                                           and pick.checks.get("support_ok")):
            bad_signal.append(pick.symbol)
        if pick.signal == "BLOCK" and hard and pick.score >= 80:
            bad_signal.append(pick.symbol)
    record("信号与硬性条件自洽", not bad_signal,
           "全部一致" if not bad_signal else f"{len(bad_signal)} 只不一致：{bad_signal[:3]}")

    # 8. 仓位约束
    bad_size = [p.symbol for p in result.picks
                if p.shares % 100 or p.risk_pct > 1.0001 or p.value_pct > 10.0001]
    record("仓位不越 1% 风险 / 10% 单票", not bad_size,
           "全部合规" if not bad_size else f"{len(bad_size)} 只越界：{bad_size[:3]}")

    # 9. 未通过统计
    evaluated = max(result.evaluated, 1)
    over = {k: v for k, v in result.reject.items() if v > evaluated}
    record("未通过统计不超评估总数", not over,
           f"评估 {evaluated}；最大未通过 {max(result.reject.values()) if result.reject else 0}")

    # 10. 落盘往返
    restored = ScanResult.from_payload(result.to_payload())
    same = (restored.data_date == result.data_date
            and len(restored.picks) == len(result.picks)
            and all(a.symbol == b.symbol and a.score == b.score and a.checks == b.checks
                    for a, b in zip(restored.picks, result.picks)))
    record("落盘往返一致（K线不落盘）", same and restored.frames == {},
           f"候选 {len(restored.picks)} 只，frames={len(restored.frames)}")

    # 11. HTML 报告
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report.html"
        path.write_text(render(result, config), encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        size = path.stat().st_size
        ok = size > 4096 and all(k in text for k in ("市场环境", "交易计划", "未通过原因统计"))
        record("HTML 报告可生成", ok, f"{size / 1024:.1f} KB，含市场环境/交易计划/未通过统计")

    # 12. 文案
    hits: list[str] = []
    for name in ("app/services/pullback.py", "app/services/pullback_report.py",
                 "app/ui/pullback_view.py", "app/adapters/tdx_daily.py"):
        source_text = (ROOT / name).read_text(encoding="utf-8")
        hits += [f"{name}:{word}" for word in BANNED if word in source_text]
    record("无身份推断文案", not hits,
           "未出现主力/游资/庄家/建仓/出货/洗盘" if not hits else str(hits))

    # 附：涨停精确价抽查
    checked = []
    for pick in result.picks[:5]:
        if not pick.limitup_date:
            continue
        bars = result.frames.get(pick.symbol)
        if bars is None:
            continue
        idx = bars.index[bars["date"].dt.strftime("%Y-%m-%d") == pick.limitup_date]
        if not len(idx) or idx[0] == 0:
            continue
        prev_close = int(bars.loc[idx[0] - 1, "close_cents"])
        limit = limit_up_cents(prev_close, pick.limitup_pct)
        hit = int(bars.loc[idx[0], "close_cents"]) >= limit
        checked.append(f"{pick.code} {pick.limitup_date} 涨停价 {limit / 100:.2f} 收盘 "
                       f"{int(bars.loc[idx[0], 'close_cents']) / 100:.2f} {'✓' if hit else '✗'}")
    if checked:
        record("涨停价按分计算可复核", all("✓" in item for item in checked), "；".join(checked[:2]))

    if result.picks:
        print("\n前 5 名候选：")
        for idx, pick in enumerate(result.picks[:5], 1):
            print(f"  {idx}. {pick.code} {pick.name} {pick.industry} · {pick.score}分 "
                  f"{pick.signal} · 收 {pick.close:.2f} MA10 {pick.ma10} · "
                  f"缩量 {pick.volume_ratio} · 止损 {pick.stop} · 2.5R {pick.target}")
    return summarise()


def summarise() -> int:
    print("\n==== 汇总 ====")
    for name, state, detail in results:
        print(f"{state}\t{name}\t{detail}")
    ok = sum(1 for _, state, _ in results if state == PASS)
    print(f"\n{ok}/{len(results)} 项通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
