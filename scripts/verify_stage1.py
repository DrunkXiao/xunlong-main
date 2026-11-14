"""第一阶段验收脚本：用真实 tqcenter 数据逐条验证 MVR 验收标准。

运行：cd F:/workcn/寻龙盯盘 && python scripts/verify_stage1.py

验收项（与 SKILL.md 第一阶段一致）：
  1. 冷启动加载 5000+ 代码
  2. 代码/名称/拼音搜索可用
  3. 分组增删、组内排序落库，重开数据库顺序不变
  4. 真实快照价格入 quotes_cache
  5. 触发一条价格预警并落库 alert_events
  6. 采样分时能产生差分点（成交量为 0 时标 no_update）
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
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
from app.services.universe import UniverseService

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


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

    print(f"数据库：{db_path}")
    caps = adapter.capabilities
    print(f"数据源：{config['datasource']['name']} · {caps.data_level} 级 · {caps.summary()}\n")

    # 1. 冷启动股票池
    started = time.time()
    universe = UniverseService(repo, adapter, config)
    summary = universe.load()
    elapsed = time.time() - started
    record("冷启动加载股票池", summary["count"] >= 5000,
           f"{summary['count']} 只，耗时 {elapsed:.2f}s，名称来源 {summary['name_source']}，"
           f"错误 {summary['error'] or '无'}")

    # 2. 搜索
    code_hit = universe.search("600519")
    name_hit = universe.search("茅台")
    universe.pinyin.build(repo.all_stocks())
    pinyin_hit = universe.search("gzmt")
    record("代码搜索", bool(code_hit) and code_hit[0]["symbol"] == "600519.SH",
           f"{code_hit[0]['name']} {code_hit[0]['symbol']}" if code_hit else "无结果")
    record("名称搜索", bool(name_hit) and name_hit[0]["symbol"] == "600519.SH",
           f"{name_hit[0]['name']}" if name_hit else "无结果")
    record("拼音首字母搜索", bool(pinyin_hit) and pinyin_hit[0]["symbol"] == "600519.SH",
           f"{pinyin_hit[0]['symbol']}" if pinyin_hit else "不可用（缺 pypinyin 或未建索引）")

    # 3. 分组与排序持久化
    group_id = None
    rule_id = None
    alert_count_before = repo.count_alerts()
    try:
        group_id = repo.create_group("阶段一验收池")
        repo.add_symbols(group_id, ["600519.SH", "000001.SZ", "300750.SZ"])
        repo.move_symbol(group_id, "300750.SZ", 0)
        expected = ["300750.SZ", "600519.SH", "000001.SZ"]

        fresh = Repo(Database(db_path))
        persisted = fresh.group_symbols(group_id)
        record("分组新增/加入/排序落库", persisted == expected, f"重开数据库后顺序 {persisted}")

        original_order = [g["id"] for g in repo.groups()]
        repo.reorder_groups(list(reversed(original_order)))
        reloaded = [g["id"] for g in Repo(Database(db_path)).groups()]
        record("分组顺序落库", reloaded == list(reversed(original_order)), "逆序保存后重开一致")
        repo.reorder_groups(original_order)      # 还原，避免污染生产库

        # 4. 真实行情
        quote = adapter.get_quote("600519.SH")
        repo.upsert_quotes([quote])
        cached = repo.cached_quotes(["600519.SH"]).get("600519.SH")
        price_match = cached is not None and quote.price is not None and \
            abs(cached["price"] - quote.price) < 1e-6
        record("真实快照价格入缓存", price_match,
               f"{quote.name} 最新 {quote.price:.2f} {quote.change_pct:+.2f}% "
               f"成交额 {(quote.amount or 0)/1e8:.2f}亿 等级 {quote.data_level}")

        # 5. 采样分时差分
        sampler = IntradaySampler()
        first = sampler.feed(quote)
        time.sleep(1.2)
        quote2 = adapter.get_quote("600519.SH")
        point = sampler.feed(quote2)
        if first is None and point is not None:
            record("采样分时差分", True,
                   f"区间量 {point.interval_volume} 股，区间额 {point.interval_amount:.0f} 元，"
                   f"方向 {point.direction}，质量 {point.quality_flag}")
        else:
            record("采样分时差分", False, "首帧未建立基线或第二个采样点为空")

        # 6. 价格预警触发并落库
        service = AlertService(repo, AlertEngine(cooldown_sec=300), require_trading=False)
        target = round((quote.price or 1.0) * 0.99, 2)
        rule_id = service.create_price_rule("600519.SH", "price_above", target, level="P3")
        fired = service.evaluate([quote], trading=True)
        events = repo.recent_alerts(5)
        ok = bool(fired) and repo.count_alerts() == alert_count_before + 1 and bool(events)
        record("预警触发并落库", ok,
               f"规则 #{rule_id} 阈值 {target}，事件 {events[0]['event_type'] if events else '无'} "
               f"价格 {events[0]['trigger_price'] if events else '-'} "
               f"等级 {events[0]['level'] if events else '-'}")

        # 7. 冷却去重
        again = service.evaluate([quote], trading=True)
        record("预警冷却去重", again == [] and repo.count_alerts() == alert_count_before + 1,
               "同规则冷却期内不重复写入")
    finally:
        # 清理验收残留，保持生产库干净
        if rule_id:
            repo.delete_rule(rule_id)
        if group_id:
            repo.delete_group(group_id)
        if repo.count_alerts() > alert_count_before:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM alert_events WHERE id IN (SELECT id FROM alert_events "
                    "ORDER BY id DESC LIMIT ?)",
                    (repo.count_alerts() - alert_count_before,),
                )
        print("\n已清理验收用分组/规则/事件")

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
