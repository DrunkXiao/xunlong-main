"""涨停四阶段选股验收脚本（真实本地日线，不是合成数据）。

运行：cd F:/workcn/寻龙盯盘 && python scripts/verify_limitup.py
     python scripts/verify_limitup.py --limit 600      # 只扫前 600 只，快速自查
     python scripts/verify_limitup.py --date 20261106  # 指定交易日回放

验收项：
  1. 本地通达信目录可定位，sh/sz 日线目录存在
  2. 交易日历可读，最新交易日不晚于今天
  3. 股票池口径：只含 60/00/30，无 ST，日线根数 ≥ min_bars，非次新
  4. base.dbf 的流通股/行业可读（抽样非空率），换手率才算得出来
  5. 全市场扫描：股票池/成功/异常 数量自洽
  6. 四阶段数量自洽：分组大小 = 计数，精选 ≤ 上限且不重复、不含淘汰
  7. 涨停价按「四舍五入到分」独立复核（Decimal 重算，逐只比对）
  8. 独立复核 Top N：5 日量能、成交额倍数、换手率、评分分母与评级
  9. 评分门控：S1/S2 分母 55、S3 75、S4 100，未验证维度必须 0 分
 10. 承接类型与二次启动检查自洽（A/B/C 才 ok，confirmed 必须七项齐）
 11. 状态机落库往返：临时库 → upsert → flush → 重新加载，状态与首板基准不丢
 12. HTML 报告七节齐全，且不含任何资金身份推断词
 13. 数据完整度上限 ≈0.706（本地日线缺 5 个分时字段），置信不得高于 C
 14. 源码不含禁用词

不写正式数据库、不改任何行情文件；临时库与报告都写在系统临时目录，跑完删除。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.tdx_daily import (  # noqa: E402
    TdxDailySource, in_limitup_scope, is_st_name, resolve_root,
)
from app.config import load_config  # noqa: E402
from app.db.repo import Repo  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.services.limitup import (  # noqa: E402
    CAPACITY, DAILY_FIELDS, INTRADAY_FIELDS, WEIGHTS, build_snapshot, data_completeness,
)
from app.services.limitup_pool import LimitupPool  # noqa: E402
from app.services.limitup_report import render  # noqa: E402
from app.services.limitup_scanner import LimitupParams, scan  # noqa: E402

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []
BANNED = ("主力", "游资", "庄家", "建仓", "出货", "洗盘")
SCHEMA = ROOT / "app" / "db" / "schema.sql"


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


def limit_price(prev_close: float, rate: float) -> float:
    """完全独立的涨停价实现：Decimal 四舍五入到分，刻意不复用引擎代码。"""
    value = Decimal(str(prev_close)) * (Decimal(1) + Decimal(str(rate)))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只扫前 N 只（0 = 全市场）")
    parser.add_argument("--date", type=int, default=0, help="交易日 YYYYMMDD（0 = 最新）")
    args = parser.parse_args()

    config = load_config(str(ROOT / "config.yaml"))
    params = LimitupParams.from_config(config)
    params.limit = args.limit

    # ---------------------------------------------------------------- 1 目录
    root = resolve_root(config)
    record("定位通达信目录", root is not None, str(root or "未找到 vipdoc"))
    if root is None:
        return summarise()
    need = [Path(root) / "vipdoc" / name / "lday" for name in ("sh", "sz")]
    record("sh/sz 日线目录存在", all(p.is_dir() for p in need),
           "、".join(str(p) for p in need))

    source = TdxDailySource.from_config(config)
    print(f"数据源：{source.root} · 市场 {source.markets} · 每股取 {source.tail_bars} 根\n")

    # ---------------------------------------------------------------- 2 交易日
    calendar = source.calendar()
    today = int(date.today().strftime("%Y%m%d"))
    latest = calendar[-1] if calendar else None
    record("交易日历可读且最新日不晚于今天",
           bool(calendar) and latest <= today,
           f"共 {len(calendar)} 个交易日，最新 {latest}（今天 {today}）")

    # ---------------------------------------------------------------- 3 股票池
    target = args.date or latest
    universe = source.universe(date=target, min_list_days=params.board_min_days,
                               require_caps=True, limit=params.limit)
    out_of_scope = [r["symbol"] for r in universe if not in_limitup_scope(r["code"])]
    st_rows = [r["symbol"] for r in universe if is_st_name(r.get("name"))]
    short = [r["symbol"] for r in universe if r["bars"] < params.min_bars]
    no_cap = [r["symbol"] for r in universe if not r.get("float_share")]
    record("股票池口径", not out_of_scope and not st_rows and not short and not no_cap,
           f"{len(universe)} 只；越界 {len(out_of_scope)}、ST {len(st_rows)}、"
           f"日线不足 {len(short)}、缺股本 {len(no_cap)}")

    # ---------------------------------------------------------------- 4 股本/行业
    caps = source.caps()
    sectors = source.sectors()
    named = sum(1 for r in universe if r.get("name") and r["name"] != r["code"])
    with_cap = sum(1 for r in universe if r.get("float_share"))
    with_sector = sum(1 for r in universe
                      if (sectors.get("by_stock") or {}).get(r["symbol"]))
    record("股本/行业来自本地缓存（决定换手率与板块分）",
           with_cap / max(len(universe), 1) > 0.95 and with_sector / max(len(universe), 1) > 0.95,
           f"股本表 {len(caps)} 条；名称 {named}/{len(universe)}、"
           f"股本 {with_cap}/{len(universe)}、行业 {with_sector}/{len(universe)}")

    # ---------------------------------------------------------------- 5 扫描
    with tempfile.TemporaryDirectory(prefix="limitup_verify_") as tmp:
        db = Database(Path(tmp) / "verify.db", schema_path=SCHEMA)
        db.init_schema()
        pool = LimitupPool(Repo(db))

        result = scan(source, params, pool, date=args.date or None)
        record("扫描数量自洽",
               result.scanned > 0 and result.universe == result.scanned + result.errors,
               f"股票池 {result.universe} · 有效快照 {result.scanned} · 跳过 {result.errors} · "
               f"耗时 {result.elapsed:.1f}s · 数据日 {result.data_date} · 滞后 {result.stale_days} 天")

        counts = result.counts
        group_total = sum(counts.values())
        all_candidates = result.all_candidates
        record("四阶段分组与计数一致",
               group_total == len(all_candidates) and group_total == sum(
                   len(result.groups.get(k) or []) for k in ("S1", "S2", "S3", "S4")),
               "、".join(f"{k} {v}" for k, v in counts.items()) + f"；合计 {group_total}")

        # ------------------------------------------------------------ 6 精选
        codes = [c["snap"]["code"] for c in result.picks]
        over_capacity = [
            stage for stage, cap in CAPACITY.items()
            if len([c for c in result.picks if c["stage"] == stage]) > cap
        ]
        record("精选不超过上限、不重复、不含淘汰",
               len(result.picks) <= params.top and len(codes) == len(set(codes))
               and not over_capacity
               and all(c["score"]["grade"] != "淘汰" for c in result.picks),
               f"精选 {len(result.picks)}/{params.top}；容量越界 {over_capacity or '无'}；"
               f"重复 {len(codes) - len(set(codes))}")

        # ------------------------------------------------------------ 7 涨停价
        checks = []
        for cand in all_candidates[:12]:
            snap = cand["snap"]
            if not snap.get("is_limit_up"):
                continue
            bars = result.frames.get(snap["symbol"])
            if not bars or len(bars) < 2:
                continue
            prev_close = bars[-2]["close"]
            expect = limit_price(prev_close, snap["limit_rate"])
            checks.append((snap["code"], prev_close, expect, snap["close"]))
        bad = [c for c in checks if abs(c[2] - c[3]) > 1e-9]
        record("涨停价按四舍五入到分可复核",
               bool(checks) and not bad,
               f"抽查 {len(checks)} 只，偏差 {len(bad)}" + (
                   "；" + "，".join(f"{c[0]} 前收 {c[1]} → {c[2]:.2f}" for c in checks[:3])
                   if checks else "；当天没有首板样本"))

        # ------------------------------------------------------------ 8 Top N 复核
        deltas = []
        for cand in result.picks[:5]:
            snap = cand["snap"]
            bars = result.frames.get(snap["symbol"])
            if not bars or len(bars) < 6:
                continue
            base = sum(b["volume"] for b in bars[-6:-1]) / 5
            expect_v5 = round(bars[-1]["volume"] / base, 2) if base else None
            base_amt = sum(b["amount"] for b in bars[-6:-1]) / 5
            expect_a5 = round(bars[-1]["amount"] / base_amt, 2) if base_amt else None
            if expect_v5 is not None:
                deltas.append(abs((snap["vol_ratio5"] or 0) - expect_v5))
            if expect_a5 is not None:
                deltas.append(abs((snap["amount_ratio5"] or 0) - expect_a5))
            if snap.get("turnover") is not None:
                share = next((r.get("float_share") for r in universe
                              if r["code"] == snap["code"]), None)
                if share:
                    deltas.append(abs(snap["turnover"] - round(
                        bars[-1]["volume"] / (share * 10000.0) * 100, 2)))
        record("独立复核量能倍数/换手率", deltas and max(deltas) <= 0.02,
               f"最大偏差 {max(deltas):.4f}" if deltas else "没有可复核的样本")

        # ------------------------------------------------------------ 9 门控分母
        gate_bad = []
        for cand in all_candidates:
            stage = cand["stage"]
            expect_max = 55.0 if stage in ("S1", "S2") else 75.0 if stage == "S3" else 100.0
            if abs(cand["score"]["max_raw"] - expect_max) > 1e-6:
                gate_bad.append(f"{cand['snap']['code']} {stage} 分母 {cand['score']['max_raw']}")
            for dim, info in cand["score"]["detail"].items():
                if not info.get("verified") and info["points"] != 0.0:
                    gate_bad.append(f"{cand['snap']['code']} {stage} 未验证维度 {dim} 有分")
        record("评分分母随阶段变化且未验证维度不计分", not gate_bad,
               f"检查 {len(all_candidates)} 只；问题 {gate_bad[:3] or '无'}")

        # ------------------------------------------------------------ 10 承接/启动
        logic_bad = []
        for cand in all_candidates:
            carry = cand.get("carry")
            launch = cand.get("launch")
            if carry:
                expect_ok = carry["carry_type"] in ("A", "B", "C")
                if carry.get("ok") != expect_ok:
                    logic_bad.append(f"{cand['snap']['code']} 承接 {carry['carry_type']} ok 不一致")
            if launch and launch.get("confirmed"):
                if not all(launch.get("checks", {}).values()):
                    logic_bad.append(f"{cand['snap']['code']} 已确认启动但检查项不全")
        record("承接类型与二次启动判定自洽", not logic_bad,
               f"当日承接样本 {sum(1 for c in all_candidates if c.get('carry'))}、"
               f"启动样本 {sum(1 for c in all_candidates if c.get('launch'))}；"
               f"问题 {logic_bad[:3] or '无'}")

        # ------------------------------------------------------------ 11 跨日推进
        # S3/S4 只在"池里已有昨日首板"时才可能出现，单日空池永远为 0。
        # 3 天窗口常常凑不齐「首板→三倍量→次日承接」，故回放 8 个交易日。
        replay_db = Database(Path(tmp) / "replay.db", schema_path=SCHEMA)
        replay_db.init_schema()
        replay_pool = LimitupPool(Repo(replay_db))
        replay = []
        carry_total = {"A": 0, "B": 0, "C": 0, "D": 0}
        for day in calendar[-8:]:
            step = scan(source, params, replay_pool, date=day)
            replay.append((day, step))
            for key, value in (step.carry_stats or {}).items():
                carry_total[key] = carry_total.get(key, 0) + value
            print(f"    · 回放 {day}：{'、'.join(f'{k} {v}' for k, v in step.counts.items())}"
                  f" · 承接 {step.carry_stats}")
        states = {r.get("state") for r in replay_pool.records.values()}
        reached = sorted(s for s in states if s in ("S4", "S5", "S6", "S7", "S8"))
        with_history = sum(1 for r in replay_pool.records.values()
                           if len(r.get("history") or []) > 1)
        replay_candidates = [c for _, step in replay for c in step.all_candidates]
        launch_confirmed = [c for c in replay_candidates
                            if (c.get("launch") or {}).get("confirmed")]
        replay_bad = []
        for cand in replay_candidates:
            carry = cand.get("carry")
            if carry and carry.get("ok") != (carry["carry_type"] in ("A", "B", "C")):
                replay_bad.append(f"{cand['snap']['code']} 承接 ok 不一致")
            launch = cand.get("launch")
            if launch and launch.get("confirmed") and not all(launch["checks"].values()):
                replay_bad.append(f"{cand['snap']['code']} 启动确认但检查项不全")
        record("跨日推进（连续 8 个交易日共用一个池）",
               bool(replay_pool.records) and sum(carry_total.values()) > 0
               and bool(reached) and not replay_bad,
               f"{replay[0][0]} → {replay[-1][0]}；承接 A{carry_total['A']}/B{carry_total['B']}"
               f"/C{carry_total['C']}/D{carry_total['D']}；推进到 {reached or '无'}；"
               f"多段历史 {with_history} 条；二次启动确认 {len(launch_confirmed)} 只；"
               f"最终 {replay_pool.summary_text()}"
               + (f"；问题 {replay_bad[:3]}" if replay_bad else ""))

        # ------------------------------------------------------------ 12 落库往返
        pool.flush()
        reloaded = LimitupPool(pool.repo)
        lost = []
        for symbol, rec in pool.records.items():
            back = reloaded.get(symbol)
            if back is None or back.get("state") != rec.get("state"):
                lost.append(symbol)
            elif back.get("first_board_date") != rec.get("first_board_date"):
                lost.append(f"{symbol}(首板基准)")
        record("状态机落库往返", bool(pool.records) and not lost,
               f"池内 {len(pool.records)} 条，丢失 {lost[:3] or '无'}；{result.pool_text}")

        # ------------------------------------------------------------ 12 报告
        html = render(result)
        sections = ["市场环境", "四阶段股票池", "最终精选", "次日交易计划",
                    "重点关注", "风险提示", "股票池状态"]
        missing = [s for s in sections if s not in html]
        hits = [w for w in BANNED if w in html]
        record("HTML 报告七节齐全且不含身份推断", not missing and not hits,
               f"{len(html) / 1024:.1f} KB；缺 {missing or '无'}；禁用词 {hits or '无'}")

    # ---------------------------------------------------------------- 13 完整度
    sample = all_candidates[0]["snap"] if all_candidates else None
    if sample is None and result.groups.get("S1"):
        sample = result.groups["S1"][0]["snap"]
    ratio = None
    if sample:
        ratio, missing_fields = data_completeness(sample)
        record("数据完整度上限与置信度",
               ratio <= round(12 / 17, 3) + 1e-9
               and set(missing_fields) <= set(INTRADAY_FIELDS) | set(DAILY_FIELDS)
               and all(c["score"]["confidence"] in ("C", "D") for c in all_candidates),
               f"完整度 {ratio:.3f}（上限 {round(12/17, 3)}）、缺 {len(missing_fields)} 项、"
               f"置信最高 {min((c['score']['confidence'] for c in all_candidates), default='—')}")
    else:
        record("数据完整度上限与置信度", False, "没有候选快照可检查")

    # ---------------------------------------------------------------- 14 源码
    hits = []
    for name in ("app/services/limitup.py", "app/services/limitup_pool.py",
                 "app/services/limitup_scanner.py", "app/services/limitup_report.py",
                 "app/ui/limitup_view.py", "app/adapters/tdx_daily.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        hits += [f"{name}:{word}" for word in BANNED if word in text]
    record("源码无身份推断文案", not hits,
           "未出现主力/游资/庄家/建仓/出货/洗盘" if not hits else str(hits))

    # ---------------------------------------------------------------- 明细
    print("\n市场环境：", {k: v for k, v in result.market.items()
                          if k in ("limit_up_count", "first_board_count", "broken_count",
                                   "broken_rate", "sentiment", "main_sector")})
    if result.picks:
        print("\n前 5 只精选：")
        for idx, cand in enumerate(result.picks[:5], 1):
            snap = cand["snap"]
            plan = cand["plan"]
            print(f"  {idx}. {snap['code']} {snap['name']} {snap['sector']} · "
                  f"{cand['stage_label']} {cand['score']['normalized']}分 "
                  f"{cand['score']['grade']} · 收 {snap['close']:.2f} "
                  f"({snap['pct']:+.2f}%) · 量能 {snap['vol_ratio5']}倍 "
                  f"换手 {snap['turnover']}% · 支撑 {plan['support']} 止损 {plan['stop']} "
                  f"目标 {plan['target1']}/{plan['target2']}")
    elif result.pending:
        print("\n无确认标的，以下为待观察（只给触发条件，不给结论）：")
        for cand in result.pending[:5]:
            print(f"  · {cand['snap']['code']} {cand['snap']['name']} {cand['state_label']}")
    else:
        print("\n当日无任何阶段确认的标的（不凑数）。")
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
