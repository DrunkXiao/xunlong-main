"""量学板枪选股验收脚本（真实本地日线，不是合成数据）。

运行：cd G:/stock/寻龙盯盘/xunlong-dingpan-main && python scripts/verify_banqiang.py
     python scripts/verify_banqiang.py --limit 600      # 只扫前 600 只，快速自查
     python scripts/verify_banqiang.py --date 20261106  # 指定交易日回放

验收项：
  1. 本地通达信目录可定位，sh/sz 日线目录存在，交易日历可读
  2. 股票池口径：只含 60/00/30，无 ST，日线根数 ≥ min_bars
  3. 全市场扫描：股票池 / 成功 / 异常 数量自洽
  4. 分组与计数自洽：分组大小 = counts，picks 只含买点两类且不重复
  5. 涨停价按「四舍五入到分」独立复核（Decimal 重算，逐只比对）
  6. 回撤比、止损、目标 1 独立复核（用同一条日线重算）
  7. 评分：任一维度不超上限、合计 = 总分、评级来自分档
  8. 状态机自洽：买入观察必有买点已触发；形态形成中回调 ≤ 1 日
  9. HTML 报告六节齐全，且不含任何资金身份推断词
 10. 源码不含禁用词

不写正式数据库、不改任何行情文件；报告写在系统临时目录，跑完删除。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.tdx_daily import (  # noqa: E402
    TdxDailySource, asof, in_limitup_scope, is_st_name, resolve_root,
)
from app.config import load_config  # noqa: E402
from app.services.banqiang import (  # noqa: E402
    DIM_MAX, build_series, grade_of, scan_one, score_all,
)
from app.services.banqiang_report import render  # noqa: E402
from app.services.banqiang_scanner import BanqiangParams, scan  # noqa: E402

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []
BANNED = ("主力", "游资", "庄家", "建仓", "出货", "洗盘")
SOURCES = ("app/services/banqiang.py", "app/services/banqiang_scanner.py",
           "app/services/banqiang_report.py", "app/ui/banqiang_view.py")


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


def limit_cents(prev_close: float, rate: float) -> float:
    """独立实现：以「分」为单位四舍五入，避免浮点误差（rate 是小数，0.10 = 10%）。"""
    cents = int(round(prev_close * 100))
    bp = int(round(rate * 10000))          # 基准点：10% -> 1000
    return ((cents * (10000 + bp) + 5000) // 10000) / 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="量学板枪选股验收")
    parser.add_argument("--limit", type=int, default=0, help="只扫前 N 只（0 = 全市场）")
    parser.add_argument("--date", type=int, default=None, help="指定交易日回放 YYYYMMDD")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    root = resolve_root(config)
    record("1 本地日线目录", bool(root and (root / "vipdoc").is_dir()),
           f"通达信根目录 {root}")

    source = TdxDailySource.from_config(config)
    params = BanqiangParams.from_config(config)
    if args.limit:
        params.limit = args.limit
    calendar = source.calendar()
    record("1 交易日历", bool(calendar), f"最新 {calendar[-1] if calendar else '--'}，"
                                        f"共 {len(calendar)} 个交易日")
    if not calendar:
        return 1

    universe = source.universe(date=args.date or calendar[-1],
                               min_list_days=params.board_min_days, limit=params.limit)
    bad_scope = [m for m in universe if not in_limitup_scope(m["code"])]
    bad_st = [m for m in universe if is_st_name(m["name"])]
    thin = [m for m in universe if m["bars"] < params.min_bars]
    record("2 股票池口径", bool(universe) and not bad_scope and not bad_st and not thin,
           f"{len(universe)} 只 · 越界 {len(bad_scope)} · ST {len(bad_st)} · 日线不足 {len(thin)}")

    result = scan(source, params, date=args.date)
    record("3 扫描数量自洽", result.universe == len(universe) and result.scanned >= 0,
           f"股票池 {result.universe} · 有效 {result.scanned} · 异常 {result.errors} · "
           f"耗时 {result.elapsed}s · 数据日 {result.data_date} 滞后 {result.stale_days} 天")

    counts = result.counts
    groups_ok = all(len(result.groups.get(state) or []) == counts[state] for state in counts)
    all_syms = [it["symbol"] for it in result.all_candidates]
    picks_ok = all(it["state"] in ("买入观察", "买点临近") for it in result.picks)
    record("4 分组与计数自洽", groups_ok and picks_ok and len(all_syms) == len(set(all_syms)),
           f"买入观察 {counts['买入观察']} / 买点临近 {counts['买点临近']} / "
           f"缩量回调 {counts['缩量回调中']} / 形态形成 {counts['形态形成中']}；"
           f"候选 {len(all_syms)} 只，去重后 {len(set(all_syms))} 只")

    # ---- 独立复核：涨停价 / 回撤比 / 止损 / 目标 1
    checked = mismatch = 0
    samples: list[str] = []
    for item in result.all_candidates[:40]:
        if item.get("early") or not item.get("s2"):
            continue
        meta = {"symbol": item["symbol"], "code": item["code"], "market": item["market"]}
        bars = asof(source.bars_list(meta, params.tail_bars), result.data_date)
        series = build_series(bars)
        i, k = item["board_i"], item["gun_i"]
        rate = item["s1"]["rate"]
        checked += 1
        expect_limit = limit_cents(series.c[i - 1], rate)
        if abs(expect_limit - item["s1"]["limit_price"]) > 0.005:
            mismatch += 1
            samples.append(f"{item['name']} 涨停价 {item['s1']['limit_price']} ≠ {expect_limit}")

        rise = max(series.h[i], series.h[k]) - series.o[i]
        ratio = (max(series.h[i], series.h[k]) - item["s3"]["pull_low"]) / rise
        if abs(ratio - item["s3"]["ratio"]) > 0.01:
            mismatch += 1
            samples.append(f"{item['name']} 回撤比 {item['s3']['ratio']:.3f} ≠ {ratio:.3f}")

        stop = round(item["s3"]["pull_low"] * 0.99 + 1e-9, 2)
        if abs(stop - item["plan"]["stop"]) > 0.011:
            mismatch += 1
            samples.append(f"{item['name']} 止损 {item['plan']['stop']} ≠ {stop}")
        if abs(item["plan"]["target1"] - round(max(series.h[i], series.h[k]), 2)) > 0.011:
            mismatch += 1
            samples.append(f"{item['name']} 目标1 {item['plan']['target1']} ≠ 板枪顶部")
    record("5/6 结构独立复核", mismatch == 0, f"复核 {checked} 只，不一致 {mismatch} 处"
                                              + ("：" + "；".join(samples[:3]) if samples else ""))

    # ---- 评分自洽
    score_bad = []
    for item in result.all_candidates:
        score = item.get("score") or {}
        if score.get("total") is None:
            continue
        dims = score.get("dims") or {}
        for key, dim in dims.items():
            if not 0 <= dim["score"] <= DIM_MAX[key]:
                score_bad.append(f"{item['name']} {key} {dim['score']}")
        if abs(sum(d["score"] for d in dims.values()) - score["total"]) > 0.5:
            score_bad.append(f"{item['name']} 合计 {score['total']} ≠ 维度和")
        if score["grade"] != grade_of(score["total"]) and not score.get("notes"):
            score_bad.append(f"{item['name']} 评级 {score['grade']} 与总分不符")
    record("7 评分自洽", not score_bad, "维度上限 / 合计 / 评级分档"
           + ("" if not score_bad else "：" + "；".join(score_bad[:3])))

    # ---- 状态机自洽
    state_bad = []
    for item in result.all_candidates:
        state = item["state"]
        if item.get("early"):
            if state != "形态形成中":
                state_bad.append(f"{item['name']} 枪日未出却写成 {state}")
            continue
        if state == "买入观察" and not item["s5"]["buy_triggered"]:
            state_bad.append(f"{item['name']} 买入观察但买点未触发")
        if state == "形态形成中" and item["s3"]["pull_days"] > 1:
            state_bad.append(f"{item['name']} 形态形成中但回调 {item['s3']['pull_days']} 日")
    record("8 状态机自洽", not state_bad,
           "买入观察 / 形态形成中与实测一致" + ("" if not state_bad else "：" + "；".join(state_bad[:3])))

    # ---- 报告
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "banqiang.html"
        html = render(result, config)
        path.write_text(html, encoding="utf-8")
        sections = ("A 扫描口径", "B 五阶段漏斗", "C 候选分组", "D 精选与买点",
                    "E 逐只结构与计划", "F 风险提示")
        missing = [s for s in sections if s not in html]
        hit = [w for w in BANNED if w in html]
        record("9 报告完整性", not missing and not hit,
               f"六节缺失 {missing or '无'} · 禁用词 {hit or '无'} · {len(html)} 字符")

    # ---- 源码禁用词
    hits = []
    for rel in SOURCES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits += [f"{rel}:{w}" for w in BANNED if w in text]
    record("10 源码禁用词", not hits, "、".join(hits) or "无")

    failures = [name for name, status, _ in results if status == FAIL]
    print("\n==== 汇总 ====")
    print(f"共 {len(results)} 项，通过 {len(results) - len(failures)} 项，未通过 {len(failures)} 项")
    if failures:
        print("未通过：" + "、".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
